from __future__ import annotations

import json
import math
import os
import stat
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from app.personal_wechat_bot.runtime.process_lock import (
    ProcessLockError,
    blocking_process_lock,
    process_pid_alive,
    process_start_marker,
)


@dataclass
class _ActiveProcessFence:
    label: str
    owner_thread_ident: int
    phase: str = "acquiring"
    adopters: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _StartupHandoffSpec:
    file_name: str
    owner_token: str
    lease_id: str


@dataclass(frozen=True)
class _AdoptedStartupHandoff:
    lease: HistoryWriterLease
    ready_path: Path
    owner_token: str
    deadline_epoch: float


_LOCAL = threading.local()
_LEASE_PROCESS_PID = os.getpid()
_LEASE_PROCESS_INSTANCE = uuid.uuid4().hex
_FORKED_WITH_ACTIVE_HISTORY_FENCE = False
_ACTIVE_LEASE_TOKENS: set[str] = set()
_ACTIVE_LEASES_GUARD = threading.Lock()
_ACTIVE_PROCESS_FENCES: dict[str, _ActiveProcessFence] = {}
_ACTIVE_PROCESS_FENCES_CONDITION = threading.Condition(threading.Lock())

_STARTUP_HANDOFF_LEASE_KIND = "startup_handoff"
_STARTUP_HANDOFF_MAX_TTL_SECONDS = 30.0
_STARTUP_HANDOFF_FILE_ENV = "CHATBOT_HISTORY_STARTUP_HANDOFF_FILE"
_STARTUP_HANDOFF_TOKEN_ENV = "CHATBOT_HISTORY_STARTUP_HANDOFF_TOKEN"
_STARTUP_HANDOFF_LEASE_ID_ENV = "CHATBOT_HISTORY_STARTUP_HANDOFF_LEASE_ID"


def _reset_lease_process_identity() -> None:
    global _LOCAL
    global _LEASE_PROCESS_PID
    global _LEASE_PROCESS_INSTANCE
    global _FORKED_WITH_ACTIVE_HISTORY_FENCE
    global _ACTIVE_LEASE_TOKENS
    global _ACTIVE_LEASES_GUARD
    global _ACTIVE_PROCESS_FENCES
    global _ACTIVE_PROCESS_FENCES_CONDITION

    inherited_active_fence = bool(_ACTIVE_PROCESS_FENCES or _ACTIVE_LEASE_TOKENS)
    _LEASE_PROCESS_PID = os.getpid()
    _LEASE_PROCESS_INSTANCE = uuid.uuid4().hex
    _FORKED_WITH_ACTIVE_HISTORY_FENCE = inherited_active_fence
    _ACTIVE_LEASE_TOKENS = set()
    _ACTIVE_LEASES_GUARD = threading.Lock()
    _ACTIVE_PROCESS_FENCES = {}
    _ACTIVE_PROCESS_FENCES_CONDITION = threading.Condition(threading.Lock())
    _LOCAL = threading.local()


def _ensure_lease_process_identity() -> None:
    if _LEASE_PROCESS_PID != os.getpid():
        _reset_lease_process_identity()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_lease_process_identity)


def history_reset_fence_path(data_dir: str | Path) -> Path:
    return Path(data_dir).resolve() / "runtime_locks" / "history_reset_fence.lock"


def history_writer_lease_dir(data_dir: str | Path) -> Path:
    return Path(data_dir).resolve() / "runtime_locks" / "history_writer_leases"


def _history_fence_key(path: str | Path) -> str:
    """Return one process-local key for Windows path spelling aliases."""

    normalized = os.path.abspath(os.fspath(path))
    if os.name == "nt":
        normalized = normalized.replace("/", "\\")
        if normalized.lower().startswith("\\\\?\\unc\\"):
            normalized = "\\\\" + normalized[8:]
        elif normalized.startswith("\\\\?\\"):
            normalized = normalized[4:]
    return os.path.normcase(normalized)


def _thread_fence_depths() -> dict[str, int]:
    depths = getattr(_LOCAL, "depths", None)
    if not isinstance(depths, dict):
        depths = {}
        _LOCAL.depths = depths
    return depths


def _local_fence_wait_timeout(
    *,
    deadline: float,
    key: str,
    state: _ActiveProcessFence,
) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProcessLockError(
            f"timed out waiting for in-process history writer fence: {key}",
            holder={
                "pid": os.getpid(),
                "label": state.label,
                "thread_ident": state.owner_thread_ident,
                "phase": state.phase,
            },
        )
    return remaining


def _reraise_deferred_exception(
    deferred: tuple[BaseException, Any] | None,
) -> None:
    if deferred is None:
        return
    error, traceback = deferred
    raise error.with_traceback(traceback)


def _release_adopted_process_fence(
    key: str,
    state: _ActiveProcessFence,
    adoption_token: str,
) -> None:
    """Return an adopted ref before surfacing an interrupt."""

    deferred: tuple[BaseException, Any] | None = None
    released = False
    while not released:
        try:
            with _ACTIVE_PROCESS_FENCES_CONDITION:
                if _ACTIVE_PROCESS_FENCES.get(key) is state:
                    state.adopters.discard(adoption_token)
                released = True
                _ACTIVE_PROCESS_FENCES_CONDITION.notify_all()
        except BaseException as exc:
            if deferred is None:
                deferred = (exc, exc.__traceback__)
    _reraise_deferred_exception(deferred)


def _close_process_fence_state(
    key: str,
    state: _ActiveProcessFence,
) -> None:
    """Drain process-local adopters without releasing the OS fence early."""

    deferred: tuple[BaseException, Any] | None = None
    closed = False
    while not closed:
        try:
            with _ACTIVE_PROCESS_FENCES_CONDITION:
                if _ACTIVE_PROCESS_FENCES.get(key) is state:
                    state.phase = "closing"
                    _ACTIVE_PROCESS_FENCES_CONDITION.notify_all()
                    while state.adopters:
                        _ACTIVE_PROCESS_FENCES_CONDITION.wait()
                    _ACTIVE_PROCESS_FENCES.pop(key, None)
                closed = True
                _ACTIVE_PROCESS_FENCES_CONDITION.notify_all()
        except BaseException as exc:
            if deferred is None:
                deferred = (exc, exc.__traceback__)
    _reraise_deferred_exception(deferred)


def owned_history_root(start_dir: str | Path) -> Path | None:
    start = Path(start_dir).resolve()
    for candidate in (start, *start.parents):
        config_path = candidate / "config.json"
        if config_path.is_file() and not config_path.is_symlink():
            return candidate
    return None


def owned_history_roots(start_dirs: list[str | Path] | tuple[str | Path, ...]) -> tuple[Path, ...]:
    """Resolve, deduplicate, and lock-order all owned history roots."""

    roots: dict[str, Path] = {}
    for start_dir in start_dirs:
        root = owned_history_root(start_dir)
        if root is not None:
            roots[_history_fence_key(root)] = root
    return tuple(roots[key] for key in sorted(roots))


@contextmanager
def history_writer_fence(
    data_dir: str | Path,
    *,
    label: str,
    wait_timeout_seconds: float = 600.0,
) -> Iterator[None]:
    """Serialize history writers against destructive history reset."""

    _ensure_lease_process_identity()
    if _FORKED_WITH_ACTIVE_HISTORY_FENCE:
        raise RuntimeError(
            "history writes are disabled in a child forked inside an active history fence; fork+exec instead"
        )
    path = history_reset_fence_path(data_dir)
    key = _history_fence_key(path)
    depths = _thread_fence_depths()
    depth = int(depths.get(key, 0) or 0)
    if depth:
        depths[key] = depth + 1
        try:
            yield
        finally:
            remaining = int(depths.get(key, 1) or 1) - 1
            if remaining > 0:
                depths[key] = remaining
            else:
                depths.pop(key, None)
        return

    deadline = time.monotonic() + max(0.0, float(wait_timeout_seconds))
    state: _ActiveProcessFence | None = None
    adoption_token = ""
    acquire_process_fence = False
    try:
        while True:
            with _ACTIVE_PROCESS_FENCES_CONDITION:
                current = _ACTIVE_PROCESS_FENCES.get(key)
                if current is None:
                    state = _ActiveProcessFence(
                        label=str(label or "history_writer"),
                        owner_thread_ident=threading.get_ident(),
                    )
                    # Publish before attempting the OS-backed lock. A sibling
                    # thread must wait for this acquisition and then adopt it,
                    # rather than independently blocking on the same process lock.
                    acquire_process_fence = True
                    _ACTIVE_PROCESS_FENCES[key] = state
                    break
                if current.phase == "active":
                    state = current
                    adoption_token = uuid.uuid4().hex
                    state.adopters.add(adoption_token)
                    depths[key] = 1
                    break
                _ACTIVE_PROCESS_FENCES_CONDITION.wait(
                    timeout=_local_fence_wait_timeout(
                        deadline=deadline,
                        key=key,
                        state=current,
                    )
                )
    except BaseException:
        depths.pop(key, None)
        if state is not None and adoption_token:
            _release_adopted_process_fence(key, state, adoption_token)
        elif state is not None and acquire_process_fence:
            _close_process_fence_state(key, state)
        raise

    if state is None:  # pragma: no cover - defensive state-machine invariant
        raise RuntimeError("history writer fence state was not initialized")

    if not acquire_process_fence:
        try:
            yield
        finally:
            depths.pop(key, None)
            _release_adopted_process_fence(key, state, adoption_token)
        return

    try:
        _ensure_private_runtime_locks_dir(Path(data_dir).resolve())
        with blocking_process_lock(
            path,
            label=f"history_writer:{label}",
            stale_after_seconds=3600.0,
            wait_timeout_seconds=max(0.0, deadline - time.monotonic()),
        ):
            with _ACTIVE_PROCESS_FENCES_CONDITION:
                if _ACTIVE_PROCESS_FENCES.get(key) is not state:
                    raise RuntimeError("history writer fence state was replaced during acquisition")
                state.phase = "active"
                depths[key] = 1
                _ACTIVE_PROCESS_FENCES_CONDITION.notify_all()
            try:
                yield
            finally:
                depths.pop(key, None)
                _close_process_fence_state(key, state)
    except BaseException:
        _close_process_fence_state(key, state)
        raise


@contextmanager
def history_writer_fence_if_owned(
    start_dir: str | Path,
    *,
    label: str,
    wait_timeout_seconds: float = 600.0,
) -> Iterator[None]:
    root = owned_history_root(start_dir)
    if root is None:
        yield
        return
    with history_writer_fence(
        root,
        label=label,
        wait_timeout_seconds=wait_timeout_seconds,
    ):
        yield


@contextmanager
def history_writer_fences_if_owned(
    start_dirs: list[str | Path] | tuple[str | Path, ...],
    *,
    label: str,
    wait_timeout_seconds: float = 600.0,
) -> Iterator[tuple[Path, ...]]:
    roots = owned_history_roots(start_dirs)
    with ExitStack() as stack:
        for root in roots:
            stack.enter_context(
                history_writer_fence(
                    root,
                    label=label,
                    wait_timeout_seconds=wait_timeout_seconds,
                )
            )
        yield roots


@dataclass
class HistoryWriterLease:
    root: Path
    path: Path
    lease_id: str
    owner_token: str
    label: str
    creator_pid: int
    _released: bool = False
    _release_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def release(self) -> None:
        """Release this lease; safe to call from a submitted worker thread."""

        with self._release_guard:
            if self._released:
                return
            self._released = True
        if self.creator_pid != os.getpid():
            return
        try:
            with history_writer_fence(
                self.root,
                label=f"lease_release:{self.label}",
                wait_timeout_seconds=600.0,
            ):
                _unlink_owned_lease(self.path, self.owner_token)
        finally:
            with _ACTIVE_LEASES_GUARD:
                _ACTIVE_LEASE_TOKENS.discard(self.owner_token)

    def __enter__(self) -> HistoryWriterLease:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


@dataclass
class HistoryWriterStartupHandoff:
    """A bounded parent-to-child lease used while a worker is starting."""

    lease: HistoryWriterLease
    ready_path: Path
    deadline_epoch: float
    _released: bool = False
    _release_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def child_environment(self) -> dict[str, str]:
        if self.lease.creator_pid != os.getpid():
            raise RuntimeError("history startup handoff cannot be exported after fork")
        return {
            _STARTUP_HANDOFF_FILE_ENV: self.lease.path.name,
            _STARTUP_HANDOFF_TOKEN_ENV: self.lease.owner_token,
            _STARTUP_HANDOFF_LEASE_ID_ENV: self.lease.lease_id,
        }

    def ready_for_process(self, pid: int, *, expected_process_start: str) -> bool:
        try:
            expected_pid = int(pid)
        except (TypeError, ValueError):
            return False
        expected_start = str(expected_process_start or "").strip()
        if expected_pid <= 0 or not expected_start:
            return False
        path_stat = _safe_lstat(self.ready_path)
        if path_stat is None or _is_reparse_point(path_stat) or not stat.S_ISREG(path_stat.st_mode):
            return False
        try:
            payload = json.loads(self.ready_path.read_text(encoding="utf-8"))
            ready_pid = int(payload.get("pid") or 0) if isinstance(payload, dict) else 0
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError):
            return False
        if not isinstance(payload, dict):
            return False
        if (
            str(payload.get("lease_id") or "") != self.lease.lease_id
            or str(payload.get("owner_token") or "") != self.lease.owner_token
            or ready_pid != expected_pid
            or not str(payload.get("data_dir") or "")
            or _history_fence_key(str(payload.get("data_dir") or ""))
            != _history_fence_key(self.lease.root)
        ):
            return False
        recorded_start = str(payload.get("process_start") or "")
        current_start = process_start_marker(expected_pid)
        return bool(recorded_start == expected_start and current_start == expected_start)

    def release(self) -> None:
        self._finish(approve_child=True)

    def cancel(self) -> None:
        self._finish(approve_child=False)

    def _finish(self, *, approve_child: bool) -> None:
        with self._release_guard:
            if self._released:
                return
            self._released = True
        try:
            if approve_child:
                _unlink_owned_ready_file(self.ready_path, self.lease.owner_token)
        finally:
            self.lease.release()

    def __enter__(self) -> HistoryWriterStartupHandoff:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


@dataclass
class HistoryWriterLeaseGroup:
    leases: tuple[HistoryWriterLease, ...]
    _released: bool = False
    _release_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def release(self) -> None:
        with self._release_guard:
            if self._released:
                return
            self._released = True
        deferred: tuple[BaseException, Any] | None = None
        for lease in reversed(self.leases):
            try:
                lease.release()
            except BaseException as exc:
                if deferred is None:
                    deferred = (exc, exc.__traceback__)
        _reraise_deferred_exception(deferred)

    def __enter__(self) -> HistoryWriterLeaseGroup:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


def register_history_writer_lease(
    data_dir: str | Path,
    *,
    label: str,
    metadata: dict[str, Any] | None = None,
    wait_timeout_seconds: float = 600.0,
) -> HistoryWriterLease:
    """Register a writer lifetime while mutually excluded with history reset."""

    return _register_history_writer_lease(
        Path(data_dir).resolve(),
        label=label,
        metadata=metadata,
        wait_timeout_seconds=wait_timeout_seconds,
    )


def _register_history_writer_lease(
    root: Path,
    *,
    label: str,
    metadata: dict[str, Any] | None,
    wait_timeout_seconds: float,
    lease_kind: str = "",
    handoff_deadline_epoch: float | None = None,
    created_at_epoch: float | None = None,
) -> HistoryWriterLease:
    _ensure_lease_process_identity()
    root = Path(root).resolve()
    lease_id = uuid.uuid4().hex
    owner_token = uuid.uuid4().hex
    clean_label = str(label or "history_writer").strip() or "history_writer"
    created_at = time.time() if created_at_epoch is None else float(created_at_epoch)
    payload = {
        "version": 1,
        "lease_id": lease_id,
        "owner_token": owner_token,
        "label": clean_label,
        "pid": os.getpid(),
        "process_start": process_start_marker(os.getpid()),
        "process_instance": _LEASE_PROCESS_INSTANCE,
        "thread_ident": threading.get_ident(),
        "thread_name": threading.current_thread().name,
        "created_at_epoch": created_at,
        "data_dir": str(root),
        "metadata": dict(metadata or {}),
    }
    if lease_kind:
        payload["lease_kind"] = str(lease_kind)
    if handoff_deadline_epoch is not None:
        payload["handoff_deadline_epoch"] = float(handoff_deadline_epoch)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path = history_writer_lease_dir(root) / f"{os.getpid()}.{lease_id}.json"
    fence_key = _history_fence_key(history_reset_fence_path(root))
    registered_under_active_fence = False
    with _ACTIVE_PROCESS_FENCES_CONDITION:
        state = _ACTIVE_PROCESS_FENCES.get(fence_key)
        if state is not None and state.phase == "active":
            _create_lease_file(path, encoded, owner_token)
            registered_under_active_fence = True
    if not registered_under_active_fence:
        with history_writer_fence(
            root,
            label=f"lease_register:{clean_label}",
            wait_timeout_seconds=wait_timeout_seconds,
        ):
            _create_lease_file(path, encoded, owner_token)
    return HistoryWriterLease(
        root=root,
        path=path,
        lease_id=lease_id,
        owner_token=owner_token,
        label=clean_label,
        creator_pid=os.getpid(),
    )


def register_history_writer_lease_if_owned(
    start_dir: str | Path,
    *,
    label: str,
    metadata: dict[str, Any] | None = None,
    wait_timeout_seconds: float = 600.0,
) -> HistoryWriterLease | None:
    root = owned_history_root(start_dir)
    if root is None:
        return None
    return register_history_writer_lease(
        root,
        label=label,
        metadata=metadata,
        wait_timeout_seconds=wait_timeout_seconds,
    )


def register_history_writer_startup_handoff_if_owned(
    start_dir: str | Path,
    *,
    label: str,
    metadata: dict[str, Any] | None = None,
    ttl_seconds: float = 15.0,
    wait_timeout_seconds: float = 600.0,
) -> HistoryWriterStartupHandoff | None:
    """Create a short lease that survives a parent crash during child startup."""

    root = owned_history_root(start_dir)
    if root is None:
        return None
    try:
        requested_ttl = float(ttl_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("history startup handoff TTL must be finite") from exc
    if not math.isfinite(requested_ttl) or requested_ttl <= 0.0:
        raise ValueError("history startup handoff TTL must be finite and positive")
    ttl = min(requested_ttl, _STARTUP_HANDOFF_MAX_TTL_SECONDS)
    created_at = time.time()
    deadline = created_at + ttl
    lease = _register_history_writer_lease(
        root,
        label=label,
        metadata=metadata,
        wait_timeout_seconds=wait_timeout_seconds,
        lease_kind=_STARTUP_HANDOFF_LEASE_KIND,
        handoff_deadline_epoch=deadline,
        created_at_epoch=created_at,
    )
    return HistoryWriterStartupHandoff(
        lease=lease,
        ready_path=lease.path.with_suffix(".ready"),
        deadline_epoch=deadline,
    )


def register_history_writer_leases_if_owned(
    start_dirs: list[str | Path] | tuple[str | Path, ...],
    *,
    label: str,
    metadata: dict[str, Any] | None = None,
    wait_timeout_seconds: float = 600.0,
) -> HistoryWriterLeaseGroup:
    leases: list[HistoryWriterLease] = []
    try:
        for root in owned_history_roots(start_dirs):
            leases.append(
                register_history_writer_lease(
                    root,
                    label=label,
                    metadata=metadata,
                    wait_timeout_seconds=wait_timeout_seconds,
                )
            )
    except BaseException:
        HistoryWriterLeaseGroup(tuple(leases)).release()
        raise
    return HistoryWriterLeaseGroup(tuple(leases))


@contextmanager
def history_writer_lease(
    data_dir: str | Path,
    *,
    label: str,
    metadata: dict[str, Any] | None = None,
    wait_timeout_seconds: float = 600.0,
) -> Iterator[HistoryWriterLease]:
    lease = register_history_writer_lease(
        data_dir,
        label=label,
        metadata=metadata,
        wait_timeout_seconds=wait_timeout_seconds,
    )
    try:
        yield lease
    finally:
        lease.release()


@contextmanager
def history_writer_lease_if_owned(
    start_dir: str | Path,
    *,
    label: str,
    metadata: dict[str, Any] | None = None,
    wait_timeout_seconds: float = 600.0,
) -> Iterator[HistoryWriterLease | None]:
    lease = register_history_writer_lease_if_owned(
        start_dir,
        label=label,
        metadata=metadata,
        wait_timeout_seconds=wait_timeout_seconds,
    )
    try:
        yield lease
    finally:
        if lease is not None:
            lease.release()


@contextmanager
def history_writer_lease_after_startup_handoff_if_owned(
    start_dir: str | Path,
    *,
    label: str,
    metadata: dict[str, Any] | None = None,
    wait_timeout_seconds: float = 600.0,
) -> Iterator[HistoryWriterLease | None]:
    """Atomically adopt a parent handoff before allowing child writes."""

    handoff = _consume_startup_handoff_environment()
    root = owned_history_root(start_dir)
    adopted: _AdoptedStartupHandoff | None = None
    if handoff is not None:
        if root is None:
            raise RuntimeError("history startup handoff has no owned data root")
        adopted = _adopt_history_writer_startup_handoff(
            root,
            handoff,
            label=label,
            metadata=metadata,
            wait_timeout_seconds=wait_timeout_seconds,
        )
        lease = adopted.lease
    elif root is not None:
        lease = register_history_writer_lease(
            root,
            label=label,
            metadata=metadata,
            wait_timeout_seconds=wait_timeout_seconds,
        )
    else:
        lease = None
    try:
        if adopted is not None:
            _wait_for_startup_handoff_parent_acknowledgement(adopted)
        yield lease
    finally:
        if lease is not None:
            lease.release()


@contextmanager
def history_writer_leases_if_owned(
    start_dirs: list[str | Path] | tuple[str | Path, ...],
    *,
    label: str,
    metadata: dict[str, Any] | None = None,
    wait_timeout_seconds: float = 600.0,
) -> Iterator[tuple[HistoryWriterLease, ...]]:
    group = register_history_writer_leases_if_owned(
        start_dirs,
        label=label,
        metadata=metadata,
        wait_timeout_seconds=wait_timeout_seconds,
    )
    try:
        yield group.leases
    finally:
        group.release()


def _consume_startup_handoff_environment() -> _StartupHandoffSpec | None:
    values = {
        key: os.environ.pop(key, None)
        for key in (
            _STARTUP_HANDOFF_FILE_ENV,
            _STARTUP_HANDOFF_TOKEN_ENV,
            _STARTUP_HANDOFF_LEASE_ID_ENV,
        )
    }
    if all(value is None for value in values.values()):
        return None
    if any(not str(value or "").strip() for value in values.values()):
        raise RuntimeError("incomplete history startup handoff environment")
    file_name = str(values[_STARTUP_HANDOFF_FILE_ENV]).strip()
    if (
        Path(file_name).name != file_name
        or file_name in {".", ".."}
        or not file_name.lower().endswith(".json")
        or any(separator in file_name for separator in ("/", "\\", ":"))
    ):
        raise RuntimeError("invalid history startup handoff file name")
    return _StartupHandoffSpec(
        file_name=file_name,
        owner_token=str(values[_STARTUP_HANDOFF_TOKEN_ENV]).strip(),
        lease_id=str(values[_STARTUP_HANDOFF_LEASE_ID_ENV]).strip(),
    )


def _adopt_history_writer_startup_handoff(
    root: Path,
    handoff: _StartupHandoffSpec,
    *,
    label: str,
    metadata: dict[str, Any] | None,
    wait_timeout_seconds: float,
) -> _AdoptedStartupHandoff:
    root = Path(root).resolve()
    handoff_path = history_writer_lease_dir(root) / handoff.file_name
    ready_path = handoff_path.with_suffix(".ready")
    with history_writer_fence(
        root,
        label=f"startup_handoff_adopt:{label}",
        wait_timeout_seconds=wait_timeout_seconds,
    ):
        handoff_payload = _validated_startup_handoff_payload(root, handoff_path, handoff)
        handoff_deadline = float(handoff_payload.get("handoff_deadline_epoch") or 0.0)
        child_metadata = dict(metadata or {})
        child_metadata["startup_handoff_lease_id"] = handoff.lease_id
        lease = _register_history_writer_lease(
            root,
            label=label,
            metadata=child_metadata,
            wait_timeout_seconds=wait_timeout_seconds,
        )
        ready_payload = {
            "version": 1,
            "lease_id": handoff.lease_id,
            "owner_token": handoff.owner_token,
            "pid": os.getpid(),
            "process_start": process_start_marker(os.getpid()),
            "ready_at_epoch": time.time(),
            "expires_at_epoch": handoff_deadline,
            "data_dir": str(root),
            "child_lease_id": lease.lease_id,
        }
        encoded = json.dumps(ready_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        try:
            _create_ready_file(ready_path, encoded)
            if not _unlink_owned_lease(handoff_path, handoff.owner_token):
                raise RuntimeError("history startup handoff ownership changed during adoption")
        except BaseException:
            _unlink_owned_ready_file(ready_path, handoff.owner_token)
            lease.release()
            raise
        return _AdoptedStartupHandoff(
            lease=lease,
            ready_path=ready_path,
            owner_token=handoff.owner_token,
            deadline_epoch=handoff_deadline,
        )


def _wait_for_startup_handoff_parent_acknowledgement(
    adopted: _AdoptedStartupHandoff,
) -> None:
    remaining = max(0.0, adopted.deadline_epoch - time.time())
    deadline = time.monotonic() + remaining
    while True:
        if deadline - time.monotonic() <= 0.0:
            raise RuntimeError("history startup parent acknowledgement timed out")
        path_stat = _safe_lstat(adopted.ready_path)
        if path_stat is None:
            if time.time() > adopted.deadline_epoch or deadline - time.monotonic() <= 0.0:
                raise RuntimeError("history startup parent acknowledgement timed out")
            return
        if _is_reparse_point(path_stat) or not stat.S_ISREG(path_stat.st_mode):
            raise RuntimeError("unsafe history startup acknowledgement file")
        try:
            payload = json.loads(adopted.ready_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("invalid history startup acknowledgement file") from exc
        if (
            not isinstance(payload, dict)
            or str(payload.get("owner_token") or "") != adopted.owner_token
            or str(payload.get("child_lease_id") or "") != adopted.lease.lease_id
        ):
            raise RuntimeError("history startup acknowledgement identity mismatch")
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise RuntimeError("history startup parent acknowledgement timed out")
        time.sleep(min(0.01, remaining))


def _validated_startup_handoff_payload(
    root: Path,
    path: Path,
    handoff: _StartupHandoffSpec,
) -> dict[str, Any]:
    path_stat = _safe_lstat(path)
    if path_stat is None:
        raise RuntimeError("history startup handoff is missing or expired")
    if _is_reparse_point(path_stat) or not stat.S_ISREG(path_stat.st_mode):
        raise RuntimeError("unsafe history startup handoff file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid history startup handoff file") from exc
    if not isinstance(payload, dict) or not _valid_lease_payload(payload):
        raise RuntimeError("invalid history startup handoff payload")
    expected_name = f"{int(payload.get('pid') or 0)}.{handoff.lease_id}.json"
    if (
        str(payload.get("lease_kind") or "") != _STARTUP_HANDOFF_LEASE_KIND
        or str(payload.get("lease_id") or "") != handoff.lease_id
        or str(payload.get("owner_token") or "") != handoff.owner_token
        or path.name != expected_name
        or not str(payload.get("data_dir") or "")
        or _history_fence_key(str(payload.get("data_dir") or "")) != _history_fence_key(root)
    ):
        raise RuntimeError("history startup handoff identity mismatch")
    deadline = float(payload.get("handoff_deadline_epoch") or 0.0)
    if time.time() > deadline:
        raise RuntimeError("history startup handoff is missing or expired")
    return payload


def active_history_writer_leases(data_dir: str | Path) -> list[dict[str, Any]]:
    """List live leases while the caller holds the history reset fence.

    Registration and normal release both take that fence. This makes an empty
    result a stable admission decision for the remainder of the reset critical
    section: no already-running writer can appear after this scan.
    """

    _ensure_lease_process_identity()
    lease_dir = history_writer_lease_dir(data_dir)
    lease_stat = _safe_lstat(lease_dir)
    if lease_stat is None:
        return []
    if _is_reparse_point(lease_stat) or not stat.S_ISDIR(lease_stat.st_mode):
        return [_unsafe_lease_record(lease_dir, "unsafe_history_writer_lease_registry")]

    try:
        paths = sorted(lease_dir.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        return [_unsafe_lease_record(lease_dir, f"lease_registry_unreadable:{type(exc).__name__}")]

    active: list[dict[str, Any]] = []
    for path in paths:
        if path.suffix.lower() == ".ready":
            _reclaim_expired_startup_ready_file(path)
            continue
        if path.suffix.lower() != ".json":
            continue
        path_stat = _safe_lstat(path)
        if path_stat is None:
            continue
        if _is_reparse_point(path_stat) or not stat.S_ISREG(path_stat.st_mode):
            active.append(_unsafe_lease_record(path, "unsafe_history_writer_lease_file"))
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            active.append(_unsafe_lease_record(path, f"invalid_history_writer_lease:{type(exc).__name__}"))
            continue
        if not isinstance(payload, dict) or not _valid_lease_payload(payload):
            active.append(_unsafe_lease_record(path, "invalid_history_writer_lease_payload"))
            continue
        if _lease_payload_is_live(payload):
            active.append(_public_lease_record(path, payload))
            continue
        _unlink_owned_lease(path, str(payload.get("owner_token") or ""))
    return active


def _reclaim_expired_startup_ready_file(path: Path) -> None:
    path_stat = _safe_lstat(path)
    if path_stat is None or _is_reparse_point(path_stat) or not stat.S_ISREG(path_stat.st_mode):
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        expires_at = float(payload.get("expires_at_epoch") or 0.0) if isinstance(payload, dict) else 0.0
        owner_token = str(payload.get("owner_token") or "") if isinstance(payload, dict) else ""
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError):
        return
    if not owner_token or not math.isfinite(expires_at) or time.time() <= expires_at:
        return
    _unlink_owned_ready_file(path, owner_token)


def _ensure_private_lease_dir(path: Path) -> None:
    root = path.parent.parent
    runtime_locks = _ensure_private_runtime_locks_dir(root)
    if path.parent != runtime_locks:
        raise RuntimeError(f"unsafe history writer lease registry location: {path}")
    path_stat = _safe_lstat(path)
    if path_stat is None:
        try:
            path.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            pass
    path_stat = _safe_lstat(path)
    if path_stat is None or _is_reparse_point(path_stat) or not stat.S_ISDIR(path_stat.st_mode):
        raise RuntimeError(f"unsafe history writer lease registry: {path}")
    # Recheck the parent after creation so a component swap does not go
    # unnoticed before the lease file itself is opened.
    _validate_private_directory(root, label="history writer data root")
    _validate_private_directory(runtime_locks, label="history writer runtime lock parent")


def _ensure_private_runtime_locks_dir(root: Path) -> Path:
    root = Path(os.path.abspath(root))
    _validate_private_directory(root, label="history writer data root")
    runtime_locks = root / "runtime_locks"
    runtime_stat = _safe_lstat(runtime_locks)
    if runtime_stat is None:
        try:
            runtime_locks.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            pass
    _validate_private_directory(
        runtime_locks,
        label="history writer runtime lock parent",
    )
    _validate_private_directory(root, label="history writer data root")
    return runtime_locks


def _validate_private_directory(path: Path, *, label: str) -> None:
    path_stat = _safe_lstat(path)
    if (
        path_stat is None
        or _is_reparse_point(path_stat)
        or not stat.S_ISDIR(path_stat.st_mode)
    ):
        raise RuntimeError(f"unsafe {label}: {path}")


def _create_lease_file(path: Path, encoded: bytes, owner_token: str) -> None:
    _write_exclusive_private_file(path, encoded)
    with _ACTIVE_LEASES_GUARD:
        _ACTIVE_LEASE_TOKENS.add(owner_token)


def _create_ready_file(path: Path, encoded: bytes) -> None:
    _write_exclusive_private_file(path, encoded)


def _write_exclusive_private_file(path: Path, encoded: bytes) -> None:
    _ensure_private_lease_dir(path.parent)
    descriptor: int | None = None
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("history writer lease write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _valid_lease_payload(payload: dict[str, Any]) -> bool:
    try:
        created_at_epoch = float(payload.get("created_at_epoch") or 0.0)
        valid = bool(
            str(payload.get("lease_id") or "")
            and str(payload.get("owner_token") or "")
            and str(payload.get("label") or "")
            and int(payload.get("pid") or 0) > 0
            and math.isfinite(created_at_epoch)
            and created_at_epoch >= 0.0
        )
        if not valid:
            return False
        lease_kind = str(payload.get("lease_kind") or "")
        has_handoff_deadline = "handoff_deadline_epoch" in payload
        if not lease_kind:
            return not has_handoff_deadline
        if lease_kind != _STARTUP_HANDOFF_LEASE_KIND or not has_handoff_deadline:
            return False
        deadline = float(payload.get("handoff_deadline_epoch") or 0.0)
        return bool(
            math.isfinite(deadline)
            and created_at_epoch <= deadline
            and deadline - created_at_epoch <= _STARTUP_HANDOFF_MAX_TTL_SECONDS
        )
    except (TypeError, ValueError, OverflowError):
        return False


def _lease_payload_is_live(payload: dict[str, Any]) -> bool:
    if str(payload.get("lease_kind") or "") == _STARTUP_HANDOFF_LEASE_KIND:
        return time.time() <= float(payload.get("handoff_deadline_epoch") or 0.0)
    pid = int(payload.get("pid") or 0)
    token = str(payload.get("owner_token") or "")
    same_instance = (
        pid == os.getpid()
        and str(payload.get("process_instance") or "") == _LEASE_PROCESS_INSTANCE
    )
    if same_instance:
        with _ACTIVE_LEASES_GUARD:
            return token in _ACTIVE_LEASE_TOKENS
    if not process_pid_alive(pid):
        return False
    recorded_start = str(payload.get("process_start") or "")
    current_start = process_start_marker(pid) if recorded_start else ""
    if recorded_start and current_start and recorded_start != current_start:
        return False
    return True


def _public_lease_record(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return {
        "worker": "history_writer_lease",
        "source": "history_writer_lease",
        "label": str(payload.get("label") or ""),
        "pid": int(payload.get("pid") or 0),
        "process_start": str(payload.get("process_start") or ""),
        "thread_name": str(payload.get("thread_name") or ""),
        "lease_id": str(payload.get("lease_id") or ""),
        "lease_path": str(path),
        "created_at_epoch": float(payload.get("created_at_epoch") or 0.0),
        "lease_kind": str(payload.get("lease_kind") or ""),
        "handoff_deadline_epoch": float(payload.get("handoff_deadline_epoch") or 0.0),
        "metadata": metadata,
    }


def _unsafe_lease_record(path: Path, reason: str) -> dict[str, Any]:
    return {
        "worker": "history_writer_lease",
        "source": "history_writer_lease",
        "label": "unknown",
        "pid": 0,
        "lease_path": str(path),
        "reason": reason,
    }


def _unlink_owned_lease(path: Path, owner_token: str) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or str(payload.get("owner_token") or "") != owner_token:
            return False
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def _unlink_owned_ready_file(path: Path, owner_token: str) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or str(payload.get("owner_token") or "") != owner_token:
            return False
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def _safe_lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_reparse_point(path_stat: os.stat_result) -> bool:
    if stat.S_ISLNK(path_stat.st_mode):
        return True
    attributes = int(getattr(path_stat, "st_file_attributes", 0) or 0)
    reparse_attribute = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_attribute)

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


DEFAULT_GPU_MAX_PARALLEL = 1
DEFAULT_LLM_MAX_PARALLEL = 1
DEFAULT_GPU_TIMEOUT_SECONDS = 30 * 60
DEFAULT_LLM_TIMEOUT_SECONDS = 30 * 60


@dataclass(frozen=True)
class ResourceLease:
    resource: str
    slot: int
    reason: str
    waited_seconds: float
    lock_path: str


class ResourceGateTimeout(TimeoutError):
    pass


_LOCAL_STATE_LOCK = threading.Lock()
_LOCAL_SEMAPHORES: dict[tuple[str, Path, int], threading.BoundedSemaphore] = {}
_LOCAL_ACTIVE: dict[str, int] = {}


@contextmanager
def acquire_gpu(
    *,
    reason: str = "",
    max_parallel: int | None = None,
    timeout_seconds: float | None = None,
    root: str | Path | None = None,
) -> Iterator[ResourceLease]:
    """Serialize GPU-heavy local workers across threads and processes.

    OCR and ASR run as subprocesses, so a thread lock alone is not enough. This
    gate combines an in-process semaphore with per-slot file locks. Defaulting
    to one slot is deliberately conservative for desktop GPU stability.
    """

    with _acquire_resource(
        "gpu",
        reason=reason,
        max_parallel=_effective_gpu_max_parallel(max_parallel),
        timeout_seconds=timeout_seconds if timeout_seconds is not None else DEFAULT_GPU_TIMEOUT_SECONDS,
        root=root,
    ) as lease:
        yield lease


@contextmanager
def acquire_llm(
    *,
    workload: str = "interactive",
    reason: str = "",
    max_parallel: int | None = None,
    total_max_parallel: int | None = None,
    timeout_seconds: float | None = None,
    root: str | Path | None = None,
) -> Iterator[ResourceLease]:
    """Throttle LLM calls by foreground/background budget.

    The key pool still enforces each API key's own concurrency. This gate adds a
    central budget above it so background file analysis and history backfill
    cannot consume every model slot while foreground conversations are waiting.
    """

    resource = _llm_resource_name(workload)
    effective_timeout = timeout_seconds if timeout_seconds is not None else DEFAULT_LLM_TIMEOUT_SECONDS
    if total_max_parallel is None:
        with _acquire_resource(
            resource,
            reason=reason,
            max_parallel=_effective_llm_max_parallel(max_parallel),
            timeout_seconds=effective_timeout,
            root=root,
        ) as lease:
            yield lease
        return
    start = time.monotonic()
    with _acquire_resource(
        "llm_total",
        reason=reason,
        max_parallel=_effective_llm_max_parallel(total_max_parallel),
        timeout_seconds=effective_timeout,
        root=root,
    ):
        with _acquire_resource(
            resource,
            reason=reason,
            max_parallel=_effective_llm_max_parallel(max_parallel),
            timeout_seconds=_remaining_timeout(start, effective_timeout),
            root=root,
        ) as lease:
            yield lease


def gpu_gate_snapshot(*, root: str | Path | None = None, max_parallel: int | None = None) -> dict[str, object]:
    lock_root = _lock_root(root)
    slots = _effective_gpu_max_parallel(max_parallel)
    with _LOCAL_STATE_LOCK:
        active = int(_LOCAL_ACTIVE.get("gpu", 0))
    slot_activity = _resource_slot_activity(
        "gpu",
        lock_root=lock_root,
        max_parallel=slots,
        create_missing=False,
    )
    return {
        "resource": "gpu",
        "max_parallel": slots,
        "active_in_process": active,
        "active_slots": sum(1 for item in slot_activity if item.get("locked")),
        "slots": slot_activity,
        "lock_root": str(lock_root),
        "timeout_seconds": DEFAULT_GPU_TIMEOUT_SECONDS,
        "policy": "OCR/ASR 只有在显式选择 GPU 档时才进入此队列；默认只允许 1 个 GPU 任务并行，GPU 被占用时排队等待。",
    }


def llm_gate_snapshot(
    *,
    root: str | Path | None = None,
    total_max: int | None = None,
    interactive_max: int | None = None,
    background_max: int | None = None,
) -> dict[str, object]:
    lock_root = _lock_root(root)
    total_slots = _effective_llm_max_parallel(total_max) if total_max is not None else None
    interactive_slots = _effective_llm_max_parallel(interactive_max)
    background_slots = _effective_llm_max_parallel(background_max)
    snapshot = {
        "resource": "llm",
        "lock_root": str(lock_root),
        "timeout_seconds": DEFAULT_LLM_TIMEOUT_SECONDS,
        "policy": "LLM calls first acquire the total model budget, then a foreground/background pool slot; each request still respects per-key concurrency.",
        "interactive": _llm_pool_snapshot("llm_interactive", lock_root, interactive_slots),
        "background": _llm_pool_snapshot("llm_background", lock_root, background_slots),
    }
    if total_slots is not None:
        snapshot["total"] = _llm_pool_snapshot("llm_total", lock_root, total_slots)
    return snapshot


@contextmanager
def _acquire_resource(
    resource: str,
    *,
    reason: str,
    max_parallel: int,
    timeout_seconds: float,
    root: str | Path | None,
) -> Iterator[ResourceLease]:
    lock_root = _lock_root(root)
    lock_root.mkdir(parents=True, exist_ok=True)
    semaphore = _local_semaphore(resource, lock_root, max_parallel)
    start = time.monotonic()
    if not semaphore.acquire(timeout=max(0.0, timeout_seconds)):
        raise ResourceGateTimeout(f"{resource} gate timed out before local slot was available")
    try:
        slot_start = time.monotonic()
        remaining = _remaining_timeout(start, timeout_seconds)
        lease, handle = _acquire_slot_file(
            resource,
            lock_root=lock_root,
            max_parallel=max_parallel,
            timeout_seconds=remaining,
            reason=reason,
            start=slot_start,
            lease_start=start,
        )
        with _LOCAL_STATE_LOCK:
            _LOCAL_ACTIVE[resource] = int(_LOCAL_ACTIVE.get(resource, 0)) + 1
        try:
            yield lease
        finally:
            with _LOCAL_STATE_LOCK:
                _LOCAL_ACTIVE[resource] = max(0, int(_LOCAL_ACTIVE.get(resource, 0)) - 1)
            _release_slot_file(handle)
    finally:
        semaphore.release()


def _effective_gpu_max_parallel(value: int | None) -> int:
    raw = value if value is not None else os.environ.get("CHATBOT_GPU_MAX_CONCURRENT", "")
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = DEFAULT_GPU_MAX_PARALLEL
    return max(1, min(8, parsed))


def _effective_llm_max_parallel(value: int | None) -> int:
    raw = value if value is not None else os.environ.get("CHATBOT_LLM_MAX_CONCURRENT", "")
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = DEFAULT_LLM_MAX_PARALLEL
    return max(1, min(128, parsed))


def _llm_resource_name(workload: str) -> str:
    text = str(workload or "").strip().lower()
    if text in {"background", "context_only", "backfill", "history", "analysis", "file_analysis", "memory"}:
        return "llm_background"
    return "llm_interactive"


def _llm_pool_snapshot(resource: str, lock_root: Path, max_parallel: int) -> dict[str, object]:
    with _LOCAL_STATE_LOCK:
        active = int(_LOCAL_ACTIVE.get(resource, 0))
    slot_activity = _resource_slot_activity(resource, lock_root=lock_root, max_parallel=max_parallel)
    return {
        "resource": resource,
        "max_parallel": max_parallel,
        "active_in_process": active,
        "active_slots": sum(1 for item in slot_activity if item.get("locked")),
        "slots": slot_activity,
    }


def _lock_root(root: str | Path | None) -> Path:
    if root is not None:
        return Path(root).resolve()
    configured = os.environ.get("CHATBOT_GPU_GATE_DIR", "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[3] / "data" / "runtime_locks"


def _local_semaphore(resource: str, lock_root: Path, max_parallel: int) -> threading.BoundedSemaphore:
    key = (resource, lock_root, max_parallel)
    with _LOCAL_STATE_LOCK:
        semaphore = _LOCAL_SEMAPHORES.get(key)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(max_parallel)
            _LOCAL_SEMAPHORES[key] = semaphore
        return semaphore


def _acquire_slot_file(
    resource: str,
    *,
    lock_root: Path,
    max_parallel: int,
    timeout_seconds: float,
    reason: str,
    start: float,
    lease_start: float,
) -> tuple[ResourceLease, object]:
    while True:
        for slot in range(max_parallel):
            path = lock_root / f"{resource}.{slot}.lock"
            handle = path.open("a+b")
            if _try_lock_handle(handle):
                waited = time.monotonic() - lease_start
                try:
                    handle.seek(0)
                    handle.truncate()
                    handle.write(f"pid={os.getpid()} reason={reason} acquired_at={time.time():.3f}\n".encode("utf-8"))
                    handle.flush()
                except OSError:
                    pass
                return ResourceLease(resource, slot, reason, waited, str(path)), handle
            handle.close()
        if time.monotonic() - start >= timeout_seconds:
            raise ResourceGateTimeout(f"{resource} gate timed out waiting for cross-process slot")
        time.sleep(0.1)


def _resource_slot_activity(
    resource: str,
    *,
    lock_root: Path,
    max_parallel: int,
    create_missing: bool = True,
) -> list[dict[str, object]]:
    activity: list[dict[str, object]] = []
    if create_missing:
        lock_root.mkdir(parents=True, exist_ok=True)
    for slot in range(max_parallel):
        path = lock_root / f"{resource}.{slot}.lock"
        if not create_missing and not path.exists():
            activity.append({"slot": slot, "locked": False, "owner": "", "lock_path": str(path)})
            continue
        try:
            handle = path.open("a+b" if create_missing else "r+b")
        except FileNotFoundError:
            activity.append({"slot": slot, "locked": False, "owner": "", "lock_path": str(path)})
            continue
        except OSError:
            activity.append({"slot": slot, "locked": True, "owner": "", "lock_path": str(path)})
            continue
        owner = ""
        locked = False
        try:
            if _try_lock_handle(handle):
                _release_slot_file(handle)
                handle = None
            else:
                locked = True
                try:
                    handle.seek(0)
                    owner = handle.read(300).decode("utf-8", errors="replace").strip()
                except OSError:
                    owner = ""
        finally:
            if handle is not None:
                handle.close()
        activity.append({"slot": slot, "locked": locked, "owner": owner, "lock_path": str(path)})
    return activity


def _try_lock_handle(handle: object) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_slot_file(handle: object) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _remaining_timeout(start: float, timeout_seconds: float) -> float:
    elapsed = time.monotonic() - start
    return max(0.0, timeout_seconds - elapsed)

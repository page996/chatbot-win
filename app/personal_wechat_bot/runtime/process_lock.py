from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class ProcessLockError(RuntimeError):
    """Raised when a single-instance process lock is already held elsewhere."""

    def __init__(self, message: str, *, holder: dict[str, Any] | None = None):
        super().__init__(message)
        self.holder = holder or {}


class ProcessLock:
    """A long-held, heartbeat-based single-instance lock backed by one file.

    Unlike the short per-operation locks in ``jsonl_bus`` / ``hook_events``
    (held only for the duration of a single append/import), this lock is meant
    to be held for the entire lifetime of a long-running consumer (e.g. a hook
    pull runner). It prevents two runners from consuming the same hook JSONL and
    racing the shared import offset.

    The lock file stores the owner PID, a label, and a heartbeat timestamp. A
    lock whose heartbeat is older than ``stale_after_seconds`` is considered
    abandoned (crashed process) and can be taken over. Callers should refresh
    the heartbeat periodically via :meth:`heartbeat` during their loop.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        label: str = "",
        stale_after_seconds: float = 60.0,
    ):
        self.path = Path(path)
        self.label = label
        self.stale_after_seconds = max(1.0, float(stale_after_seconds))
        self._fd: int | None = None
        self._acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            holder = self._read_holder()
            if not self._holder_is_stale(holder):
                raise ProcessLockError(
                    f"hook consumer already running (held by {self._describe(holder)}); "
                    f"stop it before starting another, or delete the stale lock: {self.path}",
                    holder=holder,
                )
            # Stale lock from a crashed/exited process: take it over.
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                # Another process won the race to take over the stale lock.
                holder = self._read_holder()
                raise ProcessLockError(
                    f"hook consumer already running (held by {self._describe(holder)})",
                    holder=holder,
                )
        self._acquired = True
        self._write_payload()

    def try_acquire(self) -> bool:
        """Attempt to acquire without raising. Returns True on success.

        Unlike :meth:`acquire`, a live (non-stale) holder yields ``False``
        instead of :class:`ProcessLockError`, so callers can poll/back off and
        retry — used by :func:`blocking_process_lock` to wait for a cross-process
        consume holder rather than failing fast.
        """

        try:
            self.acquire()
        except (ProcessLockError, PermissionError):
            return False
        return True

    def heartbeat(self) -> None:
        if not self._acquired:
            return
        self._write_payload()

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self._acquired:
            self._unlink_with_retry()
            self._acquired = False

    def _unlink_with_retry(self, *, attempts: int = 20, delay_seconds: float = 0.02) -> None:
        # On Windows the lock file can transiently be un-deletable if a scanner
        # (AV / search indexer) or a racing acquirer has it briefly open. Retry
        # for a short window so the file is actually removed and does not get
        # orphaned with a fresh heartbeat (which would block waiters until it
        # goes stale). If it still fails, leave it: the next acquirer takes it
        # over once its heartbeat ages out.
        for attempt in range(max(1, attempts)):
            try:
                self.path.unlink()
                return
            except FileNotFoundError:
                return
            except OSError:
                if attempt >= attempts - 1:
                    return
                time.sleep(delay_seconds)

    def _write_payload(self) -> None:
        if self._fd is None:
            return
        payload = {
            "pid": os.getpid(),
            "label": self.label,
            "acquired_at": getattr(self, "_acquired_at", None) or time.time(),
            "heartbeat_at": time.time(),
        }
        self._acquired_at = payload["acquired_at"]
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.ftruncate(self._fd, 0)
            os.write(self._fd, data)
            os.fsync(self._fd)
        except OSError:
            pass

    def _read_holder(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _holder_is_stale(self, holder: dict[str, Any]) -> bool:
        heartbeat = holder.get("heartbeat_at")
        if not isinstance(heartbeat, (int, float)):
            # Unparseable/legacy lock file: treat as stale so we can recover.
            return True
        if time.time() - float(heartbeat) <= self.stale_after_seconds:
            return False
        # Heartbeat is old. If the recorded PID is our own, it is definitely safe.
        pid = holder.get("pid")
        if isinstance(pid, int) and pid == os.getpid():
            return True
        return True

    @staticmethod
    def _describe(holder: dict[str, Any]) -> str:
        pid = holder.get("pid", "?")
        label = holder.get("label", "")
        return f"pid={pid} label={label}".strip()


@contextmanager
def process_lock(
    path: str | Path,
    *,
    label: str = "",
    stale_after_seconds: float = 60.0,
    enabled: bool = True,
) -> Iterator[ProcessLock | None]:
    """Acquire a single-instance process lock for the duration of the block.

    When ``enabled`` is False, yields None and performs no locking (useful for
    tests or one-shot commands that never race).
    """

    if not enabled:
        yield None
        return
    lock = ProcessLock(path, label=label, stale_after_seconds=stale_after_seconds)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


@contextmanager
def blocking_process_lock(
    path: str | Path,
    *,
    label: str = "",
    stale_after_seconds: float = 60.0,
    wait_timeout_seconds: float = 120.0,
    poll_interval_seconds: float = 0.05,
    enabled: bool = True,
) -> Iterator[ProcessLock | None]:
    """Acquire a short-lived cross-process lock, waiting for a live holder.

    Unlike :func:`process_lock` (which fails fast when another instance holds
    the lock, for long-lived *loop ownership*), this waits up to
    ``wait_timeout_seconds`` for the current holder to release. It is meant for
    the *per-operation* consume step: any number of consumers may take turns,
    but never overlap. A crashed holder's lock still goes stale and is taken
    over via :meth:`ProcessLock.try_acquire`.

    When ``enabled`` is False, yields None and performs no locking.
    """

    if not enabled:
        yield None
        return
    lock = ProcessLock(path, label=label, stale_after_seconds=stale_after_seconds)
    deadline = time.monotonic() + max(0.0, wait_timeout_seconds)
    interval = max(0.005, poll_interval_seconds)
    while not lock.try_acquire():
        if time.monotonic() >= deadline:
            holder = lock._read_holder()
            raise ProcessLockError(
                f"timed out after {wait_timeout_seconds}s waiting for consume lock "
                f"(held by {ProcessLock._describe(holder)}): {lock.path}",
                holder=holder,
            )
        time.sleep(interval)
    try:
        yield lock
    finally:
        lock.release()

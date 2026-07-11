from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class ProcessLockError(RuntimeError):
    """Raised when a single-instance process lock is already held elsewhere."""

    def __init__(self, message: str, *, holder: dict[str, Any] | None = None):
        super().__init__(message)
        self.holder = holder or {}


def process_pid_alive(pid: int) -> bool:
    """Return whether a recorded lock owner PID is still alive."""

    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
        except (ImportError, OSError):
            return True
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except OSError:
            return True
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        process_query_limited_information = 0x1000
        try:
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        except OSError:
            return True
        if not handle:
            # Only errors that explicitly mean "no such process" prove death.
            # Access/resource/transient failures must fail closed so a live
            # protected owner is never fenced out.
            try:
                error_code = int(ctypes.get_last_error())
            except OSError:
                return True
            return error_code not in {87, 1168}
        try:
            wait_result = int(kernel32.WaitForSingleObject(handle, 0))
            if wait_result == 0:  # WAIT_OBJECT_0: process exited
                return False
            return True  # WAIT_TIMEOUT or WAIT_FAILED both preserve ownership
        except OSError:
            return True
        finally:
            try:
                kernel32.CloseHandle(handle)
            except OSError:
                pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Only ProcessLookupError proves that the PID is gone. Other failures
        # preserve the recorded owner to avoid fencing out a live process.
        return True
    return True


def process_start_marker(pid: int) -> str:
    """Return a stable OS process-start marker when the platform exposes one."""

    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid <= 0:
        return ""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
        except (ImportError, OSError):
            return ""

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except OSError:
            return ""
        filetime_pointer = ctypes.POINTER(wintypes.FILETIME)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            filetime_pointer,
            filetime_pointer,
            filetime_pointer,
            filetime_pointer,
        )
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        try:
            handle = kernel32.OpenProcess(0x1000, False, pid)
        except OSError:
            return ""
        if not handle:
            return ""
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            try:
                ok = kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                )
            except OSError:
                return ""
            if not ok:
                return ""
            return f"win:{(int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)}"
        finally:
            try:
                kernel32.CloseHandle(handle)
            except OSError:
                pass
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2 :].split()
        return f"proc:{fields[19]}" if len(fields) > 19 else ""
    except (OSError, ValueError):
        return ""


_PROCESS_IDENTITY_PID = os.getpid()
_PROCESS_INSTANCE_TOKEN = uuid.uuid4().hex
_PROCESS_START_MARKER = process_start_marker(_PROCESS_IDENTITY_PID)
_ACTIVE_SHORT_LOCK_TOKENS: set[str] = set()
_ACTIVE_SHORT_LOCKS_GUARD = threading.Lock()
_ACTIVE_LONG_LOCK_TOKENS: set[str] = set()
_ACTIVE_LONG_LOCKS_GUARD = threading.Lock()


def _reset_process_identity() -> None:
    global _PROCESS_IDENTITY_PID
    global _PROCESS_INSTANCE_TOKEN
    global _PROCESS_START_MARKER
    global _ACTIVE_SHORT_LOCK_TOKENS
    global _ACTIVE_SHORT_LOCKS_GUARD
    global _ACTIVE_LONG_LOCK_TOKENS
    global _ACTIVE_LONG_LOCKS_GUARD

    _PROCESS_IDENTITY_PID = os.getpid()
    _PROCESS_INSTANCE_TOKEN = uuid.uuid4().hex
    _PROCESS_START_MARKER = process_start_marker(_PROCESS_IDENTITY_PID)
    _ACTIVE_SHORT_LOCK_TOKENS = set()
    _ACTIVE_SHORT_LOCKS_GUARD = threading.Lock()
    _ACTIVE_LONG_LOCK_TOKENS = set()
    _ACTIVE_LONG_LOCKS_GUARD = threading.Lock()


def _ensure_process_identity() -> None:
    if _PROCESS_IDENTITY_PID != os.getpid():
        _reset_process_identity()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_process_identity)


def scoped_process_lock_path(data_dir: str | Path, scope: str, identity: str) -> Path:
    digest = hashlib.sha256(
        str(identity or "").encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:24]
    safe_scope = "".join(
        character
        for character in str(scope or "scope")
        if character.isascii() and (character.isalnum() or character in "-_")
    )
    return (
        Path(data_dir)
        / "runtime_locks"
        / "scoped"
        / f"{safe_scope or 'scope'}.{digest}.lock"
    )


@contextmanager
def _process_lock_mutation_guard(
    path: str | Path,
    *,
    deadline: float | None = None,
) -> Iterator[Path]:
    """Serialize ownership-file replacement without deleting the guard file."""

    lock_path = Path(path)
    guard_path = Path(f"{lock_path}.guard")
    deadline = time.monotonic() + 10.0 if deadline is None else float(deadline)
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(guard_path), os.O_CREAT | os.O_RDWR, 0o600)
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out opening process-lock mutation guard: {guard_path}"
                ) from exc
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    if fd is None:  # pragma: no cover - defensive; the loop returns or raises
        raise OSError(f"unable to open process-lock mutation guard: {guard_path}")
    locked = False
    try:
        while not locked:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                if os.name != "nt" and exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for process-lock mutation guard: {guard_path}") from exc
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        yield guard_path
    finally:
        if locked:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


def _sync_released_lock_file(path: Path, owner_token: str) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if (
        not isinstance(payload, dict)
        or payload.get("owner_token") != owner_token
        or not payload.get("released_at")
    ):
        return False
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return False
    synced = False
    try:
        os.fsync(fd)
        synced = True
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return synced


def pid_lock_file_is_stale(
    path: str | Path,
    *,
    max_age_seconds: float,
) -> bool:
    """Return whether an aged short-operation lock can be reclaimed.

    Short-operation locks store their owning PID as plain text. Age alone is
    not sufficient evidence that such a lock is abandoned: a large import or
    backfill can legitimately outlive the normal stale threshold. Preserve an
    aged lock while its recorded process is alive, while still recovering
    legacy, malformed, or dead-owner locks.
    """

    _ensure_process_identity()
    lock_path = Path(path)
    try:
        age_seconds = time.time() - lock_path.stat().st_mtime
    except OSError:
        return False
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        # A racing owner or scanner may make the file briefly unreadable. It is
        # safer to wait and retry than to treat that as proof of abandonment.
        return False

    payload: dict[str, Any]
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        decoded = None
    if isinstance(decoded, dict):
        payload = decoded
    else:
        try:
            payload = {"pid": int(raw), "legacy": True}
        except (TypeError, ValueError):
            payload = {"malformed": True}

    if payload.get("released_at"):
        return True
    try:
        pid = int(payload.get("pid", 0) or 0)
    except (TypeError, ValueError):
        pid = 0
    token = str(payload.get("owner_token") or "")
    same_instance = (
        pid == os.getpid()
        and str(payload.get("process_instance") or "") == _PROCESS_INSTANCE_TOKEN
    )
    if token and same_instance:
        with _ACTIVE_SHORT_LOCKS_GUARD:
            if token not in _ACTIVE_SHORT_LOCK_TOKENS:
                return True
    if age_seconds <= max(0.0, float(max_age_seconds)):
        return False
    if payload.get("malformed"):
        return True
    if payload.get("legacy") and pid == os.getpid():
        return True
    if not process_pid_alive(pid):
        return True
    recorded_start = str(payload.get("process_start") or "")
    current_start = process_start_marker(pid) if recorded_start else ""
    return bool(recorded_start and current_start and recorded_start != current_start)


class ShortProcessLock:
    """PID-aware short-operation lock with explicit ownership and release."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout_seconds: float,
        stale_after_seconds: float,
        timeout_label: str = "short process lock",
        poll_interval_seconds: float = 0.025,
    ) -> None:
        self.path = Path(path)
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.stale_after_seconds = max(0.1, float(stale_after_seconds))
        self.timeout_label = str(timeout_label or "short process lock")
        self.poll_interval_seconds = max(0.005, float(poll_interval_seconds))
        self.owner_token = uuid.uuid4().hex
        self.acquired_at = 0.0
        self._fd: int | None = None
        self._creator_pid = os.getpid()

    def acquire(self) -> None:
        _ensure_process_identity()
        if self._creator_pid != os.getpid():
            raise RuntimeError("short process lock cannot be reused after fork")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        while self._fd is None:
            with _process_lock_mutation_guard(self.path, deadline=deadline):
                fd: int | None = None
                try:
                    fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except PermissionError:
                    if not self.path.exists():
                        raise
                    if pid_lock_file_is_stale(self.path, max_age_seconds=self.stale_after_seconds):
                        try:
                            self.path.unlink()
                        except OSError:
                            pass
                        else:
                            try:
                                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                            except FileExistsError:
                                fd = None
                except FileExistsError:
                    if pid_lock_file_is_stale(self.path, max_age_seconds=self.stale_after_seconds):
                        try:
                            self.path.unlink()
                        except OSError:
                            pass
                        else:
                            try:
                                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                            except FileExistsError:
                                fd = None

                if fd is not None:
                    self.owner_token = uuid.uuid4().hex
                    self._fd = fd
                    self.acquired_at = time.time()
                    with _ACTIVE_SHORT_LOCKS_GUARD:
                        _ACTIVE_SHORT_LOCK_TOKENS.add(self.owner_token)
                    try:
                        self._write_payload()
                    except Exception:
                        try:
                            self._write_payload(released_at=time.time())
                        except Exception:
                            pass
                        self._close_fd()
                        with _ACTIVE_SHORT_LOCKS_GUARD:
                            _ACTIVE_SHORT_LOCK_TOKENS.discard(self.owner_token)
                        try:
                            self.path.unlink()
                        except OSError:
                            pass
                        raise

            if self._fd is not None:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for {self.timeout_label}: {self.path}")
            time.sleep(self.poll_interval_seconds)

    def release(self) -> None:
        if self._fd is None:
            return
        if self._creator_pid != os.getpid():
            self._close_fd()
            return
        try:
            with _process_lock_mutation_guard(self.path, deadline=time.monotonic() + 2.0):
                self._release_under_mutation_guard()
        except OSError:
            # If the sibling guard itself is unavailable, relinquish our file
            # descriptor but never perform an unguarded token-check/unlink.
            try:
                self._write_payload(released_at=time.time())
            except Exception:
                pass
            try:
                self._close_fd()
            except OSError:
                pass
            with _ACTIVE_SHORT_LOCKS_GUARD:
                _ACTIVE_SHORT_LOCK_TOKENS.discard(self.owner_token)

    def _release_under_mutation_guard(self) -> None:
        marker_written = False
        try:
            self._write_payload(released_at=time.time(), sync=False)
            marker_written = True
        except OSError:
            pass
        try:
            self._close_fd()
        except OSError:
            pass
        with _ACTIVE_SHORT_LOCKS_GUARD:
            _ACTIVE_SHORT_LOCK_TOKENS.discard(self.owner_token)
        if not self._unlink_owned_with_retry():
            if marker_written:
                _sync_released_lock_file(self.path, self.owner_token)
            else:
                with _ACTIVE_SHORT_LOCKS_GUARD:
                    _ACTIVE_SHORT_LOCK_TOKENS.add(self.owner_token)

    def _write_payload(
        self,
        *,
        released_at: float | None = None,
        sync: bool = True,
    ) -> None:
        if self._fd is None:
            return
        payload: dict[str, Any] = {
            "version": 1,
            "pid": os.getpid(),
            "process_start": _PROCESS_START_MARKER,
            "process_instance": _PROCESS_INSTANCE_TOKEN,
            "owner_token": self.owner_token,
            "acquired_at": self.acquired_at,
        }
        if released_at is not None:
            payload["released_at"] = released_at
        data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.ftruncate(self._fd, 0)
        view = memoryview(data)
        while view:
            written = os.write(self._fd, view)
            if written <= 0:
                raise OSError("short lock payload write made no progress")
            view = view[written:]
        if sync:
            os.fsync(self._fd)

    def _close_fd(self) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        finally:
            self._fd = None

    def _unlink_owned_with_retry(self, *, attempts: int = 20, delay_seconds: float = 0.02) -> bool:
        for attempt in range(max(1, attempts)):
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or payload.get("owner_token") != self.owner_token:
                    return True
                self.path.unlink()
                return True
            except FileNotFoundError:
                return True
            except (OSError, json.JSONDecodeError, UnicodeError):
                if attempt >= attempts - 1:
                    return False
                time.sleep(delay_seconds)
        return False


@contextmanager
def short_process_lock(
    path: str | Path,
    *,
    timeout_seconds: float,
    stale_after_seconds: float,
    timeout_label: str = "short process lock",
) -> Iterator[ShortProcessLock]:
    lock = ShortProcessLock(
        path,
        timeout_seconds=timeout_seconds,
        stale_after_seconds=stale_after_seconds,
        timeout_label=timeout_label,
    )
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


class ProcessLock:
    """A long-held, heartbeat-based single-instance lock backed by one file.

    Unlike the short per-operation locks in ``jsonl_bus`` / ``hook_events``
    (held only for the duration of a single append/import), this lock is meant
    to be held for the entire lifetime of a long-running consumer (e.g. a hook
    pull runner). It prevents two runners from consuming the same hook JSONL and
    racing the shared import offset.

    The lock file stores an owner token plus the PID and OS process-start
    marker. A stale heartbeat alone cannot fence a live owner, while a persistent
    sibling guard serializes ownership-file replacement. Dead owners, reused
    PIDs, and explicitly released owners can be recovered safely; malformed
    files require review.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        label: str = "",
        stale_after_seconds: float = 60.0,
        metadata: dict[str, Any] | None = None,
    ):
        self.path = Path(path)
        self.label = label
        self.stale_after_seconds = max(1.0, float(stale_after_seconds))
        self.metadata = dict(metadata or {})
        self.owner_token = uuid.uuid4().hex
        self._fd: int | None = None
        self._acquired = False
        self._acquired_at = 0.0
        self._io_guard = threading.Lock()
        self._creator_pid = os.getpid()

    def acquire(self, *, mutation_deadline: float | None = None) -> None:
        _ensure_process_identity()
        if self._creator_pid != os.getpid():
            raise RuntimeError("process lock cannot be reused after fork")
        if self._acquired:
            return
        self._validate_metadata(self.metadata)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 10.0 if mutation_deadline is None else float(mutation_deadline)
        with _process_lock_mutation_guard(self.path, deadline=deadline):
            self._acquire_under_mutation_guard()

    def _acquire_under_mutation_guard(self) -> None:
        try:
            self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except (FileExistsError, PermissionError) as exc:
            if isinstance(exc, PermissionError) and not self.path.exists():
                raise
            holder = self._read_holder()
            if not self._holder_is_stale(holder):
                raise ProcessLockError(
                    f"hook consumer already running (held by {self._describe(holder)}); "
                    f"stop it before starting another, or delete the stale lock: {self.path}",
                    holder=holder,
                )
            # Remove only the exact stale owner we inspected. A replacement
            # may have won the race after our first read.
            if not self._unlink_holder_if_unchanged(holder):
                holder = self._read_holder()
                raise ProcessLockError(
                    f"hook consumer already running (held by {self._describe(holder)})",
                    holder=holder,
                )
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except (FileExistsError, PermissionError) as retry_exc:
                if isinstance(retry_exc, PermissionError) and not self.path.exists():
                    raise
                # Another process won the race to take over the stale lock.
                holder = self._read_holder()
                raise ProcessLockError(
                    f"hook consumer already running (held by {self._describe(holder)})",
                    holder=holder,
                )
        self.owner_token = uuid.uuid4().hex
        self._acquired_at = time.time()
        with _ACTIVE_LONG_LOCKS_GUARD:
            _ACTIVE_LONG_LOCK_TOKENS.add(self.owner_token)
        try:
            self._write_payload()
        except Exception:
            try:
                self._write_payload(released_at=time.time())
            except Exception:
                pass
            self._close_fd()
            with _ACTIVE_LONG_LOCKS_GUARD:
                _ACTIVE_LONG_LOCK_TOKENS.discard(self.owner_token)
            try:
                self.path.unlink()
            except OSError:
                pass
            raise
        self._acquired = True

    def try_acquire(self, *, mutation_deadline: float | None = None) -> bool:
        """Attempt to acquire without raising. Returns True on success.

        Unlike :meth:`acquire`, a live (non-stale) holder yields ``False``
        instead of :class:`ProcessLockError`, so callers can poll/back off and
        retry — used by :func:`blocking_process_lock` to wait for a cross-process
        consume holder rather than failing fast.
        """

        try:
            self.acquire(mutation_deadline=mutation_deadline)
        except ProcessLockError:
            return False
        return True

    def heartbeat(self) -> None:
        if self._creator_pid != os.getpid():
            return
        if not self._acquired:
            return
        with self._io_guard:
            self._write_payload()

    def update_metadata(self, metadata: dict[str, Any] | None) -> None:
        """Merge metadata into future heartbeat payloads."""

        with self._io_guard:
            previous = self.metadata
            candidate = dict(previous)
            if isinstance(metadata, dict):
                candidate.update(metadata)
            self._validate_metadata(candidate)
            self.metadata = candidate
            try:
                if self._acquired:
                    self._write_payload()
            except BaseException:
                self.metadata = previous
                raise

    def release(self) -> None:
        if self._creator_pid != os.getpid():
            self._close_fd()
            self._acquired = False
            return
        with self._io_guard:
            if not self._acquired and self._fd is None:
                return
            try:
                with _process_lock_mutation_guard(self.path, deadline=time.monotonic() + 2.0):
                    self._release_under_mutation_guard()
            except OSError:
                # Never fall back to an unguarded token-check/unlink. A durable
                # released marker lets a future guarded acquirer recover.
                try:
                    self._write_payload(released_at=time.time())
                except Exception:
                    pass
                self._close_fd()
                self._acquired = False
                with _ACTIVE_LONG_LOCKS_GUARD:
                    _ACTIVE_LONG_LOCK_TOKENS.discard(self.owner_token)

    def _release_under_mutation_guard(self) -> None:
        marker_written = False
        try:
            self._write_payload(released_at=time.time(), sync=False)
            marker_written = True
        except OSError:
            pass
        self._close_fd()
        self._acquired = False
        with _ACTIVE_LONG_LOCKS_GUARD:
            _ACTIVE_LONG_LOCK_TOKENS.discard(self.owner_token)
        if not self._unlink_owned_with_retry():
            if marker_written:
                _sync_released_lock_file(self.path, self.owner_token)
            else:
                with _ACTIVE_LONG_LOCKS_GUARD:
                    _ACTIVE_LONG_LOCK_TOKENS.add(self.owner_token)

    @staticmethod
    def _validate_metadata(metadata: dict[str, Any]) -> None:
        json.dumps(metadata, ensure_ascii=False)

    def _unlink_owned_with_retry(self, *, attempts: int = 20, delay_seconds: float = 0.02) -> bool:
        # On Windows the lock file can transiently be un-deletable if a scanner
        # (AV / search indexer) or a racing acquirer has it briefly open. Retry
        # for a short window so the file is actually removed and does not get
        # orphaned. If deletion still fails, the durable released marker lets
        # the next guarded acquirer reclaim it immediately.
        for attempt in range(max(1, attempts)):
            try:
                holder = self._read_holder()
                if str(holder.get("owner_token") or "") != self.owner_token:
                    return True
                self.path.unlink()
                return True
            except FileNotFoundError:
                return True
            except OSError:
                if attempt >= attempts - 1:
                    return False
                time.sleep(delay_seconds)
        return False

    def _close_fd(self) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        finally:
            self._fd = None

    def _write_payload(
        self,
        *,
        released_at: float | None = None,
        sync: bool = True,
    ) -> None:
        if self._fd is None:
            return
        reserved = {
            "version",
            "pid",
            "process_start",
            "process_instance",
            "owner_token",
            "acquired_at",
            "heartbeat_at",
            "released_at",
        }
        payload = {key: value for key, value in self.metadata.items() if key not in reserved}
        payload.update({
            "version": 2,
            "pid": os.getpid(),
            "process_start": _PROCESS_START_MARKER,
            "process_instance": _PROCESS_INSTANCE_TOKEN,
            "owner_token": self.owner_token,
            "label": self.label,
            "acquired_at": self._acquired_at or time.time(),
            "heartbeat_at": 0.0 if released_at is not None else time.time(),
        })
        if released_at is not None:
            payload["released_at"] = released_at
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        os.lseek(self._fd, 0, os.SEEK_SET)
        view = memoryview(data)
        while view:
            written = os.write(self._fd, view)
            if written <= 0:
                raise OSError("process lock payload write made no progress")
            view = view[written:]
        os.ftruncate(self._fd, len(data))
        if sync:
            os.fsync(self._fd)

    def _read_holder(self) -> dict[str, Any]:
        for attempt in range(3):
            try:
                raw = self.path.read_text(encoding="utf-8")
                payload = json.loads(raw)
            except (OSError, UnicodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                return payload
            if attempt < 2:
                time.sleep(0.005)
        return {}

    def _holder_is_stale(self, holder: dict[str, Any]) -> bool:
        if holder.get("released_at"):
            return True
        try:
            pid = int(holder.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0 and not process_pid_alive(pid):
            return True
        if pid > 0:
            recorded_start = str(holder.get("process_start") or "")
            current_start = process_start_marker(pid) if recorded_start else ""
            if recorded_start and current_start and recorded_start != current_start:
                return True
            token = str(holder.get("owner_token") or "")
            same_instance = (
                pid == os.getpid()
                and str(holder.get("process_instance") or "") == _PROCESS_INSTANCE_TOKEN
            )
            if token and same_instance:
                with _ACTIVE_LONG_LOCKS_GUARD:
                    return token not in _ACTIVE_LONG_LOCK_TOKENS
            # Heartbeat age alone cannot safely fence a live owner.
            return False
        # Empty or malformed payloads can be a heartbeat write observed in
        # flight. Without a PID/start identity there is no safe fencing proof;
        # require explicit operator cleanup instead of stealing the lock.
        return False

    def _unlink_holder_if_unchanged(self, holder: dict[str, Any]) -> bool:
        try:
            current = self._read_holder()
            expected_token = str(holder.get("owner_token") or "")
            if expected_token:
                if str(current.get("owner_token") or "") != expected_token:
                    return False
            elif current != holder:
                return False
            self.path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

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
    while True:
        try:
            acquired = lock.try_acquire(mutation_deadline=deadline)
        except TimeoutError as exc:
            holder = lock._read_holder()
            raise ProcessLockError(
                f"timed out after {wait_timeout_seconds}s waiting for consume lock "
                f"(held by {ProcessLock._describe(holder)}): {lock.path}",
                holder=holder,
            ) from exc
        if acquired:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            holder = lock._read_holder()
            raise ProcessLockError(
                f"timed out after {wait_timeout_seconds}s waiting for consume lock "
                f"(held by {ProcessLock._describe(holder)}): {lock.path}",
                holder=holder,
            )
        time.sleep(min(interval, remaining))
    try:
        yield lock
    finally:
        lock.release()

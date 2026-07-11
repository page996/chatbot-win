from __future__ import annotations

import json
import multiprocessing
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.runtime.process_lock import (
    ProcessLock,
    ProcessLockError,
    ShortProcessLock,
    blocking_process_lock,
    pid_lock_file_is_stale,
    process_lock,
    process_start_marker,
    scoped_process_lock_path,
    short_process_lock,
)


def _delayed_stale_takeover_process(
    path: str,
    label: str,
    delay_unlink: bool,
    started,
    unlink_entered,
    allow_unlink,
    release_owner,
    results,
) -> None:
    lock = ProcessLock(path, label=label, stale_after_seconds=1.0)
    started.set()
    real_unlink = Path.unlink
    delayed = False

    def controlled_unlink(target: Path, *args, **kwargs):
        nonlocal delayed
        if delay_unlink and target == lock.path and not delayed:
            delayed = True
            unlink_entered.set()
            if not allow_unlink.wait(10.0):
                raise TimeoutError("test did not release stale-owner unlink")
        return real_unlink(target, *args, **kwargs)

    try:
        with mock.patch.object(Path, "unlink", new=controlled_unlink):
            lock.acquire()
            results.put((label, "acquired"))
            release_owner.wait(10.0)
            lock.release()
    except ProcessLockError:
        results.put((label, "blocked"))
    except Exception as exc:
        results.put((label, f"error:{type(exc).__name__}:{exc}"))


class ProcessLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "consumer.lock"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_second_acquire_fails_while_held(self) -> None:
        first = ProcessLock(self.path, label="first")
        first.acquire()
        try:
            second = ProcessLock(self.path, label="second")
            with self.assertRaises(ProcessLockError) as ctx:
                second.acquire()
            self.assertIn("first", str(ctx.exception))
            self.assertEqual(ctx.exception.holder.get("label"), "first")
        finally:
            first.release()

    def test_release_allows_reacquire(self) -> None:
        first = ProcessLock(self.path, label="first")
        first.acquire()
        first.release()
        second = ProcessLock(self.path, label="second")
        second.acquire()  # should not raise
        second.release()
        self.assertFalse(self.path.exists())

    def test_successful_long_release_does_not_fsync_terminal_marker(self) -> None:
        lock = ProcessLock(self.path, label="release-fast-path")
        lock.acquire()

        with mock.patch(
            "app.personal_wechat_bot.runtime.process_lock.os.fsync",
            side_effect=AssertionError("successful release must not fsync terminal marker"),
        ):
            lock.release()

        self.assertFalse(self.path.exists())

    def test_stale_lock_is_taken_over(self) -> None:
        # Write a lock file with an old heartbeat and a PID that is not us.
        self.path.write_text(
            json.dumps({"pid": 999999, "label": "dead", "heartbeat_at": time.time() - 3600}),
            encoding="utf-8",
        )
        lock = ProcessLock(self.path, label="fresh", stale_after_seconds=60.0)
        lock.acquire()  # takes over the stale lock
        try:
            holder = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(holder["label"], "fresh")
        finally:
            lock.release()

    def test_fresh_heartbeat_blocks_takeover(self) -> None:
        self.path.write_text(
            json.dumps({"pid": os.getpid(), "label": "alive", "heartbeat_at": time.time()}),
            encoding="utf-8",
        )
        lock = ProcessLock(self.path, label="intruder", stale_after_seconds=60.0)
        with self.assertRaises(ProcessLockError):
            lock.acquire()

    def test_dead_pid_with_fresh_heartbeat_is_taken_over(self) -> None:
        self.path.write_text(
            json.dumps({"pid": 999999, "label": "dead", "heartbeat_at": time.time()}),
            encoding="utf-8",
        )
        lock = ProcessLock(self.path, label="fresh", stale_after_seconds=60.0)
        lock.acquire()
        try:
            holder = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(holder["label"], "fresh")
        finally:
            lock.release()

    def test_heartbeat_updates_timestamp(self) -> None:
        lock = ProcessLock(self.path, label="hb")
        lock.acquire()
        try:
            first = json.loads(self.path.read_text(encoding="utf-8"))["heartbeat_at"]
            time.sleep(0.02)
            lock.heartbeat()
            second = json.loads(self.path.read_text(encoding="utf-8"))["heartbeat_at"]
            self.assertGreater(second, first)
        finally:
            lock.release()

    def test_aged_heartbeat_does_not_steal_from_same_live_process(self) -> None:
        first = ProcessLock(self.path, label="live", stale_after_seconds=1.0)
        first.acquire()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            payload["heartbeat_at"] = time.time() - 3600
            self.path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(ProcessLockError):
                ProcessLock(self.path, label="intruder", stale_after_seconds=1.0).acquire()
        finally:
            first.release()

    def test_pid_reuse_is_recovered_using_process_start_marker(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "pid": 12345,
                    "process_start": "old-start",
                    "process_instance": "old-instance",
                    "owner_token": "old-owner",
                    "label": "old",
                    "heartbeat_at": time.time(),
                }
            ),
            encoding="utf-8",
        )
        with (
            mock.patch(
                "app.personal_wechat_bot.runtime.process_lock.process_pid_alive",
                return_value=True,
            ),
            mock.patch(
                "app.personal_wechat_bot.runtime.process_lock.process_start_marker",
                return_value="new-start",
            ),
        ):
            lock = ProcessLock(self.path, label="replacement")
            lock.acquire()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(payload["label"], "replacement")
            self.assertNotEqual(payload["owner_token"], "old-owner")
        finally:
            lock.release()

    def test_cross_process_stale_takeover_has_single_owner(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "pid": 999999,
                    "process_start": "dead-start",
                    "owner_token": "dead-owner",
                    "label": "dead",
                    "heartbeat_at": time.time() - 3600,
                }
            ),
            encoding="utf-8",
        )
        context = multiprocessing.get_context("spawn")
        first_started = context.Event()
        second_started = context.Event()
        unlink_entered = context.Event()
        allow_unlink = context.Event()
        release_owner = context.Event()
        results = context.Queue()
        first = context.Process(
            target=_delayed_stale_takeover_process,
            args=(
                str(self.path),
                "first",
                True,
                first_started,
                unlink_entered,
                allow_unlink,
                release_owner,
                results,
            ),
        )
        second = context.Process(
            target=_delayed_stale_takeover_process,
            args=(
                str(self.path),
                "second",
                False,
                second_started,
                context.Event(),
                allow_unlink,
                release_owner,
                results,
            ),
        )
        first.start()
        second_started_ok = False
        try:
            self.assertTrue(first_started.wait(10.0))
            self.assertTrue(unlink_entered.wait(10.0))
            first_guard_payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(first_guard_payload["owner_token"], "dead-owner")

            second.start()
            second_started_ok = second_started.wait(10.0)
            self.assertTrue(second_started_ok)
            with self.assertRaises(queue.Empty):
                results.get(timeout=0.5)

            allow_unlink.set()
            outcomes = {results.get(timeout=10.0), results.get(timeout=10.0)}
            self.assertEqual(outcomes, {("first", "acquired"), ("second", "blocked")})
        finally:
            allow_unlink.set()
            release_owner.set()
            for process in (first, second):
                if process.pid is None:
                    continue
                process.join(timeout=10.0)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5.0)
        self.assertEqual(first.exitcode, 0)
        if second_started_ok:
            self.assertEqual(second.exitcode, 0)
        self.assertFalse(self.path.exists())
        self.assertTrue(Path(f"{self.path}.guard").exists())

    def test_release_does_not_unlink_replacement_owner(self) -> None:
        lock = ProcessLock(self.path, label="first")
        lock.acquire()
        lock._close_fd()
        replacement = {
            "pid": 54321,
            "process_start": "replacement-start",
            "process_instance": "replacement-instance",
            "owner_token": "replacement-owner",
            "label": "replacement",
            "heartbeat_at": time.time(),
        }
        self.path.write_text(json.dumps(replacement), encoding="utf-8")

        lock.release()

        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), replacement)

    def test_released_long_lock_is_recoverable_when_unlink_never_succeeds(self) -> None:
        lock = ProcessLock(self.path, label="first")
        lock.acquire()
        with (
            mock.patch.object(Path, "unlink", side_effect=PermissionError("busy")),
            mock.patch(
                "app.personal_wechat_bot.runtime.process_lock.os.fsync",
                wraps=os.fsync,
            ) as fsync,
        ):
            lock.release()

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(fsync.call_count, 1)
        self.assertIn("released_at", payload)
        self.assertEqual(payload["heartbeat_at"], 0.0)
        self.assertTrue(lock._holder_is_stale(payload))

        replacement = ProcessLock(self.path, label="replacement")
        replacement.acquire()
        replacement.release()
        self.assertFalse(self.path.exists())

    def test_long_release_marker_remains_recoverable_when_terminal_sync_fails(self) -> None:
        import app.personal_wechat_bot.runtime.process_lock as process_lock_module

        lock = ProcessLock(self.path, label="unsynced-release")
        lock.acquire()
        with (
            mock.patch.object(Path, "unlink", side_effect=PermissionError("busy")),
            mock.patch(
                "app.personal_wechat_bot.runtime.process_lock.os.fsync",
                side_effect=OSError("sync unavailable"),
            ),
        ):
            lock.release()

        self.assertNotIn(lock.owner_token, process_lock_module._ACTIVE_LONG_LOCK_TOKENS)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("released_at", payload)
        self.assertTrue(lock._holder_is_stale(payload))
        self.path.unlink()

    def test_long_lock_payload_records_process_identity_and_owner(self) -> None:
        lock = ProcessLock(self.path, label="identity")
        lock.acquire()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(payload["pid"], os.getpid())
            self.assertTrue(payload["process_instance"])
            self.assertTrue(payload["owner_token"])
            self.assertIn("process_start", payload)
        finally:
            lock.release()

    @unittest.skipUnless(os.name == "nt", "Windows process identity contract")
    def test_windows_process_start_marker_uses_valid_handle(self) -> None:
        marker = process_start_marker(os.getpid())
        self.assertRegex(marker, r"^win:\d+$")

    @unittest.skipUnless(os.name == "nt", "Windows PID probing contract")
    def test_windows_pid_probe_only_declares_explicitly_missing_process_dead(self) -> None:
        import ctypes

        from app.personal_wechat_bot.runtime.process_lock import process_pid_alive

        kernel32 = mock.Mock()
        kernel32.OpenProcess = mock.Mock(return_value=0)
        kernel32.WaitForSingleObject = mock.Mock(return_value=0xFFFFFFFF)
        kernel32.CloseHandle = mock.Mock(return_value=True)
        test_pid = os.getpid() + 1_000_000

        with (
            mock.patch.object(ctypes, "WinDLL", return_value=kernel32),
            mock.patch.object(ctypes, "get_last_error", return_value=5),
        ):
            self.assertTrue(process_pid_alive(test_pid))

        with (
            mock.patch.object(ctypes, "WinDLL", return_value=kernel32),
            mock.patch.object(ctypes, "get_last_error", return_value=87),
        ):
            self.assertFalse(process_pid_alive(test_pid))

        kernel32.OpenProcess.return_value = 123
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel32):
            self.assertTrue(process_pid_alive(test_pid))

        with mock.patch.object(ctypes, "WinDLL", side_effect=OSError("kernel unavailable")):
            self.assertTrue(process_pid_alive(test_pid))
            self.assertEqual(process_start_marker(test_pid), "")

    def test_existing_lock_permission_error_is_reported_as_contention(self) -> None:
        self.path.write_text(
            json.dumps({"pid": os.getpid(), "label": "held", "heartbeat_at": time.time()}),
            encoding="utf-8",
        )
        lock = ProcessLock(self.path, label="intruder")
        real_open = os.open

        def sharing_violation(path, flags, *args):
            if Path(path) == self.path:
                raise PermissionError(13, "sharing violation", str(self.path))
            return real_open(path, flags, *args)

        with mock.patch(
            "app.personal_wechat_bot.runtime.process_lock.os.open",
            side_effect=sharing_violation,
        ):
            with self.assertRaises(ProcessLockError) as ctx:
                lock.acquire()
        self.assertEqual(ctx.exception.holder.get("label"), "held")

    def test_permission_error_without_lock_file_propagates(self) -> None:
        lock = ProcessLock(self.path, label="blocked")
        real_open = os.open

        def directory_denied(path, flags, *args):
            if Path(path) == self.path:
                raise PermissionError(13, "parent directory denied", str(self.path))
            return real_open(path, flags, *args)

        with mock.patch(
            "app.personal_wechat_bot.runtime.process_lock.os.open",
            side_effect=directory_denied,
        ):
            with self.assertRaises(PermissionError):
                lock.try_acquire()

    @unittest.skipIf(os.name == "nt", "POSIX PID probing contract")
    def test_non_lookup_pid_probe_error_fails_closed(self) -> None:
        with mock.patch(
            "app.personal_wechat_bot.runtime.process_lock.os.kill",
            side_effect=OSError("transient kernel failure"),
        ):
            from app.personal_wechat_bot.runtime.process_lock import process_pid_alive

            self.assertTrue(process_pid_alive(12345))

    def test_mutation_guard_retries_transient_open_permission_error(self) -> None:
        real_open = os.open
        guard_path = Path(f"{self.path}.guard")
        guard_attempts = 0

        def transient_guard_error(path, flags, *args):
            nonlocal guard_attempts
            if Path(path) == guard_path:
                guard_attempts += 1
                if guard_attempts == 1:
                    raise PermissionError(13, "guard temporarily unavailable", str(guard_path))
            return real_open(path, flags, *args)

        with mock.patch(
            "app.personal_wechat_bot.runtime.process_lock.os.open",
            side_effect=transient_guard_error,
        ):
            with process_lock(self.path, label="guard-retry"):
                self.assertTrue(self.path.exists())

        self.assertGreaterEqual(guard_attempts, 3)
        self.assertFalse(self.path.exists())
        self.assertTrue(guard_path.exists())

    def test_mutation_guard_open_permission_error_honors_deadline(self) -> None:
        lock = ProcessLock(self.path, label="guard-deadline")
        guard_path = Path(f"{self.path}.guard")
        real_open = os.open

        def denied_guard(path, flags, *args):
            if Path(path) == guard_path:
                raise PermissionError(13, "guard unavailable", str(guard_path))
            return real_open(path, flags, *args)

        started = time.monotonic()
        with mock.patch(
            "app.personal_wechat_bot.runtime.process_lock.os.open",
            side_effect=denied_guard,
        ):
            with self.assertRaises(TimeoutError):
                lock.acquire(mutation_deadline=time.monotonic() + 0.03)

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(self.path.exists())

    def test_malformed_long_lock_is_never_automatically_stolen(self) -> None:
        self.path.write_text('{"pid":', encoding="utf-8")
        old = time.time() - 3600
        os.utime(self.path, (old, old))

        with self.assertRaises(ProcessLockError):
            ProcessLock(self.path, label="intruder", stale_after_seconds=1.0).acquire()

    def test_holder_read_retries_through_partial_json(self) -> None:
        lock = ProcessLock(self.path, label="reader")
        complete = json.dumps({"pid": 12345, "label": "complete", "heartbeat_at": time.time()})
        with mock.patch.object(Path, "read_text", side_effect=['{"pid":', complete]) as read:
            holder = lock._read_holder()

        self.assertEqual(holder.get("label"), "complete")
        self.assertEqual(read.call_count, 2)

    def test_invalid_initial_metadata_creates_no_lock_file(self) -> None:
        lock = ProcessLock(self.path, label="invalid", metadata={"bad": object()})

        with self.assertRaises(TypeError):
            lock.acquire()

        self.assertFalse(self.path.exists())
        self.assertFalse(Path(f"{self.path}.guard").exists())

    def test_initial_payload_failure_closes_descriptor_and_marks_orphan_released(self) -> None:
        lock = ProcessLock(self.path, label="write-failure")
        real_write = os.write
        failed = False

        def fail_first_payload(fd: int, data) -> int:
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("disk full")
            return real_write(fd, data)

        with (
            mock.patch(
                "app.personal_wechat_bot.runtime.process_lock.os.write",
                side_effect=fail_first_payload,
            ),
            mock.patch.object(Path, "unlink", side_effect=PermissionError("busy")),
        ):
            with self.assertRaises(OSError):
                lock.acquire()

        self.assertIsNone(lock._fd)
        self.assertFalse(lock._acquired)
        self.assertTrue(self.path.exists())
        self.assertTrue(lock._holder_is_stale(json.loads(self.path.read_text(encoding="utf-8"))))

    def test_metadata_write_failure_rolls_back_in_memory_metadata(self) -> None:
        lock = ProcessLock(self.path, label="metadata", metadata={"before": 1})
        lock.acquire()
        try:
            with mock.patch.object(lock, "_write_payload", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    lock.update_metadata({"after": 2})
            self.assertEqual(lock.metadata, {"before": 1})
        finally:
            lock.release()

    def test_heartbeat_writes_before_truncating_old_payload(self) -> None:
        lock = ProcessLock(self.path, label="heartbeat-order")
        lock.acquire()
        real_write = os.write
        real_truncate = os.ftruncate
        events: list[str] = []

        def tracking_write(fd: int, data) -> int:
            events.append("write")
            return real_write(fd, data)

        def tracking_truncate(fd: int, size: int) -> None:
            events.append("truncate")
            real_truncate(fd, size)

        try:
            with (
                mock.patch("app.personal_wechat_bot.runtime.process_lock.os.write", side_effect=tracking_write),
                mock.patch(
                    "app.personal_wechat_bot.runtime.process_lock.os.ftruncate",
                    side_effect=tracking_truncate,
                ),
            ):
                lock.heartbeat()
        finally:
            lock.release()

        self.assertEqual(events[0], "write")
        self.assertIn("truncate", events)

    def test_context_manager_disabled_is_noop(self) -> None:
        with process_lock(self.path, enabled=False) as lock:
            self.assertIsNone(lock)
        self.assertFalse(self.path.exists())

    def test_context_manager_releases_on_exit(self) -> None:
        with process_lock(self.path, label="ctx") as lock:
            self.assertIsNotNone(lock)
            self.assertTrue(self.path.exists())
        self.assertFalse(self.path.exists())

    def test_aged_legacy_short_lock_with_current_pid_is_stale(self) -> None:
        self.path.write_text(str(os.getpid()), encoding="ascii")
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        self.assertTrue(pid_lock_file_is_stale(self.path, max_age_seconds=60.0))

    def test_missing_short_lock_is_not_reported_as_stale(self) -> None:
        self.assertFalse(pid_lock_file_is_stale(self.path, max_age_seconds=60.0))

    def test_aged_short_lock_with_other_live_pid_is_not_stale(self) -> None:
        self.path.write_text("12345", encoding="ascii")
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        with mock.patch(
            "app.personal_wechat_bot.runtime.process_lock.process_pid_alive",
            return_value=True,
        ):
            self.assertFalse(pid_lock_file_is_stale(self.path, max_age_seconds=60.0))

    def test_aged_short_lock_with_dead_pid_is_stale(self) -> None:
        self.path.write_text("999999", encoding="ascii")
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        self.assertTrue(pid_lock_file_is_stale(self.path, max_age_seconds=60.0))

    def test_fresh_short_lock_with_dead_pid_is_not_stale(self) -> None:
        self.path.write_text("999999", encoding="ascii")
        self.assertFalse(pid_lock_file_is_stale(self.path, max_age_seconds=60.0))

    def test_aged_legacy_short_lock_is_stale(self) -> None:
        self.path.write_text("legacy-owner", encoding="ascii")
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        self.assertTrue(pid_lock_file_is_stale(self.path, max_age_seconds=60.0))

    def test_active_tokenized_short_lock_is_not_stale_when_aged(self) -> None:
        with short_process_lock(
            self.path,
            timeout_seconds=1.0,
            stale_after_seconds=60.0,
        ):
            old = time.time() - 3600
            os.utime(self.path, (old, old))
            self.assertFalse(pid_lock_file_is_stale(self.path, max_age_seconds=60.0))

    def test_short_lock_release_retries_transient_unlink_failure(self) -> None:
        real_unlink = Path.unlink
        attempts = 0

        def transient_unlink(path: Path, *args, **kwargs):
            nonlocal attempts
            if path == self.path:
                attempts += 1
                if attempts < 3:
                    raise PermissionError("temporarily busy")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=transient_unlink):
            with short_process_lock(
                self.path,
                timeout_seconds=1.0,
                stale_after_seconds=60.0,
            ):
                self.assertTrue(self.path.exists())

        self.assertEqual(attempts, 3)
        self.assertFalse(self.path.exists())

    def test_successful_short_release_does_not_fsync_terminal_marker(self) -> None:
        lock = ShortProcessLock(
            self.path,
            timeout_seconds=1.0,
            stale_after_seconds=60.0,
        )
        lock.acquire()

        with mock.patch(
            "app.personal_wechat_bot.runtime.process_lock.os.fsync",
            side_effect=AssertionError("successful release must not fsync terminal marker"),
        ):
            lock.release()

        self.assertFalse(self.path.exists())

    def test_released_short_lock_is_recoverable_when_unlink_never_succeeds(self) -> None:
        lock = ShortProcessLock(
            self.path,
            timeout_seconds=1.0,
            stale_after_seconds=60.0,
        )
        lock.acquire()
        with (
            mock.patch.object(Path, "unlink", side_effect=PermissionError("busy")),
            mock.patch(
                "app.personal_wechat_bot.runtime.process_lock.os.fsync",
                wraps=os.fsync,
            ) as fsync,
        ):
            lock.release()

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(fsync.call_count, 1)
        self.assertIn("released_at", payload)
        self.assertTrue(pid_lock_file_is_stale(self.path, max_age_seconds=60.0))
        with short_process_lock(
            self.path,
            timeout_seconds=1.0,
            stale_after_seconds=60.0,
        ):
            self.assertTrue(self.path.exists())
        self.assertFalse(self.path.exists())

    def test_short_release_marker_remains_recoverable_when_terminal_sync_fails(self) -> None:
        import app.personal_wechat_bot.runtime.process_lock as process_lock_module

        lock = ShortProcessLock(
            self.path,
            timeout_seconds=1.0,
            stale_after_seconds=60.0,
        )
        lock.acquire()
        with (
            mock.patch.object(Path, "unlink", side_effect=PermissionError("busy")),
            mock.patch(
                "app.personal_wechat_bot.runtime.process_lock.os.fsync",
                side_effect=OSError("sync unavailable"),
            ),
        ):
            lock.release()

        self.assertNotIn(lock.owner_token, process_lock_module._ACTIVE_SHORT_LOCK_TOKENS)
        self.assertTrue(pid_lock_file_is_stale(self.path, max_age_seconds=60.0))
        self.path.unlink()

    def test_short_initial_payload_failure_marks_orphan_released(self) -> None:
        lock = ShortProcessLock(
            self.path,
            timeout_seconds=1.0,
            stale_after_seconds=60.0,
        )
        real_write = os.write
        failed = False

        def fail_first_payload(fd: int, data) -> int:
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("disk full")
            return real_write(fd, data)

        with (
            mock.patch(
                "app.personal_wechat_bot.runtime.process_lock.os.write",
                side_effect=fail_first_payload,
            ),
            mock.patch.object(Path, "unlink", side_effect=PermissionError("busy")),
        ):
            with self.assertRaises(OSError):
                lock.acquire()

        self.assertIsNone(lock._fd)
        self.assertTrue(self.path.exists())
        self.assertTrue(pid_lock_file_is_stale(self.path, max_age_seconds=60.0))

    def test_concurrent_short_stale_takeover_has_single_owner(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "pid": 999999,
                    "owner_token": "released-owner",
                    "released_at": time.time(),
                }
            ),
            encoding="utf-8",
        )
        real_unlink = Path.unlink
        first_unlink_entered = threading.Event()
        allow_first_unlink = threading.Event()
        release_owner = threading.Event()
        second_acquired = threading.Event()
        outcomes: queue.Queue[tuple[str, str]] = queue.Queue()
        delayed = False

        def controlled_unlink(target: Path, *args, **kwargs):
            nonlocal delayed
            if (
                target == self.path
                and threading.current_thread().name == "short-first"
                and not delayed
            ):
                delayed = True
                first_unlink_entered.set()
                if not allow_first_unlink.wait(5.0):
                    raise TimeoutError("test did not release short stale-owner unlink")
            return real_unlink(target, *args, **kwargs)

        def contender(label: str, timeout_seconds: float) -> None:
            lock = ShortProcessLock(
                self.path,
                timeout_seconds=timeout_seconds,
                stale_after_seconds=1.0,
                poll_interval_seconds=0.01,
            )
            acquired = False
            try:
                lock.acquire()
                acquired = True
                if label == "second":
                    second_acquired.set()
                outcomes.put((label, "acquired"))
                release_owner.wait(5.0)
            except TimeoutError:
                outcomes.put((label, "timeout"))
            finally:
                if acquired:
                    lock.release()

        first = threading.Thread(target=contender, args=("first", 2.0), name="short-first")
        second = threading.Thread(target=contender, args=("second", 0.3), name="short-second")
        with mock.patch.object(Path, "unlink", new=controlled_unlink):
            first.start()
            self.assertTrue(first_unlink_entered.wait(5.0))
            second.start()
            self.assertFalse(second_acquired.wait(0.2))
            allow_first_unlink.set()
            observed = {outcomes.get(timeout=5.0), outcomes.get(timeout=5.0)}
            self.assertEqual(observed, {("first", "acquired"), ("second", "timeout")})
            release_owner.set()
            first.join(timeout=5.0)
            second.join(timeout=5.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(self.path.exists())
        self.assertTrue(Path(f"{self.path}.guard").exists())

    def test_process_start_mismatch_marks_aged_lock_stale(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "pid": 12345,
                    "process_start": "old-process",
                    "process_instance": "old-instance",
                    "owner_token": "old-token",
                }
            ),
            encoding="utf-8",
        )
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        with (
            mock.patch(
                "app.personal_wechat_bot.runtime.process_lock.process_pid_alive",
                return_value=True,
            ),
            mock.patch(
                "app.personal_wechat_bot.runtime.process_lock.process_start_marker",
                return_value="new-process",
            ),
        ):
            self.assertTrue(pid_lock_file_is_stale(self.path, max_age_seconds=60.0))

    def test_forked_identity_cannot_release_parent_lock(self) -> None:
        lock = ProcessLock(self.path, label="parent")
        lock.acquire()
        try:
            with mock.patch(
                "app.personal_wechat_bot.runtime.process_lock.os.getpid",
                return_value=os.getpid() + 1000,
            ):
                lock.release()
            self.assertTrue(self.path.exists())
        finally:
            lock._creator_pid = os.getpid()
            lock._acquired = True
            lock.release()

    def test_scoped_lock_path_is_stable_and_ascii(self) -> None:
        first = scoped_process_lock_path(self.path.parent, "conversation/lifecycle", "会话/../1")
        second = scoped_process_lock_path(self.path.parent, "conversation/lifecycle", "会话/../1")

        self.assertEqual(first, second)
        self.assertEqual(first.parent, self.path.parent / "runtime_locks" / "scoped")
        self.assertTrue(first.name.isascii())
        self.assertNotIn("..", first.name)


class HookRunnerSingleInstanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _runner(self):
        from unittest import mock
        from app.personal_wechat_bot.runtime.hook_pull_runner import HookMessagePullRunner

        hook_file = self.root / "hook_events.jsonl"
        backend_file = self.root / "backend_events.jsonl"
        state_file = self.root / "hook_events_state.json"
        importer = mock.Mock()
        importer.state_path = state_file
        polling = mock.Mock()
        return HookMessagePullRunner(
            importer,
            polling,
            hook_event_file=hook_file,
            backend_event_file=backend_file,
        )

    def test_two_runners_are_mutually_exclusive(self) -> None:
        from app.personal_wechat_bot.runtime.process_lock import ProcessLockError

        runner_a = self._runner()
        runner_b = self._runner()
        with runner_a.single_instance(label="a"):
            with self.assertRaises(ProcessLockError):
                with runner_b.single_instance(label="b"):
                    pass

    def test_lock_released_allows_second_runner(self) -> None:
        runner_a = self._runner()
        with runner_a.single_instance(label="a"):
            pass
        runner_b = self._runner()
        with runner_b.single_instance(label="b"):
            self.assertTrue(runner_b.lock_path().exists())

    def test_disabled_lock_allows_concurrent(self) -> None:
        runner_a = self._runner()
        runner_b = self._runner()
        with runner_a.single_instance(enabled=False):
            with runner_b.single_instance(enabled=False):
                self.assertFalse(runner_a.lock_path().exists())

    def test_consume_lock_path_differs_from_loop_ownership_lock(self) -> None:
        runner = self._runner()
        self.assertNotEqual(runner.consume_lock_path(), runner.lock_path())
        self.assertTrue(str(runner.consume_lock_path()).endswith(".consume.lock"))
        self.assertTrue(str(runner.lock_path()).endswith(".consumer.lock"))

    def test_consume_lock_serializes_run_once_across_runners(self) -> None:
        # Two runners sharing the same state path must not run their consume
        # step at the same time when consume_lock_enabled=True.
        import threading

        overlap = {"max": 0, "active": 0}
        guard = threading.Lock()

        def make_runner():
            from unittest import mock
            from app.personal_wechat_bot.runtime.hook_pull_runner import HookMessagePullRunner

            importer = mock.Mock()
            importer.state_path = self.root / "hook_events_state.json"

            def import_new():
                with guard:
                    overlap["active"] += 1
                    overlap["max"] = max(overlap["max"], overlap["active"])
                time.sleep(0.05)
                with guard:
                    overlap["active"] -= 1
                return mock.Mock(
                    status="ok",
                    error_count=0,
                    source_offset=0,
                    backend_event_count=0,
                    scanned_count=0,
                    appended_count=0,
                    skipped_count=0,
                    source_path="hook_events.jsonl",
                    backend_event_path="backend_events.jsonl",
                    appended_raw_ids=[],
                    errors=[],
                )

            importer.import_new.side_effect = import_new
            polling = mock.Mock()
            polling.run_once.return_value = {"status": "ok", "processed": []}
            polling.driver = mock.Mock(_seen_event_ids=set(), _seen_message_raw_ids=set())
            return HookMessagePullRunner(
                importer,
                polling,
                hook_event_file=self.root / "hook_events.jsonl",
                backend_event_file=self.root / "backend_events.jsonl",
                consume_lock_enabled=True,
                consume_lock_wait_seconds=5.0,
            )

        threads = [threading.Thread(target=lambda: make_runner().run_once()) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(overlap["max"], 1)


class BlockingProcessLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "consume.lock"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_waits_for_holder_then_acquires(self) -> None:
        import threading

        order: list[str] = []
        release_holder = threading.Event()

        def holder():
            with blocking_process_lock(self.path, label="holder", wait_timeout_seconds=2):
                order.append("holder-in")
                release_holder.wait(1)
                order.append("holder-out")

        t = threading.Thread(target=holder)
        t.start()
        # Wait until the holder is inside the lock.
        while "holder-in" not in order:
            time.sleep(0.01)
        release_holder.set()
        with blocking_process_lock(self.path, label="waiter", wait_timeout_seconds=2):
            order.append("waiter-in")
        t.join()
        # The waiter must only enter after the holder has left.
        self.assertEqual(order, ["holder-in", "holder-out", "waiter-in"])

    def test_times_out_when_holder_never_releases(self) -> None:
        with process_lock(self.path, label="holder", stale_after_seconds=60):
            with self.assertRaises(ProcessLockError):
                with blocking_process_lock(
                    self.path, label="waiter", stale_after_seconds=60, wait_timeout_seconds=0.2
                ):
                    pass

    def test_total_deadline_is_forwarded_to_each_mutation_attempt(self) -> None:
        observed: list[float | None] = []

        def blocked(lock: ProcessLock, *, mutation_deadline: float | None = None) -> bool:
            observed.append(mutation_deadline)
            return False

        started = time.monotonic()
        with mock.patch.object(ProcessLock, "try_acquire", new=blocked):
            with self.assertRaises(ProcessLockError):
                with blocking_process_lock(
                    self.path,
                    label="waiter",
                    wait_timeout_seconds=0.05,
                    poll_interval_seconds=0.01,
                ):
                    pass
        elapsed = time.monotonic() - started

        self.assertGreaterEqual(len(observed), 2)
        self.assertTrue(all(item == observed[0] for item in observed))
        self.assertIsNotNone(observed[0])
        self.assertLess(elapsed, 0.5)

    def test_mutation_guard_deadline_becomes_process_lock_timeout(self) -> None:
        with mock.patch.object(
            ProcessLock,
            "try_acquire",
            side_effect=TimeoutError("guard busy"),
        ):
            with self.assertRaises(ProcessLockError) as ctx:
                with blocking_process_lock(
                    self.path,
                    label="waiter",
                    wait_timeout_seconds=0.01,
                ):
                    pass
        self.assertIn("timed out", str(ctx.exception))

    def test_disabled_is_noop(self) -> None:
        with blocking_process_lock(self.path, enabled=False) as lock:
            self.assertIsNone(lock)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()

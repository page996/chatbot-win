from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from app.personal_wechat_bot.runtime.process_lock import (
    ProcessLock,
    ProcessLockError,
    blocking_process_lock,
    process_lock,
)


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

    def test_context_manager_disabled_is_noop(self) -> None:
        with process_lock(self.path, enabled=False) as lock:
            self.assertIsNone(lock)
        self.assertFalse(self.path.exists())

    def test_context_manager_releases_on_exit(self) -> None:
        with process_lock(self.path, label="ctx") as lock:
            self.assertIsNotNone(lock)
            self.assertTrue(self.path.exists())
        self.assertFalse(self.path.exists())


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

    def test_disabled_is_noop(self) -> None:
        with blocking_process_lock(self.path, enabled=False) as lock:
            self.assertIsNone(lock)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()

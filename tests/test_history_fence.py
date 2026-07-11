from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.config.loader import create_default_config
from app.personal_wechat_bot.control import cli
from app.personal_wechat_bot.control.sidebar_api import clear_sidebar_history_data
from app.personal_wechat_bot.runtime import history_fence
from app.personal_wechat_bot.runtime.history_fence import (
    active_history_writer_leases,
    history_reset_fence_path,
    history_writer_fence,
    history_writer_lease_after_startup_handoff_if_owned,
    history_writer_lease_dir,
    owned_history_roots,
    register_history_writer_lease,
    register_history_writer_startup_handoff_if_owned,
)
from app.personal_wechat_bot.runtime.process_lock import ProcessLock, short_process_lock
from app.personal_wechat_bot.wechat_driver.backend_events import append_backend_event


class HistoryFenceTest(unittest.TestCase):
    def test_cli_lazy_read_initializers_hold_writer_lease(self) -> None:
        cases = (
            ("confirm-list", "list_confirm_queue"),
            ("send-audit", "list_send_audit"),
            ("send-bridge-state", "bridge_state"),
        )
        for command, target in cases:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                create_default_config(data_dir)
                observed: list[bool] = []

                def inspect_lease(*args, **kwargs):
                    observed.append(bool(active_history_writer_leases(data_dir)))
                    return {}

                with mock.patch.object(cli, target, side_effect=inspect_lease), redirect_stdout(io.StringIO()):
                    cli.main(["--data-dir", str(data_dir), command])

                self.assertEqual(observed, [True])
                self.assertEqual(active_history_writer_leases(data_dir), [])

    def test_nested_writer_fence_is_reentrant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            with history_writer_fence(data_dir, label="outer"):
                with history_writer_fence(data_dir, label="inner"):
                    self.assertTrue(history_reset_fence_path(data_dir).exists())

            self.assertFalse(history_reset_fence_path(data_dir).exists())

    def test_backend_event_append_waits_for_reset_fence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            event_path = data_dir / "backend_events.jsonl"
            fence = ProcessLock(history_reset_fence_path(data_dir), label="history_clear", stale_after_seconds=3600)
            fence.acquire()
            started = threading.Event()
            finished = threading.Event()

            def append() -> None:
                started.set()
                append_backend_event(event_path, chat_title="Alice", sender_name="Alice", text="hello")
                finished.set()

            thread = threading.Thread(target=append, daemon=True)
            thread.start()
            try:
                self.assertTrue(started.wait(1.0))
                time.sleep(0.1)
                self.assertFalse(finished.is_set())
                self.assertFalse(event_path.exists())
            finally:
                fence.release()
                thread.join(timeout=3.0)
            self.assertTrue(finished.is_set())
            self.assertTrue(event_path.exists())

    def test_clear_blocks_while_writer_fence_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            sentinel = data_dir / "backend_events.jsonl"
            sentinel.write_text("{}\n", encoding="utf-8")
            fence = ProcessLock(
                history_reset_fence_path(data_dir),
                label="history_writer:test",
                stale_after_seconds=3600,
            )
            fence.acquire()
            try:
                result = clear_sidebar_history_data(data_dir)
            finally:
                fence.release()

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["active_workers"][0]["worker"], "history_writer")
            self.assertTrue(sentinel.exists())

    def test_acquiring_fence_is_published_before_os_lock_and_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            os_attempted = threading.Event()
            allow_os_lock = threading.Event()
            owner_entered = threading.Event()
            adopter_entered = threading.Event()
            release_owner = threading.Event()
            release_adopter = threading.Event()
            owner_finished = threading.Event()
            os_released = threading.Event()
            errors: list[BaseException] = []
            calls = 0
            calls_guard = threading.Lock()

            @contextmanager
            def fake_process_lock(*_args, **_kwargs):
                nonlocal calls
                with calls_guard:
                    calls += 1
                os_attempted.set()
                self.assertTrue(allow_os_lock.wait(2.0))
                try:
                    yield
                finally:
                    os_released.set()

            def owner() -> None:
                try:
                    with history_writer_fence(data_dir, label="owner"):
                        owner_entered.set()
                        release_owner.wait(3.0)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    owner_finished.set()

            def adopter() -> None:
                try:
                    with history_writer_fence(data_dir, label="adopter"):
                        adopter_entered.set()
                        release_adopter.wait(3.0)
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch.object(history_fence, "blocking_process_lock", fake_process_lock):
                owner_thread = threading.Thread(target=owner)
                adopter_thread = threading.Thread(target=adopter)
                owner_thread.start()
                self.assertTrue(os_attempted.wait(1.0))
                adopter_thread.start()
                time.sleep(0.05)
                self.assertEqual(calls, 1)
                allow_os_lock.set()
                self.assertTrue(owner_entered.wait(1.0))
                self.assertTrue(adopter_entered.wait(1.0))
                release_owner.set()
                time.sleep(0.05)
                self.assertFalse(owner_finished.is_set())
                self.assertFalse(os_released.is_set())
                release_adopter.set()
                owner_thread.join(timeout=2.0)
                adopter_thread.join(timeout=2.0)

            self.assertEqual(errors, [])
            self.assertTrue(owner_finished.is_set())
            self.assertTrue(os_released.is_set())
            self.assertEqual(calls, 1)

    def test_owner_drains_adopter_before_reraising_close_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            owner_entered = threading.Event()
            adopter_entered = threading.Event()
            release_owner = threading.Event()
            release_adopter = threading.Event()
            wait_interrupted = threading.Event()
            errors: list[BaseException] = []

            def owner() -> None:
                try:
                    with history_writer_fence(data_dir, label="owner"):
                        owner_entered.set()
                        release_owner.wait(2.0)
                except BaseException as exc:
                    errors.append(exc)

            def adopter() -> None:
                with history_writer_fence(data_dir, label="adopter"):
                    adopter_entered.set()
                    release_adopter.wait(2.0)

            owner_thread = threading.Thread(target=owner)
            adopter_thread = threading.Thread(target=adopter)
            owner_thread.start()
            self.assertTrue(owner_entered.wait(1.0))
            adopter_thread.start()
            self.assertTrue(adopter_entered.wait(1.0))
            condition = history_fence._ACTIVE_PROCESS_FENCES_CONDITION
            original_wait = condition.wait
            interrupted = False

            def interrupt_once(timeout=None):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    wait_interrupted.set()
                    raise KeyboardInterrupt()
                return original_wait(timeout)

            with mock.patch.object(condition, "wait", side_effect=interrupt_once):
                release_owner.set()
                self.assertTrue(wait_interrupted.wait(1.0))
                self.assertTrue(history_reset_fence_path(data_dir).exists())
                release_adopter.set()
                owner_thread.join(timeout=2.0)
                adopter_thread.join(timeout=2.0)

            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], KeyboardInterrupt)
            self.assertFalse(history_reset_fence_path(data_dir).exists())

    def test_writer_lease_blocks_clear_until_released(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            sentinel = data_dir / "backend_events.jsonl"
            sentinel.write_text("{}\n", encoding="utf-8")
            lease = register_history_writer_lease(data_dir, label="queued_async")
            try:
                blocked = clear_sidebar_history_data(data_dir)
            finally:
                lease.release()

            self.assertEqual(blocked["status"], "blocked")
            self.assertTrue(sentinel.exists())
            cleared = clear_sidebar_history_data(data_dir)
            self.assertEqual(cleared["status"], "ok")
            self.assertFalse(sentinel.exists())

    def test_startup_handoff_survives_parent_death_only_until_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            handoff = register_history_writer_startup_handoff_if_owned(
                data_dir,
                label="worker_startup",
                ttl_seconds=5.0,
            )
            self.assertIsNotNone(handoff)
            assert handoff is not None
            history_fence._ACTIVE_LEASE_TOKENS.discard(handoff.lease.owner_token)
            try:
                with mock.patch.object(history_fence.time, "time", return_value=handoff.deadline_epoch - 0.1):
                    active = active_history_writer_leases(data_dir)
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["lease_kind"], "startup_handoff")
                self.assertTrue(handoff.lease.path.exists())

                with mock.patch.object(history_fence.time, "time", return_value=handoff.deadline_epoch + 0.1):
                    self.assertEqual(active_history_writer_leases(data_dir), [])
                self.assertFalse(handoff.lease.path.exists())
            finally:
                handoff.release()

    def test_child_atomically_adopts_valid_startup_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            handoff = register_history_writer_startup_handoff_if_owned(
                data_dir,
                label="worker_startup",
            )
            self.assertIsNotNone(handoff)
            assert handoff is not None
            expected_start = history_fence.process_start_marker(os.getpid())
            self.assertTrue(expected_start)
            approval_errors: list[BaseException] = []

            def approve_child() -> None:
                try:
                    deadline = time.monotonic() + 2.0
                    while not handoff.ready_path.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(
                        handoff.ready_for_process(
                            os.getpid(),
                            expected_process_start=expected_start,
                        )
                    )
                    handoff.release()
                except BaseException as exc:
                    approval_errors.append(exc)

            approval = threading.Thread(target=approve_child)
            approval.start()
            try:
                with mock.patch.dict(os.environ, handoff.child_environment(), clear=False):
                    with history_writer_lease_after_startup_handoff_if_owned(
                        data_dir,
                        label="child_worker",
                    ) as child_lease:
                        self.assertIsNotNone(child_lease)
                        self.assertFalse(handoff.lease.path.exists())
                        active = active_history_writer_leases(data_dir)
                        self.assertEqual(len(active), 1)
                        self.assertEqual(active[0]["label"], "child_worker")
                self.assertEqual(active_history_writer_leases(data_dir), [])
            finally:
                approval.join(timeout=3.0)
                handoff.release()
            self.assertEqual(approval_errors, [])

    def test_expired_startup_ready_file_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            handoff = register_history_writer_startup_handoff_if_owned(
                data_dir,
                label="worker_startup",
                ttl_seconds=1.0,
            )
            self.assertIsNotNone(handoff)
            assert handoff is not None
            try:
                with mock.patch.dict(os.environ, handoff.child_environment(), clear=False):
                    with mock.patch.object(
                        history_fence,
                        "_wait_for_startup_handoff_parent_acknowledgement",
                    ):
                        with history_writer_lease_after_startup_handoff_if_owned(
                            data_dir,
                            label="child_worker",
                        ):
                            self.assertTrue(handoff.ready_path.exists())
                self.assertTrue(handoff.ready_path.exists())
                with mock.patch.object(
                    history_fence.time,
                    "time",
                    return_value=handoff.deadline_epoch + 1.0,
                ):
                    self.assertEqual(active_history_writer_leases(data_dir), [])
                self.assertFalse(handoff.ready_path.exists())
            finally:
                handoff.release()

    def test_missing_ready_file_cannot_approve_child_after_handoff_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adopted = history_fence._AdoptedStartupHandoff(
                lease=mock.Mock(lease_id="child-lease"),
                ready_path=Path(tmp) / "missing.ready",
                owner_token="owner",
                deadline_epoch=time.time() - 1.0,
            )

            with self.assertRaisesRegex(RuntimeError, "parent acknowledgement timed out"):
                history_fence._wait_for_startup_handoff_parent_acknowledgement(adopted)

    def test_cancelled_parent_handoff_never_releases_child_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            handoff = register_history_writer_startup_handoff_if_owned(
                data_dir,
                label="worker_startup",
                ttl_seconds=0.3,
            )
            self.assertIsNotNone(handoff)
            assert handoff is not None
            entered = threading.Event()
            errors: list[BaseException] = []

            def child() -> None:
                try:
                    with mock.patch.dict(os.environ, handoff.child_environment(), clear=False):
                        with history_writer_lease_after_startup_handoff_if_owned(
                            data_dir,
                            label="child_worker",
                        ):
                            entered.set()
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=child)
            thread.start()
            deadline = time.monotonic() + 1.0
            while not handoff.ready_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(handoff.ready_path.exists())
            handoff.cancel()
            thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertFalse(entered.is_set())
            self.assertEqual(len(errors), 1)
            self.assertRegex(str(errors[0]), "parent acknowledgement timed out")

    def test_expired_handoff_rejects_late_child_before_worker_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            handoff = register_history_writer_startup_handoff_if_owned(
                data_dir,
                label="worker_startup",
                ttl_seconds=1.0,
            )
            self.assertIsNotNone(handoff)
            assert handoff is not None
            entered_worker_body = False
            try:
                with mock.patch.dict(os.environ, handoff.child_environment(), clear=False):
                    with mock.patch.object(
                        history_fence.time,
                        "time",
                        return_value=handoff.deadline_epoch + 1.0,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "missing or expired"):
                            with history_writer_lease_after_startup_handoff_if_owned(
                                data_dir,
                                label="late_child",
                            ):
                                entered_worker_body = True
                self.assertFalse(entered_worker_body)
                self.assertFalse(handoff.ready_path.exists())
                with mock.patch.object(
                    history_fence.time,
                    "time",
                    return_value=handoff.deadline_epoch + 1.0,
                ):
                    self.assertEqual(active_history_writer_leases(data_dir), [])
            finally:
                handoff.release()

    def test_legacy_hook_process_authorities_block_clear(self) -> None:
        for name in (
            "hook_events_state.json.consumer.lock",
            "hook_events_state.json.consume.lock",
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                create_default_config(data_dir)
                sentinel = data_dir / "backend_events.jsonl"
                sentinel.write_text("{}\n", encoding="utf-8")
                lock = ProcessLock(data_dir / name, label="legacy", stale_after_seconds=60.0)
                lock.acquire()
                try:
                    result = clear_sidebar_history_data(data_dir)
                finally:
                    lock.release()

                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["active_workers"][0]["source"], "legacy_process_lock")
                self.assertTrue(sentinel.exists())

    def test_legacy_hook_state_authority_blocks_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            sentinel = data_dir / "backend_events.jsonl"
            sentinel.write_text("{}\n", encoding="utf-8")
            with short_process_lock(
                data_dir / "hook_events_state.json.lock",
                timeout_seconds=1.0,
                stale_after_seconds=120.0,
            ):
                result = clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["active_workers"][0]["source"], "legacy_process_lock")
            self.assertTrue(sentinel.exists())

    def test_stale_legacy_hook_authorities_are_reclaimed_during_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            paths = [
                data_dir / "hook_events_state.json.consumer.lock",
                data_dir / "hook_events_state.json.consume.lock",
            ]
            for path in paths:
                path.write_text(
                    json.dumps({"pid": 2147483647, "label": "stale", "owner_token": path.name}),
                    encoding="utf-8",
                )

            result = clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertTrue(all(not path.exists() for path in paths))

    def test_invalid_lease_timestamp_is_an_unsafe_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            lease_dir = history_writer_lease_dir(data_dir)
            lease_dir.mkdir(parents=True)
            path = lease_dir / "malformed.json"
            path.write_text(
                json.dumps(
                    {
                        "lease_id": "bad",
                        "owner_token": "bad",
                        "label": "bad",
                        "pid": os.getpid(),
                        "created_at_epoch": "not-a-number",
                    }
                ),
                encoding="utf-8",
            )

            records = active_history_writer_leases(data_dir)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["reason"], "invalid_history_writer_lease_payload")

    def test_runtime_locks_directory_alias_is_rejected_before_external_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            outside = Path(tmp) / "outside"
            create_default_config(data_dir)
            outside.mkdir()
            runtime_locks = data_dir / "runtime_locks"
            if runtime_locks.exists():
                runtime_locks.rmdir()
            self._create_directory_alias(runtime_locks, outside)
            try:
                with self.assertRaisesRegex(RuntimeError, "runtime lock parent"):
                    register_history_writer_lease(data_dir, label="unsafe-parent")
            finally:
                self._remove_directory_alias(runtime_locks)

            self.assertEqual(list(outside.iterdir()), [])

    def test_lease_registry_alias_is_rejected_before_external_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            outside = Path(tmp) / "outside"
            create_default_config(data_dir)
            outside.mkdir()
            runtime_locks = data_dir / "runtime_locks"
            runtime_locks.mkdir(exist_ok=True)
            registry = runtime_locks / "history_writer_leases"
            self._create_directory_alias(registry, outside)
            try:
                with self.assertRaisesRegex(RuntimeError, "lease registry"):
                    register_history_writer_lease(data_dir, label="unsafe-registry")
            finally:
                self._remove_directory_alias(registry)

            self.assertEqual(list(outside.iterdir()), [])

    def test_owned_history_roots_are_deduplicated_and_stably_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "Alpha"
            second = Path(tmp) / "beta"
            create_default_config(first)
            create_default_config(second)
            inputs = [second / "nested", first, first / "other"]
            if os.name == "nt":
                inputs.append(Path(str(first).swapcase()))
                inputs.append(Path(f"\\\\?\\{first}"))

            roots = owned_history_roots(tuple(inputs))

            self.assertEqual(len(roots), 2)
            keys = [history_fence._history_fence_key(root) for root in roots]
            self.assertEqual(keys, sorted(keys))
            if os.name == "nt":
                self.assertEqual(
                    history_fence._history_fence_key(first),
                    history_fence._history_fence_key(Path(f"\\\\?\\{first}")),
                )

    def test_process_identity_reset_rebuilds_thread_local_state(self) -> None:
        previous_local = history_fence._LOCAL
        previous_local.depths = {"inherited": 1}

        history_fence._reset_lease_process_identity()

        self.assertIsNot(history_fence._LOCAL, previous_local)
        self.assertFalse(hasattr(history_fence._LOCAL, "depths"))

    def test_forked_child_from_active_fence_rejects_history_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            key = history_fence._history_fence_key(history_reset_fence_path(data_dir))
            history_fence._ACTIVE_PROCESS_FENCES[key] = history_fence._ActiveProcessFence(
                label="inherited",
                owner_thread_ident=threading.get_ident(),
                phase="active",
            )
            try:
                history_fence._reset_lease_process_identity()
                with self.assertRaisesRegex(RuntimeError, r"fork\+exec"):
                    with history_writer_fence(data_dir, label="child"):
                        pass
            finally:
                history_fence._reset_lease_process_identity()

    def test_forked_child_from_active_lease_rejects_history_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            history_fence._ACTIVE_LEASE_TOKENS.add("inherited-lease")
            try:
                history_fence._reset_lease_process_identity()
                with self.assertRaisesRegex(RuntimeError, r"fork\+exec"):
                    with history_writer_fence(data_dir, label="child"):
                        pass
            finally:
                history_fence._reset_lease_process_identity()

    def _create_directory_alias(self, alias: Path, destination: Path) -> None:
        if os.name != "nt":
            try:
                alias.symlink_to(destination, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")
            return
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(destination)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest(f"directory junctions are unavailable (exit {completed.returncode})")

    @staticmethod
    def _remove_directory_alias(alias: Path) -> None:
        try:
            if os.name == "nt":
                alias.rmdir()
            else:
                alias.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    unittest.main()

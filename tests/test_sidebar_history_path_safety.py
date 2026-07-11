from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.config.loader import create_default_config, persistent_config_dir, save_config
from app.personal_wechat_bot.control import sidebar_api
from app.personal_wechat_bot.control.audit import DISPOSABLE_ARTIFACTS, DISPOSABLE_DIRECTORIES
from app.personal_wechat_bot.domain.errors import ConfigError


class SidebarHistoryPathSafetyTest(unittest.TestCase):
    def test_config_loading_begins_only_after_config_update_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            real_lock = sidebar_api.config_update_lock
            real_load = sidebar_api.load_config
            lock_depth = 0

            @contextmanager
            def tracked_lock(root):
                nonlocal lock_depth
                with real_lock(root):
                    lock_depth += 1
                    try:
                        yield
                    finally:
                        lock_depth -= 1

            def checked_load(root):
                self.assertGreater(lock_depth, 0)
                return real_load(root)

            with mock.patch.object(sidebar_api, "config_update_lock", tracked_lock), mock.patch.object(
                sidebar_api,
                "load_config",
                side_effect=checked_load,
            ):
                result = sidebar_api.clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "ok")

    def test_data_root_alias_is_rejected_without_deleting_owned_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_data = Path(tmp) / "real-data"
            create_default_config(real_data)
            sentinel = real_data / "tool_outputs" / "keep.txt"
            sentinel.write_text("owned history", encoding="utf-8")
            alias = Path(tmp) / "data-alias"
            self._create_directory_alias(alias, real_data)

            try:
                with self.assertRaisesRegex(ValueError, "data_dir"):
                    sidebar_api.clear_sidebar_history_data(alias, {"source": "shutdown_helper"})

                self.assertEqual(sentinel.read_text(encoding="utf-8"), "owned history")
                self.assertTrue((real_data / "config.json").exists())
            finally:
                self._remove_directory_alias(alias)

    def test_unowned_data_directory_is_rejected_without_writes_or_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "unowned"
            sentinel = data_dir / "tool_outputs" / "keep.txt"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("unrelated business data", encoding="utf-8")

            with self.assertRaises(ConfigError):
                sidebar_api.clear_sidebar_history_data(data_dir, {"source": "shutdown_helper"})

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unrelated business data")
            self.assertFalse((data_dir / "config.json").exists())
            self.assertFalse((data_dir / "runtime").exists())
            self.assertFalse(persistent_config_dir(data_dir).exists())

    def test_symlinked_config_does_not_claim_an_unowned_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            owned_data = Path(tmp) / "owned"
            create_default_config(owned_data)
            unowned_data = Path(tmp) / "unowned"
            sentinel = unowned_data / "tool_outputs" / "keep.txt"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("unrelated data", encoding="utf-8")
            config_alias = unowned_data / "config.json"
            try:
                config_alias.symlink_to(owned_data / "config.json")
            except OSError as exc:
                self.skipTest(f"file symlinks are unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "config.json"):
                sidebar_api.clear_sidebar_history_data(unowned_data)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unrelated data")

    def test_hardlinked_config_does_not_claim_an_unowned_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            owned_data = Path(tmp) / "owned"
            create_default_config(owned_data)
            unowned_data = Path(tmp) / "unowned"
            sentinel = unowned_data / "tool_outputs" / "keep.txt"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("unrelated data", encoding="utf-8")
            os.link(owned_data / "config.json", unowned_data / "config.json")

            with self.assertRaisesRegex(ValueError, "private regular file"):
                sidebar_api.clear_sidebar_history_data(unowned_data)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unrelated data")

    def test_hardlinked_shutdown_status_is_rejected_before_external_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir()
            outside_status = Path(tmp) / "outside-status.json"
            outside_status.write_text("outside status", encoding="utf-8")
            os.link(outside_status, runtime_dir / "history_reset_shutdown.json")

            with self.assertRaisesRegex(
                sidebar_api.HistoryResetNotScheduledError,
                "must not be hardlinked",
            ):
                sidebar_api.clear_sidebar_history_data(data_dir, {"shutdown_processes": True})

            self.assertEqual(outside_status.read_text(encoding="utf-8"), "outside status")
            self.assertFalse((runtime_dir / "history_reset_shutdown.lock").exists())

    def test_hardlinked_sidebar_launch_state_is_rejected_before_helper_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir()
            outside_launch = Path(tmp) / "outside-launch.json"
            outside_launch.write_text("{}", encoding="utf-8")
            os.link(outside_launch, runtime_dir / "sidebar_launch.json")

            with mock.patch.object(sidebar_api.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(
                    sidebar_api.HistoryResetNotScheduledError,
                    "sidebar launch state must be private",
                ):
                    sidebar_api.clear_sidebar_history_data(data_dir, {"shutdown_processes": True})

            popen.assert_not_called()
            self.assertEqual(outside_launch.read_text(encoding="utf-8"), "{}")
            self.assertFalse((runtime_dir / "history_reset_shutdown.lock").exists())

    def test_shutdown_requires_current_launch_state_before_helper_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            with mock.patch.object(sidebar_api.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(RuntimeError, "launch state is required"):
                    sidebar_api.clear_sidebar_history_data(data_dir, {"shutdown_processes": True})

            popen.assert_not_called()
            runtime_dir = data_dir / "runtime"
            self.assertFalse((runtime_dir / "history_reset_shutdown.lock").exists())
            self.assertFalse((runtime_dir / "history_reset_shutdown.json").exists())

    def test_runtime_alias_is_rejected_before_shutdown_status_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            outside_runtime = Path(tmp) / "outside-runtime"
            outside_runtime.mkdir()
            outside_status = outside_runtime / "history_reset_shutdown.json"
            outside_status.write_text("outside status", encoding="utf-8")
            runtime_alias = data_dir / "runtime"
            self._create_directory_alias(runtime_alias, outside_runtime)

            try:
                with self.assertRaisesRegex(
                    sidebar_api.HistoryResetNotScheduledError,
                    "reparse point",
                ):
                    sidebar_api.clear_sidebar_history_data(data_dir, {"shutdown_processes": True})

                self.assertEqual(outside_status.read_text(encoding="utf-8"), "outside status")
                self.assertFalse((outside_runtime / "history_reset_shutdown.lock").exists())
            finally:
                self._remove_directory_alias(runtime_alias)

    def test_runtime_lock_alias_is_rejected_before_process_lock_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            outside_locks = Path(tmp) / "outside-locks"
            outside_locks.mkdir()
            outside_lock = outside_locks / "sidebar_agent_tick.lock"
            outside_lock.write_text("outside lock", encoding="utf-8")
            lock_alias = data_dir / "runtime_locks"
            self._create_directory_alias(lock_alias, outside_locks)

            try:
                with self.assertRaisesRegex(ValueError, "reparse point"):
                    sidebar_api.clear_sidebar_history_data(data_dir)

                self.assertEqual(outside_lock.read_text(encoding="utf-8"), "outside lock")
                self.assertFalse((outside_locks / "sidebar_agent_tick.lock.guard").exists())
            finally:
                self._remove_directory_alias(lock_alias)

    def test_shutdown_runtime_lock_alias_is_rejected_before_admission_fence_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            outside_locks = Path(tmp) / "outside-locks"
            outside_locks.mkdir()
            sentinel = outside_locks / "sentinel.txt"
            sentinel.write_text("outside lock data", encoding="utf-8")
            lock_alias = data_dir / "runtime_locks"
            self._create_directory_alias(lock_alias, outside_locks)

            try:
                with self.assertRaisesRegex(
                    sidebar_api.HistoryResetNotScheduledError,
                    "reparse point",
                ):
                    sidebar_api.clear_sidebar_history_data(data_dir, {"shutdown_processes": True})

                self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside lock data")
                self.assertFalse((outside_locks / "history_reset_fence.lock").exists())
                self.assertFalse((outside_locks / "history_reset_fence.lock.guard").exists())
            finally:
                self._remove_directory_alias(lock_alias)

    def test_history_clear_removes_all_declared_disposable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            for relative in DISPOSABLE_ARTIFACTS:
                target = data_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(f"disposable:{relative}".encode("utf-8"))
            for relative in DISPOSABLE_DIRECTORIES:
                target = data_dir / relative / "cache" / "entry.bin"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(f"disposable-dir:{relative}".encode("utf-8"))

            result = sidebar_api.clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "ok")
            removed = {item["relative_path"] for item in result["removed"]}
            self.assertTrue(set(DISPOSABLE_ARTIFACTS).issubset(removed))
            self.assertTrue(set(DISPOSABLE_DIRECTORIES).issubset(removed))
            for relative in DISPOSABLE_ARTIFACTS:
                self.assertFalse((data_dir / relative).exists())
            for relative in DISPOSABLE_DIRECTORIES:
                self.assertFalse((data_dir / relative).exists())

    def test_history_clear_removes_every_sqlite_wal_shm_and_journal_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            for relative in sidebar_api._HISTORY_RESET_SQLITE_PATHS:
                (data_dir / relative).write_bytes(f"stale:{relative}".encode("utf-8"))

            result = sidebar_api.clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "ok")
            removed = {item["relative_path"] for item in result["removed"]}
            self.assertTrue(set(sidebar_api._HISTORY_RESET_SQLITE_PATHS).issubset(removed))
            for relative in sidebar_api._HISTORY_RESET_SQLITE_PATHS:
                if relative.endswith(("-wal", "-shm", "-journal")):
                    self.assertFalse((data_dir / relative).exists())

    def test_history_clear_removes_every_rotated_plaintext_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            sentinel = b"ROTATED_HISTORY_SENTINEL_724b"
            for relative in sidebar_api._HISTORY_RESET_ROTATED_LOG_PATHS:
                (data_dir / relative).write_bytes(sentinel + relative.encode("ascii"))

            result = sidebar_api.clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "ok")
            removed = {item["relative_path"] for item in result["removed"]}
            self.assertEqual(
                set(sidebar_api._HISTORY_RESET_ROTATED_LOG_PATHS),
                set(sidebar_api._HISTORY_RESET_ROTATED_LOG_PATHS).intersection(removed),
            )
            for relative in sidebar_api._HISTORY_RESET_ROTATED_LOG_PATHS:
                self.assertFalse((data_dir / relative).exists(), relative)

    def test_history_clear_removes_known_atomic_write_orphans_and_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            sentinel = b"ATOMIC_HISTORY_SENTINEL_60ef"
            expected = set(sidebar_api._HISTORY_RESET_FIXED_ORPHAN_TMP_PATHS)
            for relative in sidebar_api._HISTORY_RESET_FIXED_ORPHAN_TMP_PATHS:
                target = data_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(sentinel + relative.encode("ascii"))
            for base in sidebar_api._HISTORY_RESET_UUID_TMP_BASES:
                target = data_dir / f"{base}.{'a' * 32}.tmp"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(sentinel + base.encode("ascii"))
                expected.add(target.relative_to(data_dir).as_posix())
            for relative in ("diagnostics", "native_diagnostics"):
                target = data_dir / relative / "history-snapshot.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(sentinel)
            near_miss = data_dir / "weflow_sessions.json.not-a-uuid.tmp"
            near_miss.write_bytes(b"unrecognized file")

            result = sidebar_api.clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "ok")
            removed = {Path(item["relative_path"]).as_posix() for item in result["removed"]}
            self.assertTrue(expected.issubset(removed))
            self.assertIn("diagnostics", removed)
            self.assertIn("native_diagnostics", removed)
            for relative in expected:
                self.assertFalse((data_dir / relative).exists(), relative)
            self.assertFalse((data_dir / "diagnostics").exists())
            self.assertFalse((data_dir / "native_diagnostics").exists())
            self.assertTrue(near_miss.exists())

    def test_hardlinked_atomic_write_orphan_blocks_before_any_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            outside = Path(tmp) / "outside-temp.json"
            outside.write_text("must remain", encoding="utf-8")
            orphan = data_dir / f"weflow_sessions.json.{'b' * 32}.tmp"
            os.link(outside, orphan)
            earlier_history = data_dir / "agent_workspace" / "history.txt"
            earlier_history.parent.mkdir()
            earlier_history.write_text("history", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "orphan temp must be private"):
                sidebar_api.clear_sidebar_history_data(data_dir)

            self.assertEqual(outside.read_text(encoding="utf-8"), "must remain")
            self.assertEqual(earlier_history.read_text(encoding="utf-8"), "history")

    def test_disposable_artifact_configured_as_api_key_blocks_before_any_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = create_default_config(data_dir)
            config.providers["chat"].api_key_file = "wechat_window.bmp"
            save_config(config)
            key_file = data_dir / "wechat_window.bmp"
            key_file.write_bytes(b"configured-key-material")
            early_history = data_dir / "agent_workspace" / "must-survive.txt"
            early_history.parent.mkdir()
            early_history.write_text("history", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "retained config path (?:contains|conflicts)"):
                sidebar_api.clear_sidebar_history_data(data_dir)

            self.assertEqual(key_file.read_bytes(), b"configured-key-material")
            self.assertEqual(early_history.read_text(encoding="utf-8"), "history")

    def test_history_reset_mutated_control_file_cannot_be_configured_api_key(self) -> None:
        for relative in ("weflow_bridge_state.json", "send_bridge/synced_acks.json"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                config = create_default_config(data_dir)
                config.providers["chat"].api_key_file = relative
                save_config(config)
                key_file = data_dir / relative
                key_file.parent.mkdir(parents=True, exist_ok=True)
                key_file.write_text("configured-key-material", encoding="utf-8")
                early_history = data_dir / "agent_workspace" / "must-survive.txt"
                early_history.parent.mkdir()
                early_history.write_text("history", encoding="utf-8")

                with self.assertRaisesRegex(ValueError, "retained config path (?:contains|conflicts)"):
                    sidebar_api.clear_sidebar_history_data(data_dir)

                self.assertEqual(key_file.read_text(encoding="utf-8"), "configured-key-material")
                self.assertEqual(early_history.read_text(encoding="utf-8"), "history")

    def test_hardlinked_disposable_artifact_only_unlinks_data_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            outside = Path(tmp) / "outside.bmp"
            outside.write_bytes(b"external-content")
            linked = data_dir / "wechat_window.bmp"
            os.link(outside, linked)

            result = sidebar_api.clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertFalse(linked.exists())
            self.assertEqual(outside.read_bytes(), b"external-content")

    def test_nested_disposable_directory_alias_is_rejected_before_any_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir()
            outside = Path(tmp) / "outside-profile"
            outside.mkdir()
            sentinel = outside / "keep.txt"
            sentinel.write_text("external profile", encoding="utf-8")
            alias = runtime_dir / "sidebar_browser_profile"
            self._create_directory_alias(alias, outside)
            early_history = data_dir / "agent_workspace" / "must-survive.txt"
            early_history.parent.mkdir()
            early_history.write_text("history", encoding="utf-8")

            try:
                with self.assertRaisesRegex(ValueError, "symlink or reparse point"):
                    sidebar_api.clear_sidebar_history_data(data_dir)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "external profile")
                self.assertEqual(early_history.read_text(encoding="utf-8"), "history")
            finally:
                self._remove_directory_alias(alias)

    def test_configured_api_key_inside_reset_tree_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = create_default_config(data_dir)
            config.providers["chat"].api_key_file = "agent_workspace/provider-keys.md"
            save_config(config)
            key_file = data_dir / "agent_workspace" / "provider-keys.md"
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_text("test-key", encoding="utf-8")
            history_file = key_file.parent / "history.txt"
            history_file.write_text("history beside configured credential", encoding="utf-8")
            nested_history = key_file.parent / "nested" / "history.txt"
            nested_history.parent.mkdir()
            nested_history.write_text("nested history", encoding="utf-8")

            result = sidebar_api.clear_sidebar_history_data(data_dir, {"source": "shutdown_helper"})

            self.assertEqual(result["status"], "ok")
            self.assertEqual(key_file.read_text(encoding="utf-8"), "test-key")
            self.assertFalse(history_file.exists())
            self.assertFalse(nested_history.exists())
            self.assertFalse(nested_history.parent.exists())
            self.assertIn(str(key_file.resolve()), result["retained_config"])
            removed = {item["relative_path"]: item["kind"] for item in result["removed"]}
            self.assertEqual(removed["agent_workspace"], "dir_contents")

    def test_configured_api_key_conflicting_with_reset_file_fails_before_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = create_default_config(data_dir)
            config.providers["chat"].api_key_file = "backend_events.jsonl"
            save_config(config)
            key_file = data_dir / "backend_events.jsonl"
            key_file.write_text("configured-key", encoding="utf-8")
            early_history = data_dir / "agent_workspace" / "must-survive.txt"
            early_history.parent.mkdir()
            early_history.write_text("history", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "history reset"):
                sidebar_api.clear_sidebar_history_data(data_dir)

            self.assertEqual(key_file.read_text(encoding="utf-8"), "configured-key")
            self.assertEqual(early_history.read_text(encoding="utf-8"), "history")

    def test_configured_api_key_conflict_fails_before_shutdown_is_scheduled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = create_default_config(data_dir)
            config.providers["chat"].api_key_file = "backend_events.jsonl"
            save_config(config)
            key_file = data_dir / "backend_events.jsonl"
            key_file.write_text("configured-key", encoding="utf-8")

            with mock.patch.object(sidebar_api, "_schedule_sidebar_history_reset_shutdown") as schedule:
                with self.assertRaisesRegex(
                    sidebar_api.HistoryResetNotScheduledError,
                    "history reset",
                ):
                    sidebar_api.clear_sidebar_history_data(data_dir, {"shutdown_processes": True})

            schedule.assert_not_called()
            self.assertEqual(key_file.read_text(encoding="utf-8"), "configured-key")

    def test_config_update_cannot_complete_during_history_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            history_file = data_dir / "agent_workspace" / "history.txt"
            history_file.parent.mkdir()
            history_file.write_text("history", encoding="utf-8")
            update_attempted = threading.Event()
            update_completed = threading.Event()
            updater: threading.Thread | None = None
            original_remove = sidebar_api._remove_history_path

            def update_provider() -> None:
                update_attempted.set()
                sidebar_api.update_config(
                    data_dir,
                    lambda config: setattr(
                        config.providers["chat"],
                        "api_key_file",
                        "replacement-keys.md",
                    ),
                )
                update_completed.set()

            def remove_while_contended(*args: object, **kwargs: object) -> None:
                nonlocal updater
                if updater is None:
                    updater = threading.Thread(target=update_provider, daemon=True)
                    updater.start()
                    self.assertTrue(update_attempted.wait(1.0))
                    self.assertFalse(update_completed.wait(0.15))
                original_remove(*args, **kwargs)

            with mock.patch.object(sidebar_api, "_remove_history_path", remove_while_contended):
                result = sidebar_api.clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertIsNotNone(updater)
            updater.join(timeout=2.0)
            self.assertFalse(updater.is_alive())
            self.assertTrue(update_completed.is_set())
            self.assertFalse(history_file.exists())

    def test_hardlinked_tolerant_log_is_rejected_before_any_delete(self) -> None:
        for relative in sidebar_api._HISTORY_RESET_LOCK_TOLERANT_FILES:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                create_default_config(data_dir)
                outside_log = Path(tmp) / f"outside-{Path(relative).name}"
                outside_log.write_text("outside log", encoding="utf-8")
                os.link(outside_log, data_dir / relative)
                early_history = data_dir / "agent_workspace" / "must-survive.txt"
                early_history.parent.mkdir()
                early_history.write_text("history", encoding="utf-8")

                with self.assertRaisesRegex(ValueError, "lock-tolerant file must be private"):
                    sidebar_api.clear_sidebar_history_data(data_dir)

                self.assertEqual(outside_log.read_text(encoding="utf-8"), "outside log")
                self.assertEqual(early_history.read_text(encoding="utf-8"), "history")

    def test_truncate_fallback_refuses_multiply_linked_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside_log = Path(tmp) / "outside.log"
            outside_log.write_text("outside log", encoding="utf-8")
            linked_log = Path(tmp) / "linked.log"
            os.link(outside_log, linked_log)
            record: dict[str, object] = {}

            sidebar_api._truncate_locked_history_file(linked_log, "weflow_process.err.log", record)

            self.assertEqual(record["fallback"], "retained")
            self.assertIn("private regular file", str(record["fallback_error"]))
            self.assertEqual(outside_log.read_text(encoding="utf-8"), "outside log")
            self.assertEqual(linked_log.read_text(encoding="utf-8"), "outside log")

    def test_writable_and_preserved_files_must_not_be_hardlinked(self) -> None:
        relatives = (
            *sidebar_api._HISTORY_CLEAR_WRITABLE_CONTROL_FILES,
            *sidebar_api._HISTORY_PRESERVED_RUNTIME_PATHS,
        )
        for relative in relatives:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                create_default_config(data_dir)
                outside_file = Path(tmp) / f"outside-{Path(relative).name}"
                outside_file.write_text("outside content", encoding="utf-8")
                target = data_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(outside_file, target)
                early_history = data_dir / "agent_workspace" / "must-survive.txt"
                early_history.parent.mkdir()
                early_history.write_text("history", encoding="utf-8")

                with self.assertRaisesRegex(ValueError, "must (?:be private and regular|not be hardlinked)"):
                    sidebar_api.clear_sidebar_history_data(data_dir)

                self.assertEqual(outside_file.read_text(encoding="utf-8"), "outside content")
                self.assertEqual(early_history.read_text(encoding="utf-8"), "history")

    def test_writable_and_preserved_file_symlinks_are_rejected(self) -> None:
        for relative in (
            "sidebar_state.sqlite",
            "weflow_sidebar_state.json",
            "send_bridge/outbox.jsonl",
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                create_default_config(data_dir)
                outside_file = Path(tmp) / f"outside-{Path(relative).name}"
                outside_file.write_text("outside content", encoding="utf-8")
                target = data_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    target.symlink_to(outside_file)
                except OSError as exc:
                    self.skipTest(f"file symlinks are unavailable: {exc}")

                with self.assertRaisesRegex(ValueError, "symlink or reparse point"):
                    sidebar_api.clear_sidebar_history_data(data_dir)

                self.assertEqual(outside_file.read_text(encoding="utf-8"), "outside content")

    def test_manifest_rejects_non_direct_reset_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            for relative in ("../outside", "runtime/nested"):
                with self.subTest(relative=relative), mock.patch.object(
                    sidebar_api,
                    "_HISTORY_RESET_DIRS",
                    (relative,),
                ), mock.patch.object(sidebar_api, "_HISTORY_RESET_FILES", ()):
                    with self.assertRaises(ValueError):
                        sidebar_api._validate_history_reset_manifest(data_dir)

    def test_top_level_aliases_fail_before_any_manifest_target_is_deleted(self) -> None:
        for destination_name in ("data_root", "send_bridge"):
            with self.subTest(destination=destination_name), tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                create_default_config(data_dir)
                early_target = data_dir / "agent_workspace" / "must-survive.txt"
                early_target.parent.mkdir(parents=True)
                early_target.write_text("prevalidation sentinel", encoding="utf-8")
                send_bridge = data_dir / "send_bridge"
                send_bridge.mkdir()
                bridge_sentinel = send_bridge / "must-survive.jsonl"
                bridge_sentinel.write_text("preserved bridge evidence", encoding="utf-8")
                destination = data_dir if destination_name == "data_root" else send_bridge
                alias = data_dir / "conversation_channels"
                self._create_directory_alias(alias, destination)

                try:
                    with self.assertRaisesRegex(ValueError, "symlink or reparse point"):
                        sidebar_api.clear_sidebar_history_data(data_dir, {"source": "shutdown_helper"})

                    self.assertEqual(early_target.read_text(encoding="utf-8"), "prevalidation sentinel")
                    self.assertEqual(bridge_sentinel.read_text(encoding="utf-8"), "preserved bridge evidence")
                    self.assertTrue((data_dir / "config.json").exists())
                finally:
                    self._remove_directory_alias(alias)

    def test_manifest_parent_alias_fails_before_earlier_targets_are_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            early_target = data_dir / "agent_workspace" / "must-survive.txt"
            early_target.parent.mkdir(parents=True)
            early_target.write_text("prevalidation sentinel", encoding="utf-8")
            outside_runtime = Path(tmp) / "outside-runtime"
            outside_runtime.mkdir()
            outside_state = outside_runtime / "agent_state.json"
            outside_state.write_text("outside state", encoding="utf-8")
            runtime_alias = data_dir / "runtime"
            self._create_directory_alias(runtime_alias, outside_runtime)

            try:
                with self.assertRaisesRegex(ValueError, "symlink or reparse point"):
                    sidebar_api.clear_sidebar_history_data(data_dir, {"source": "shutdown_helper"})

                self.assertEqual(early_target.read_text(encoding="utf-8"), "prevalidation sentinel")
                self.assertEqual(outside_state.read_text(encoding="utf-8"), "outside state")
            finally:
                self._remove_directory_alias(runtime_alias)

    def test_nested_alias_is_removed_without_touching_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            reset_tree = data_dir / "agent_workspace"
            read_only_dir = reset_tree / "ordinary-read-only"
            read_only_dir.mkdir(parents=True)
            read_only_file = read_only_dir / "artifact.txt"
            read_only_file.write_text("ordinary history", encoding="utf-8")
            read_only_file.chmod(stat.S_IREAD)
            read_only_dir.chmod(stat.S_IREAD)

            outside_dir = Path(tmp) / "outside"
            outside_dir.mkdir()
            outside_file = outside_dir / "sentinel.txt"
            outside_file.write_text("outside content", encoding="utf-8")
            outside_file.chmod(stat.S_IREAD)
            before_mode = outside_file.stat().st_mode
            before_attributes = int(getattr(outside_file.stat(), "st_file_attributes", 0) or 0)

            nested_alias = reset_tree / "nested" / "outside-alias"
            nested_alias.parent.mkdir()
            self._create_directory_alias(nested_alias, outside_dir)

            try:
                result = sidebar_api.clear_sidebar_history_data(data_dir, {"source": "shutdown_helper"})

                self.assertEqual(result["status"], "ok")
                self.assertFalse(reset_tree.exists())
                self.assertEqual(outside_file.read_text(encoding="utf-8"), "outside content")
                self.assertEqual(outside_file.stat().st_mode, before_mode)
                self.assertEqual(
                    int(getattr(outside_file.stat(), "st_file_attributes", 0) or 0),
                    before_attributes,
                )
            finally:
                self._remove_directory_alias(nested_alias)
                outside_file.chmod(stat.S_IWRITE | stat.S_IREAD)
                outside_dir.chmod(stat.S_IWRITE | stat.S_IREAD)

    @unittest.skipUnless(os.name == "nt", "Windows read-only hard-link semantics")
    def test_read_only_hard_link_does_not_change_external_file_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            outside_file = Path(tmp) / "outside.txt"
            outside_file.write_text("outside content", encoding="utf-8")
            linked_file = data_dir / "agent_workspace" / "linked.txt"
            linked_file.parent.mkdir(parents=True)
            os.link(outside_file, linked_file)
            outside_file.chmod(stat.S_IREAD)
            before_mode = outside_file.stat().st_mode
            before_attributes = int(getattr(outside_file.stat(), "st_file_attributes", 0) or 0)

            try:
                result = sidebar_api.clear_sidebar_history_data(data_dir, {"source": "shutdown_helper"})

                self.assertEqual(result["status"], "partial_error")
                self.assertEqual(outside_file.read_text(encoding="utf-8"), "outside content")
                self.assertEqual(outside_file.stat().st_mode, before_mode)
                self.assertEqual(
                    int(getattr(outside_file.stat(), "st_file_attributes", 0) or 0),
                    before_attributes,
                )
                self.assertTrue(linked_file.exists())
            finally:
                outside_file.chmod(stat.S_IWRITE | stat.S_IREAD)

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
            path_stat = alias.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISDIR(path_stat.st_mode):
            alias.rmdir()
        else:
            alias.unlink()


if __name__ == "__main__":
    unittest.main()

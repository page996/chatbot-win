from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import scripts.start_sidebar_frontend as starter
from app.personal_wechat_bot.control import cli
from app.personal_wechat_bot.runtime.process_lock import ProcessLockError


class StartSidebarFrontendTest(unittest.TestCase):
    def test_frontend_lifecycle_lock_is_held_while_server_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            held = False

            @contextmanager
            def frontend_lock(_data_dir):
                nonlocal held
                held = True
                try:
                    yield
                finally:
                    held = False

            @contextmanager
            def weflow_lock(_data_dir, *, wait_timeout_seconds):
                yield

            def run_server(*args, **kwargs):
                self.assertTrue(held)

            with (
                mock.patch.object(starter, "_history_reset_in_progress", return_value=False),
                mock.patch.object(starter, "_sidebar_frontend_lifecycle_lock", side_effect=frontend_lock),
                mock.patch.object(starter, "_weflow_lifecycle_lock", side_effect=weflow_lock),
                mock.patch.object(starter, "_write_sidebar_launch_state"),
                mock.patch.object(starter, "ensure_send_bridge_worker", return_value={"status": "skipped"}),
                mock.patch(
                    "app.personal_wechat_bot.control.sidebar_server.run_sidebar_server",
                    side_effect=run_server,
                ) as server,
            ):
                result = starter.main(
                    ["--data-dir", str(data_dir), "--mode", "server", "--weflow", "off"]
                )

            self.assertEqual(result, 0)
            self.assertFalse(held)
            server.assert_called_once()

    def test_duplicate_frontend_does_not_overwrite_live_launch_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True)
            launch_path = runtime_dir / "sidebar_launch.json"
            launch_path.write_text('{"pid": 123, "process_start": "first"}', encoding="utf-8")

            @contextmanager
            def contended(_data_dir):
                raise ProcessLockError("frontend already running", holder={"pid": 123})
                yield

            with (
                mock.patch.object(starter, "_history_reset_in_progress", return_value=False),
                mock.patch.object(starter, "_sidebar_frontend_lifecycle_lock", side_effect=contended),
                mock.patch.object(starter, "_write_sidebar_launch_state") as write_launch,
                mock.patch(
                    "app.personal_wechat_bot.control.sidebar_server.run_sidebar_server"
                ) as server,
            ):
                result = starter.main(
                    ["--data-dir", str(data_dir), "--mode", "server", "--port", "9876", "--weflow", "off"]
                )

            self.assertEqual(result, 3)
            self.assertEqual(launch_path.read_text(encoding="utf-8"), '{"pid": 123, "process_start": "first"}')
            write_launch.assert_not_called()
            server.assert_not_called()

    def test_reset_precheck_does_not_wait_for_frontend_lifecycle_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            starter,
            "_history_reset_in_progress",
            return_value=True,
        ), mock.patch.object(starter, "_sidebar_frontend_lifecycle_lock") as frontend_lock:
            result = starter.main(["--data-dir", str(Path(tmp) / "data"), "--weflow", "off"])

        self.assertEqual(result, 3)
        frontend_lock.assert_not_called()

    def test_send_sidebar_cli_uses_guarded_startup_entrypoint(self) -> None:
        with mock.patch.object(starter, "main", return_value=0) as start:
            cli.main(["--data-dir", "isolated-data", "send-sidebar", "--host", "127.0.0.1", "--port", "8899"])

        start.assert_called_once_with(
            [
                "--data-dir",
                "isolated-data",
                "--mode",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                "8899",
                "--weflow",
                "off",
            ]
        )

    def test_send_sidebar_window_cli_uses_guarded_startup_entrypoint(self) -> None:
        with mock.patch.object(starter, "main", return_value=0) as start:
            cli.main(["--data-dir", "isolated-data", "send-sidebar-window", "--interval-ms", "1750"])

        start.assert_called_once_with(
            [
                "--data-dir",
                "isolated-data",
                "--mode",
                "window",
                "--interval-ms",
                "1750",
                "--weflow",
                "off",
            ]
        )

    def test_weflow_start_lock_prevents_duplicate_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            weflow_dir = root / "WeFlow"
            (weflow_dir / "node_modules").mkdir(parents=True)
            (weflow_dir / "package.json").write_text("{}", encoding="utf-8")
            lock_path = data_dir / "runtime" / "weflow_start.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(
                json.dumps({"pid": 7777, "process_start": "start-7777", "updated_at_epoch": time.time()}),
                encoding="utf-8",
            )

            with mock.patch.object(starter, "WEFLOW_DIR", weflow_dir), mock.patch.object(
                starter.shutil, "which", return_value="npm.cmd"
            ), mock.patch.object(starter, "weflow_token", return_value="token"), mock.patch.object(
                starter, "weflow_health", return_value={"status": "error", "message": "not ready"}
            ), mock.patch.object(
                starter, "_pid_exists", return_value=True
            ), mock.patch.object(
                starter, "process_start_marker", return_value="start-7777"
            ), mock.patch.object(
                starter.subprocess, "Popen"
            ) as popen:
                result = starter.ensure_weflow_started(
                    data_dir=data_dir,
                    host="127.0.0.1",
                    port=5031,
                    install_deps="auto",
                    wait_seconds=1,
                    required=False,
                    hidden=True,
                )

            popen.assert_not_called()
            self.assertEqual(result["status"], "starting")
            self.assertEqual(result["state"], "start_in_progress")
            self.assertEqual(result["pid"], 7777)

    def test_old_weflow_start_lock_with_matching_live_owner_remains_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "weflow_start.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 7777,
                        "process_start": "start-7777",
                        "updated_at_epoch": time.time() - 7200,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(starter, "weflow_health", return_value={"status": "error"}), mock.patch.object(
                starter, "_pid_exists", return_value=True
            ), mock.patch.object(starter, "process_start_marker", return_value="start-7777"), mock.patch.object(
                starter.time, "monotonic", side_effect=[0.0, 2.0]
            ), mock.patch.object(starter.time, "sleep"):
                result = starter._existing_weflow_start(
                    lock_path,
                    base_url="http://127.0.0.1:5031",
                    token="token",
                    wait_seconds=1.0,
                )

            self.assertIsNotNone(result)
            self.assertEqual(result["state"], "start_in_progress")
            self.assertTrue(lock_path.exists())

    def test_old_weflow_start_lock_marker_query_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "weflow_start.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 7777,
                        "process_start": "start-7777",
                        "updated_at_epoch": time.time() - 7200,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(starter, "weflow_health", return_value={"status": "error"}), mock.patch.object(
                starter, "_pid_exists", return_value=True
            ), mock.patch.object(starter, "process_start_marker", return_value=""):
                result = starter._existing_weflow_start(
                    lock_path,
                    base_url="http://127.0.0.1:5031",
                    token="token",
                    wait_seconds=1.0,
                )

            self.assertIsNotNone(result)
            self.assertEqual(result["state"], "start_identity_unverified")
            self.assertTrue(lock_path.exists())

    def test_stale_weflow_start_lock_is_replaced_by_single_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            weflow_dir = root / "WeFlow"
            (weflow_dir / "node_modules").mkdir(parents=True)
            (weflow_dir / "package.json").write_text("{}", encoding="utf-8")
            lock_path = data_dir / "runtime" / "weflow_start.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(json.dumps({"pid": 7777, "updated_at_epoch": time.time() - 999}), encoding="utf-8")
            process = mock.Mock(pid=8888)
            process.poll.return_value = None

            with mock.patch.object(starter, "WEFLOW_DIR", weflow_dir), mock.patch.object(
                starter.shutil, "which", return_value="npm.cmd"
            ), mock.patch.object(starter, "weflow_token", return_value="token"), mock.patch.object(
                starter, "weflow_health", return_value={"status": "error", "message": "not ready"}
            ), mock.patch.object(
                starter, "_pid_exists", return_value=False
            ), mock.patch.object(
                starter, "process_start_marker", return_value="start-8888"
            ), mock.patch.object(
                starter.subprocess, "Popen", return_value=process
            ) as popen:
                result = starter.ensure_weflow_started(
                    data_dir=data_dir,
                    host="127.0.0.1",
                    port=5031,
                    install_deps="auto",
                    wait_seconds=1,
                    required=False,
                    hidden=True,
                )

            popen.assert_called_once()
            self.assertEqual(result["status"], "starting")
            self.assertEqual(result["state"], "launched_waiting_for_health")
            self.assertEqual(result["pid"], 8888)
            self.assertEqual(result["process_start"], "start-8888")
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(lock["process_start"], "start-8888")
            self.assertTrue(lock_path.exists())

    def test_fresh_start_lock_with_reused_pid_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            weflow_dir = root / "WeFlow"
            (weflow_dir / "node_modules").mkdir(parents=True)
            (weflow_dir / "package.json").write_text("{}", encoding="utf-8")
            lock_path = data_dir / "runtime" / "weflow_start.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(
                json.dumps({"pid": 7777, "process_start": "old-start", "updated_at_epoch": time.time()}),
                encoding="utf-8",
            )
            process = mock.Mock(pid=8888)
            process.poll.return_value = None

            def marker(pid: int) -> str:
                return "reused-start" if pid == 7777 else "start-8888"

            with mock.patch.object(starter, "WEFLOW_DIR", weflow_dir), mock.patch.object(
                starter.shutil, "which", return_value="npm.cmd"
            ), mock.patch.object(starter, "weflow_token", return_value="token"), mock.patch.object(
                starter, "weflow_health", return_value={"status": "error", "message": "not ready"}
            ), mock.patch.object(starter, "_pid_exists", return_value=True), mock.patch.object(
                starter, "process_start_marker", side_effect=marker
            ), mock.patch.object(starter.subprocess, "Popen", return_value=process) as popen:
                result = starter.ensure_weflow_started(
                    data_dir=data_dir,
                    host="127.0.0.1",
                    port=5031,
                    install_deps="auto",
                    wait_seconds=1,
                    required=False,
                    hidden=True,
                )

            popen.assert_called_once()
            self.assertEqual(result["pid"], 8888)

    def test_history_reset_lock_blocks_weflow_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True)
            runtime_dir.joinpath("history_reset_shutdown.lock").write_text(
                json.dumps(
                    {
                        "helper_pid": 7777,
                        "helper_process_start": "start-7777",
                        "updated_at_epoch": time.time(),
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(starter, "_pid_exists", return_value=True), mock.patch.object(
                starter, "process_start_marker", return_value="start-7777"
            ), mock.patch.object(starter.subprocess, "Popen") as popen:
                result = starter.ensure_weflow_started(
                    data_dir=data_dir,
                    host="127.0.0.1",
                    port=5031,
                    install_deps="auto",
                    wait_seconds=1,
                    required=False,
                    hidden=True,
                )

            popen.assert_not_called()
            self.assertEqual(result["state"], "history_reset_in_progress")

    def test_old_history_reset_lock_with_matching_live_helper_remains_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True)
            runtime_dir.joinpath("history_reset_shutdown.lock").write_text(
                json.dumps(
                    {
                        "helper_pid": 7777,
                        "helper_process_start": "start-7777",
                        "updated_at_epoch": time.time() - 7200,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(starter, "_pid_exists", return_value=True), mock.patch.object(
                starter, "process_start_marker", return_value="start-7777"
            ):
                active = starter._history_reset_in_progress(data_dir)

            self.assertTrue(active)

    def test_history_reset_live_helper_marker_query_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True)
            runtime_dir.joinpath("history_reset_shutdown.lock").write_text(
                json.dumps(
                    {
                        "helper_pid": 7777,
                        "helper_process_start": "start-7777",
                        "updated_at_epoch": time.time() - 7200,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(starter, "_pid_exists", return_value=True), mock.patch.object(
                starter, "process_start_marker", return_value=""
            ):
                active = starter._history_reset_in_progress(data_dir)

            self.assertTrue(active)

    def test_orphan_history_reset_status_reconciles_helper_identity(self) -> None:
        cases = (
            ("matching_live_helper", True, "start-7777", True),
            ("dead_helper", False, "", False),
            ("reused_pid", True, "different-start", False),
            ("marker_unavailable", True, "", True),
        )
        for name, pid_alive, current_start, expected_active in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                runtime_dir = data_dir / "runtime"
                runtime_dir.mkdir(parents=True)
                runtime_dir.joinpath("history_reset_shutdown.json").write_text(
                    json.dumps(
                        {
                            "status": "running",
                            "phase": "clearing_history",
                            "helper_pid": 7777,
                            "helper_process_start": "start-7777",
                        }
                    ),
                    encoding="utf-8",
                )

                with mock.patch.object(starter, "_pid_exists", return_value=pid_alive), mock.patch.object(
                    starter,
                    "process_start_marker",
                    return_value=current_start,
                ):
                    active = starter._history_reset_in_progress(data_dir)

                self.assertEqual(active, expected_active)

    def test_orphan_history_reset_status_fails_closed_when_unverifiable(self) -> None:
        cases = (
            ("corrupt_json", "{not-json", True),
            ("non_object_json", "[]", True),
            ("unknown_status", json.dumps({"status": "mystery"}), True),
            ("missing_helper_identity", json.dumps({"status": "running"}), True),
            ("terminal_status", json.dumps({"status": "ok"}), False),
        )
        for name, status_text, expected_active in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                runtime_dir = data_dir / "runtime"
                runtime_dir.mkdir(parents=True)
                runtime_dir.joinpath("history_reset_shutdown.json").write_text(status_text, encoding="utf-8")

                self.assertEqual(starter._history_reset_in_progress(data_dir), expected_active)

    def test_orphan_history_reset_status_blocks_weflow_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True)
            runtime_dir.joinpath("history_reset_shutdown.json").write_text(
                json.dumps(
                    {
                        "status": "running",
                        "phase": "clearing_history",
                        "helper_pid": 7777,
                        "helper_process_start": "start-7777",
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(starter, "_pid_exists", return_value=True), mock.patch.object(
                starter,
                "process_start_marker",
                return_value="start-7777",
            ), mock.patch.object(starter.subprocess, "Popen") as popen:
                result = starter.ensure_weflow_started(
                    data_dir=data_dir,
                    host="127.0.0.1",
                    port=5031,
                    install_deps="auto",
                    wait_seconds=1,
                    required=True,
                    hidden=True,
                )

            popen.assert_not_called()
            self.assertEqual(result["state"], "history_reset_in_progress")

    def test_malformed_history_reset_lock_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True)
            runtime_dir.joinpath("history_reset_shutdown.lock").write_text("{", encoding="utf-8")

            self.assertTrue(starter._history_reset_in_progress(data_dir))

    def test_hardlinked_history_reset_lock_is_fail_closed_without_external_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True)
            outside = Path(tmp) / "outside.lock"
            outside.write_text("outside content", encoding="utf-8")
            os.link(outside, runtime_dir / "history_reset_shutdown.lock")

            self.assertTrue(starter._history_reset_in_progress(data_dir))
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside content")

    def test_already_running_preserves_valid_launcher_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True)
            runtime_dir.joinpath("sidebar_launch.json").write_text(
                json.dumps({"weflow_pid": 7777, "weflow_process_start": "start-7777"}),
                encoding="utf-8",
            )
            with mock.patch.object(starter, "weflow_health", return_value={"status": "ok"}), mock.patch.object(
                starter, "_pid_exists", return_value=True
            ), mock.patch.object(starter, "process_start_marker", return_value="start-7777"):
                result = starter.ensure_weflow_started(
                    data_dir=data_dir,
                    host="127.0.0.1",
                    port=5031,
                    install_deps="auto",
                    wait_seconds=1,
                    required=False,
                    hidden=True,
                )

            self.assertEqual(result["state"], "already_running")
            self.assertEqual(result["pid"], 7777)
            self.assertEqual(result["process_start"], "start-7777")

    def test_start_lock_owner_cannot_remove_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "weflow_start.lock"
            lock_path.write_text(json.dumps({"owner_token": "new-owner", "pid": 2}), encoding="utf-8")

            removed = starter._remove_weflow_start_lock(lock_path, expected_owner_token="old-owner")

            self.assertFalse(removed)
            self.assertTrue(lock_path.exists())

    def test_sidebar_launch_state_records_sidebar_process_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            args = starter.build_parser().parse_args(["--data-dir", str(data_dir), "--weflow", "off"])
            with mock.patch.object(starter, "process_start_marker", return_value="sidebar-start"):
                starter._write_sidebar_launch_state(data_dir, args, {"status": "skipped"})

            payload = json.loads((data_dir / "runtime" / "sidebar_launch.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["process_start"], "sidebar-start")
            self.assertFalse(payload["browser_owned"])
            self.assertEqual(
                payload["browser_profile"],
                str((data_dir / "runtime" / "sidebar_browser_profile").resolve()),
            )

    def test_browser_launch_identity_is_atomically_merged_into_launch_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            args = starter.build_parser().parse_args(["--data-dir", str(data_dir), "--weflow", "off"])
            browser_pid = 7001

            def marker(pid: int) -> str:
                return "browser-start" if pid == browser_pid else "parent-start"

            with mock.patch.object(starter, "process_start_marker", side_effect=marker):
                starter._write_sidebar_launch_state(data_dir, args, {"status": "skipped"})
                starter._merge_sidebar_browser_launch_state(
                    data_dir,
                    {
                        "browser_status": "ok",
                        "browser_pid": browser_pid,
                        "browser_process_start": "browser-start",
                        "browser_executable": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                        "browser_profile": str((data_dir / "runtime" / "sidebar_browser_profile").resolve()),
                        "browser_owned": True,
                        "browser_job_owned": True,
                        "browser_descendants": [
                            {
                                "pid": browser_pid,
                                "process_start": "browser-start",
                                "executable": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                            }
                        ],
                        "browser_url": "http://127.0.0.1:8123/",
                    },
                )

            payload = json.loads((data_dir / "runtime" / "sidebar_launch.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["browser_owned"])
            self.assertEqual(payload["browser_pid"], browser_pid)
            self.assertEqual(payload["browser_process_start"], "browser-start")

    def test_browser_launch_merge_rejects_reused_pid_and_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            args = starter.build_parser().parse_args(["--data-dir", str(data_dir), "--weflow", "off"])
            with mock.patch.object(starter, "process_start_marker", return_value="parent-start"):
                starter._write_sidebar_launch_state(data_dir, args, {"status": "skipped"})
            launch_path = data_dir / "runtime" / "sidebar_launch.json"
            before = launch_path.read_text(encoding="utf-8")
            with mock.patch.object(
                starter,
                "process_start_marker",
                side_effect=lambda pid: "reused-browser" if pid == 7001 else "parent-start",
            ):
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    starter._merge_sidebar_browser_launch_state(
                        data_dir,
                        {
                            "browser_status": "ok",
                            "browser_pid": 7001,
                            "browser_process_start": "browser-start",
                            "browser_executable": r"C:\Chrome\chrome.exe",
                            "browser_profile": str((data_dir / "runtime" / "sidebar_browser_profile").resolve()),
                            "browser_owned": True,
                            "browser_job_owned": True,
                            "browser_descendants": [
                                {
                                    "pid": 7001,
                                    "process_start": "browser-start",
                                    "executable": r"C:\Chrome\chrome.exe",
                                }
                            ],
                        },
                    )

            self.assertEqual(launch_path.read_text(encoding="utf-8"), before)

    def test_window_lifecycle_passes_atomic_browser_state_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()

            @contextmanager
            def lock(*args, **kwargs):
                yield

            def run_window(*args, **kwargs):
                kwargs["browser_state_callback"]({"browser_owned": False})

            with mock.patch.object(starter, "_history_reset_in_progress", return_value=False), mock.patch.object(
                starter, "_sidebar_frontend_lifecycle_lock", side_effect=lock
            ), mock.patch.object(starter, "_weflow_lifecycle_lock", side_effect=lock), mock.patch.object(
                starter, "_write_sidebar_launch_state"
            ), mock.patch.object(starter, "ensure_send_bridge_worker", return_value={"status": "skipped"}), mock.patch.object(
                starter, "_merge_sidebar_browser_launch_state"
            ) as merge, mock.patch(
                "app.personal_wechat_bot.control.sidebar_window.run_sidebar_window",
                side_effect=run_window,
            ):
                result = starter.main(
                    ["--data-dir", str(data_dir), "--mode", "window", "--weflow", "off"]
                )

            self.assertEqual(result, 0)
            merge.assert_called_once_with(data_dir.resolve(), {"browser_owned": False})

    def test_sidebar_launch_state_is_atomically_replaced_with_private_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True)
            launch_path = runtime_dir / "sidebar_launch.json"
            external = Path(tmp) / "external.json"
            external.write_text("external-sentinel", encoding="utf-8")
            try:
                launch_path.hardlink_to(external)
            except OSError as exc:
                self.skipTest(f"hardlinks unavailable: {exc}")
            args = starter.build_parser().parse_args(["--data-dir", str(data_dir), "--weflow", "off"])
            real_replace = starter.os.replace
            observed: dict[str, object] = {}

            def checked_replace(source: str | Path, destination: str | Path) -> None:
                source_path = Path(source)
                destination_path = Path(destination)
                observed["old_content"] = destination_path.read_text(encoding="utf-8")
                observed["tmp_nlink"] = source_path.stat().st_nlink
                real_replace(source_path, destination_path)

            with mock.patch.object(starter, "process_start_marker", return_value="sidebar-start"), mock.patch.object(
                starter.os, "replace", side_effect=checked_replace
            ):
                starter._write_sidebar_launch_state(data_dir, args, {"status": "skipped"})

            self.assertEqual(observed["old_content"], "external-sentinel")
            self.assertEqual(observed["tmp_nlink"], 1)
            self.assertEqual(external.read_text(encoding="utf-8"), "external-sentinel")
            self.assertEqual(launch_path.stat().st_nlink, 1)
            self.assertEqual(json.loads(launch_path.read_text(encoding="utf-8"))["process_start"], "sidebar-start")
            self.assertEqual(list(runtime_dir.glob(".sidebar_launch.json.*.tmp")), [])

    def test_sidebar_launch_state_identity_failure_preserves_existing_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True)
            launch_path = runtime_dir / "sidebar_launch.json"
            launch_path.write_text("existing-state", encoding="utf-8")
            args = starter.build_parser().parse_args(["--data-dir", str(data_dir), "--weflow", "off"])

            with mock.patch.object(starter, "process_start_marker", return_value=""):
                with self.assertRaisesRegex(RuntimeError, "sidebar process identity is unavailable"):
                    starter._write_sidebar_launch_state(data_dir, args, {"status": "skipped"})

            self.assertEqual(launch_path.read_text(encoding="utf-8"), "existing-state")
            self.assertEqual(list(runtime_dir.glob(".sidebar_launch.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()

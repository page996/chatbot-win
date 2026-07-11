from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import scripts.sidebar_history_reset_shutdown as helper
from app.personal_wechat_bot.config.loader import create_default_config
from app.personal_wechat_bot.control import sidebar_api


class SidebarHistoryResetShutdownHelperTest(unittest.TestCase):
    def test_browser_preflight_binds_launch_state_to_sidebar_parent(self) -> None:
        status = {
            "verified": True,
            "inventory_verified": True,
            "launch_state": {
                "pid": 1234,
                "process_start": "parent:start",
                "browser_owned": True,
            },
            "recorded_process": {"state": "verified"},
            "profile_processes": [{"pid": 7001, "identity_verified": True}],
            "browser_process_tree": [{"pid": 7001, "identity_verified": True}],
        }
        with mock.patch.object(helper, "inspect_sidebar_browser_runtime", return_value=status):
            matched = helper._preflight_sidebar_browser(
                Path("data"),
                parent_pid=1234,
                parent_process_start="parent:start",
            )
            reused = helper._preflight_sidebar_browser(
                Path("data"),
                parent_pid=1234,
                parent_process_start="parent:reused",
            )

        self.assertTrue(matched["verified_identity"])
        self.assertFalse(reused["verified_identity"])
        self.assertEqual(reused["state"], "launch_state_parent_identity_mismatch")

    def test_verified_sidebar_browser_is_rechecked_terminated_and_reinventoried(self) -> None:
        process = {
            "pid": 7001,
            "process_start": "browser:start",
            "image": r"C:\Chrome\chrome.exe",
            "identity_verified": True,
            "argv": [r"C:\Chrome\chrome.exe", r"--user-data-dir=C:\data\runtime\sidebar_browser_profile"],
        }
        active = {
            "verified": True,
            "inventory_verified": True,
            "profile_processes": [process],
            "browser_process_tree": [process],
        }
        stopped = {
            "verified": True,
            "inventory_verified": True,
            "profile_processes": [],
            "browser_process_tree": [],
        }
        with mock.patch.object(
            helper,
            "inspect_sidebar_browser_runtime",
            side_effect=[active, stopped],
        ), mock.patch.object(
            helper, "process_start_marker", return_value="browser:start"
        ), mock.patch.object(
            helper, "_terminate_pid", return_value={"attempted": True}
        ) as terminate, mock.patch.object(
            helper, "_termination_exited", return_value=True
        ), mock.patch.object(helper.time, "sleep"):
            result = helper._stop_sidebar_browser_profile_processes(Path("C:/data"))

        self.assertTrue(result["verified_stopped"])
        terminate.assert_called_once_with(
            7001,
            tree=False,
            expected_process_start="browser:start",
        )

    def test_reused_browser_pid_is_not_terminated(self) -> None:
        process = {
            "pid": 7001,
            "process_start": "browser:original",
            "image": r"C:\Chrome\chrome.exe",
            "identity_verified": True,
        }
        with mock.patch.object(
            helper,
            "inspect_sidebar_browser_runtime",
            return_value={
                "verified": True,
                "inventory_verified": True,
                "profile_processes": [process],
                "browser_process_tree": [process],
            },
        ), mock.patch.object(
            helper, "process_start_marker", return_value="browser:reused"
        ), mock.patch.object(helper, "_terminate_pid") as terminate:
            result = helper._stop_sidebar_browser_profile_processes(Path("C:/data"))

        self.assertFalse(result["verified_stopped"])
        self.assertEqual(result["state"], "browser_process_identity_changed_before_terminate")
        terminate.assert_not_called()

    def test_remaining_profile_process_fails_shutdown_verification(self) -> None:
        process = {
            "pid": 7001,
            "process_start": "browser:start",
            "image": r"C:\Chrome\chrome.exe",
            "identity_verified": True,
        }
        active = {
            "verified": True,
            "inventory_verified": True,
            "profile_processes": [process],
            "browser_process_tree": [process],
        }
        with mock.patch.object(
            helper, "inspect_sidebar_browser_runtime", return_value=active
        ), mock.patch.object(
            helper, "process_start_marker", return_value="browser:start"
        ), mock.patch.object(
            helper, "_terminate_pid", return_value={"attempted": True}
        ), mock.patch.object(
            helper, "_termination_exited", return_value=True
        ), mock.patch.object(helper.time, "sleep"):
            result = helper._stop_sidebar_browser_profile_processes(Path("C:/data"))

        self.assertFalse(result["verified_stopped"])
        self.assertEqual(result["state"], "profile_processes_remain")

    def test_root_exit_does_not_hide_recorded_renderer_descendant(self) -> None:
        executable = r"C:\Chrome\chrome.exe"
        root = {
            "pid": 7001,
            "parent_pid": 1234,
            "process_start": "root:start",
            "image": executable,
            "identity_verified": True,
            "tree_depth": 0,
        }
        renderer = {
            "pid": 7002,
            "parent_pid": 7001,
            "process_start": "renderer:start",
            "image": executable,
            "identity_verified": True,
            "tree_depth": 1,
        }
        current_renderer = {
            "pid": 7002,
            "parent_pid": 4,
            "image": executable,
            "argv": [executable, "--type=renderer"],
        }

        with mock.patch.object(
            helper, "sidebar_browser_process_inventory", return_value=[current_renderer]
        ), mock.patch.object(
            helper,
            "process_start_marker",
            side_effect=lambda pid: "renderer:start" if pid == 7002 else "",
        ), mock.patch.object(
            helper, "_pid_state", return_value=False
        ), mock.patch.object(
            helper, "_terminate_pid", return_value={"attempted": True}
        ) as terminate, mock.patch.object(
            helper, "_termination_exited", return_value=True
        ), mock.patch.object(
            helper,
            "inspect_sidebar_browser_runtime",
            return_value={
                "verified": True,
                "inventory_verified": True,
                "profile_processes": [],
                "browser_process_tree": [],
            },
        ):
            result = helper._stop_sidebar_browser_profile_processes(
                Path("C:/data"),
                expected_processes=(root, renderer),
            )

        self.assertTrue(result["verified_stopped"])
        terminate.assert_called_once_with(
            7002,
            tree=False,
            expected_process_start="renderer:start",
        )

    def test_helper_stops_processes_clears_history_and_does_not_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            stale_weflow_lock = data_dir / "runtime" / "weflow_start.lock"
            stale_weflow_lock.parent.mkdir(parents=True, exist_ok=True)
            stale_weflow_lock.write_text(json.dumps({"pid": 5678}), encoding="utf-8")
            calls: list[str] = []

            def fake_clear(path: Path, payload: dict[str, object] | None = None) -> dict[str, object]:
                calls.append("clear")
                self.assertEqual(Path(path), data_dir.resolve())
                self.assertIsNone(payload)
                return {"status": "ok", "removed_count": 3}

            with mock.patch.object(
                helper,
                "_preflight_sidebar_parent",
                return_value={"verified_identity": True, "creation_date": "start-a"},
            ), mock.patch.object(
                helper, "_preflight_sidebar_browser", return_value={"verified_identity": True}
            ), mock.patch.object(
                helper, "_verify_shutdown_authorization", return_value={"authorized": True}
            ), mock.patch.object(helper, "_close_sidebar_windows", side_effect=lambda *args: calls.append("close_sidebar")), mock.patch.object(
                helper,
                "_stop_weflow",
                side_effect=lambda pid, port, process_start: calls.append(f"stop_weflow:{pid}:{port}")
                or {
                    "verified_stopped": True,
                    "port_released": True,
                    "remaining_port_pids": [],
                    "terminated_pids": [pid],
                },
            ), mock.patch.object(
                helper,
                "_stop_sidebar_parent",
                side_effect=lambda pid, data_dir, expected_creation_date, expected_process_start: calls.append(
                    f"stop_parent:{pid}"
                )
                or {"verified_stopped": True, "state": "stopped", "pid": pid},
            ), mock.patch.object(
                helper,
                "_stop_sidebar_browser_profile_processes",
                side_effect=lambda data_dir, **kwargs: calls.append("stop_browser") or {"verified_stopped": True},
            ), mock.patch.object(
                sidebar_api, "clear_sidebar_history_data", side_effect=fake_clear
            ):
                code = helper.main(
                    [
                        "--data-dir",
                        str(data_dir),
                        "--parent-pid",
                        "1234",
                        "--parent-process-start",
                        "parent-start",
                        "--shutdown-owner-token",
                        "owner-token",
                        "--weflow",
                        "on",
                        "--weflow-pid",
                        "5678",
                        "--weflow-port",
                        "5031",
                        "--response-delay-seconds",
                        "0.1",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertEqual(
                calls,
                [
                    "close_sidebar",
                    "stop_parent:1234",
                    "stop_browser",
                    "stop_weflow:5678:5031",
                    "clear",
                ],
            )
            status = json.loads((data_dir / "runtime" / "history_reset_shutdown.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "ok")
            self.assertEqual(status["phase"], "stopped_after_clear")
            self.assertTrue(status["manual_reopen_required"])
            self.assertNotIn("restart_result", status)
            self.assertFalse(stale_weflow_lock.exists())

    def test_helper_does_not_clear_when_weflow_shutdown_is_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            with mock.patch.object(
                helper,
                "_preflight_sidebar_parent",
                return_value={"verified_identity": True, "creation_date": "start-a"},
            ), mock.patch.object(
                helper, "_preflight_sidebar_browser", return_value={"verified_identity": True}
            ), mock.patch.object(
                helper, "_verify_shutdown_authorization", return_value={"authorized": True}
            ), mock.patch.object(helper, "_close_sidebar_windows"), mock.patch.object(
                helper,
                "_stop_weflow",
                return_value={
                    "verified_stopped": False,
                    "port_released": False,
                    "remaining_port_pids": [9001],
                    "blocked_pids": [{"pid": 9001, "state": "identity_mismatch"}],
                },
            ), mock.patch.object(
                helper,
                "_stop_sidebar_parent",
                return_value={"verified_stopped": True, "state": "stopped", "pid": 1234},
            ) as stop_parent, mock.patch.object(
                helper, "_stop_sidebar_browser_profile_processes", return_value={"verified_stopped": True}
            ), mock.patch.object(
                sidebar_api, "clear_sidebar_history_data"
            ) as clear:
                code = helper.main(
                    [
                        "--data-dir",
                        str(data_dir),
                        "--parent-pid",
                        "1234",
                        "--parent-process-start",
                        "parent-start",
                        "--shutdown-owner-token",
                        "owner-token",
                        "--weflow",
                        "on",
                        "--response-delay-seconds",
                        "0.1",
                    ]
                )

            self.assertEqual(code, 2)
            stop_parent.assert_called_once()
            clear.assert_not_called()
            status = json.loads((data_dir / "runtime" / "history_reset_shutdown.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "error")
            self.assertEqual(status["phase"], "shutdown_verification_failed")
            self.assertEqual(status["shutdown_checks"]["weflow"]["remaining_port_pids"], [9001])

    def test_helper_weflow_off_still_requires_verified_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            with mock.patch.object(
                helper,
                "_preflight_sidebar_parent",
                return_value={"verified_identity": True, "creation_date": "start-a"},
            ), mock.patch.object(
                helper, "_preflight_sidebar_browser", return_value={"verified_identity": True}
            ), mock.patch.object(
                helper, "_verify_shutdown_authorization", return_value={"authorized": True}
            ), mock.patch.object(helper, "_close_sidebar_windows"), mock.patch.object(
                helper,
                "_stop_sidebar_parent",
                return_value={"verified_stopped": True, "state": "stopped", "pid": 1234},
            ), mock.patch.object(
                helper, "_stop_sidebar_browser_profile_processes", return_value={"verified_stopped": True}
            ), mock.patch.object(
                helper,
                "_stop_weflow",
                return_value={
                    "verified_stopped": False,
                    "remaining_project_pids": [9001],
                    "terminated_pids": [],
                },
            ) as stop_weflow, mock.patch.object(sidebar_api, "clear_sidebar_history_data") as clear:
                code = helper.main(
                    [
                        "--data-dir",
                        str(data_dir),
                        "--parent-pid",
                        "1234",
                        "--parent-process-start",
                        "parent-start",
                        "--shutdown-owner-token",
                        "owner-token",
                        "--weflow",
                        "off",
                        "--response-delay-seconds",
                        "0.1",
                    ]
                )

            self.assertEqual(code, 2)
            stop_weflow.assert_called_once_with(0, 5031, "")
            clear.assert_not_called()

    def test_helper_does_not_clear_when_sidebar_parent_remains_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            with mock.patch.object(
                helper,
                "_preflight_sidebar_parent",
                return_value={"verified_identity": True, "creation_date": "start-a"},
            ), mock.patch.object(
                helper, "_preflight_sidebar_browser", return_value={"verified_identity": True}
            ), mock.patch.object(
                helper, "_verify_shutdown_authorization", return_value={"authorized": True}
            ), mock.patch.object(helper, "_close_sidebar_windows"), mock.patch.object(
                helper,
                "_stop_sidebar_parent",
                return_value={"verified_stopped": False, "state": "still_running_after_terminate", "pid": 1234},
            ), mock.patch.object(helper, "_stop_weflow") as stop_weflow, mock.patch.object(
                sidebar_api, "clear_sidebar_history_data"
            ) as clear:
                code = helper.main(
                    [
                        "--data-dir",
                        str(data_dir),
                        "--parent-pid",
                        "1234",
                        "--parent-process-start",
                        "parent-start",
                        "--shutdown-owner-token",
                        "owner-token",
                        "--weflow",
                        "off",
                        "--response-delay-seconds",
                        "0.1",
                    ]
                )

            self.assertEqual(code, 2)
            stop_weflow.assert_not_called()
            clear.assert_not_called()
            status = json.loads((data_dir / "runtime" / "history_reset_shutdown.json").read_text(encoding="utf-8"))
            self.assertEqual(status["phase"], "shutdown_verification_failed")
            self.assertFalse(status["shutdown_checks"]["sidebar_parent"]["verified_stopped"])

    def test_helper_reports_clear_blocked_without_claiming_clear_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            with mock.patch.object(
                helper,
                "_preflight_sidebar_parent",
                return_value={"verified_identity": True, "creation_date": "start-a"},
            ), mock.patch.object(
                helper, "_preflight_sidebar_browser", return_value={"verified_identity": True}
            ), mock.patch.object(
                helper, "_verify_shutdown_authorization", return_value={"authorized": True}
            ), mock.patch.object(helper, "_close_sidebar_windows"), mock.patch.object(
                helper,
                "_stop_sidebar_parent",
                return_value={"verified_stopped": True, "state": "stopped", "pid": 1234},
            ), mock.patch.object(
                helper, "_stop_sidebar_browser_profile_processes", return_value={"verified_stopped": True}
            ), mock.patch.object(
                helper,
                "_stop_weflow",
                return_value={"verified_stopped": True, "terminated_pids": []},
            ) as stop_weflow, mock.patch.object(
                helper,
                "_finalize_weflow_start_lock",
                return_value={"verified": True, "state": "absent"},
            ), mock.patch.object(
                sidebar_api,
                "clear_sidebar_history_data",
                return_value={"status": "blocked", "reason": "history_clear_runtime_active"},
            ):
                code = helper.main(
                    [
                        "--data-dir",
                        str(data_dir),
                        "--parent-pid",
                        "1234",
                        "--parent-process-start",
                        "parent-start",
                        "--shutdown-owner-token",
                        "owner-token",
                        "--weflow",
                        "off",
                        "--response-delay-seconds",
                        "0.1",
                    ]
                )

            self.assertEqual(code, 4)
            stop_weflow.assert_called_once_with(0, 5031, "")
            status = json.loads((data_dir / "runtime" / "history_reset_shutdown.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "blocked")
            self.assertEqual(status["phase"], "clear_blocked")
            self.assertNotEqual(status["phase"], "stopped_after_clear")

    def test_sidebar_parent_preflight_requires_the_helper_creator(self) -> None:
        info = {
            "status": "running",
            "pid": 1234,
            "image": r"C:\Python\python.exe",
            "command_line": r"python scripts\start_sidebar_frontend.py",
            "creation_date": "start-a",
        }
        with mock.patch.object(helper, "process_start_marker", return_value="parent-marker"), mock.patch.object(
            helper, "_process_info", return_value=info
        ), mock.patch.object(
            helper.os, "getppid", return_value=4321
        ), mock.patch.object(helper, "_terminate_pid") as terminate:
            result = helper._preflight_sidebar_parent(
                1234,
                data_dir=Path("data").resolve(),
                expected_process_start="parent-marker",
            )

        terminate.assert_not_called()
        self.assertFalse(result["verified_identity"])
        self.assertEqual(result["state"], "not_helper_parent")

    def test_sidebar_parent_preflight_rejects_scheduled_marker_before_process_query(self) -> None:
        with mock.patch.object(helper, "process_start_marker", return_value="reused-process"), mock.patch.object(
            helper, "_process_info"
        ) as process_info:
            result = helper._preflight_sidebar_parent(
                1234,
                data_dir=Path("data").resolve(),
                expected_process_start="scheduled-process",
            )

        process_info.assert_not_called()
        self.assertFalse(result["verified_identity"])
        self.assertEqual(result["state"], "parent_process_start_mismatch")

    def test_sidebar_parent_identity_and_exit_are_verified(self) -> None:
        info = {
            "status": "running",
            "pid": 1234,
            "image": r"C:\Python\python.exe",
            "command_line": r"python -m app.personal_wechat_bot.main --data-dir data send-sidebar",
            "creation_date": "start-a",
        }
        with mock.patch.object(helper, "process_start_marker", return_value="parent-marker"), mock.patch.object(
            helper,
            "_process_info",
            side_effect=[info, info, {"status": "absent", "pid": 1234}],
        ), mock.patch.object(
            helper, "_process_descendants", side_effect=[[], []]
        ), mock.patch.object(
            helper, "_terminate_pid", return_value={"attempted": True, "returncode": 0}
        ) as terminate, mock.patch.object(helper, "_wait_for_pid_exit", return_value=True) as wait:
            result = helper._stop_sidebar_parent(
                1234,
                data_dir=Path("data").resolve(),
                expected_creation_date="start-a",
                expected_process_start="parent-marker",
            )

        terminate.assert_called_once_with(1234, tree=False, expected_process_start="parent-marker")
        wait.assert_called_once_with(1234, timeout_seconds=8.0)
        self.assertTrue(result["verified_stopped"])
        self.assertEqual(result["identity"], "personal_wechat_bot_sidebar_cli")

    def test_sidebar_parent_identity_mismatch_is_not_terminated(self) -> None:
        info = {
            "status": "running",
            "pid": 1234,
            "image": r"C:\Windows\System32\notepad.exe",
            "command_line": "notepad.exe",
            "creation_date": "start-a",
        }
        with mock.patch.object(helper, "process_start_marker", return_value="parent-marker"), mock.patch.object(
            helper, "_process_info", return_value=info
        ), mock.patch.object(helper, "_terminate_pid") as terminate:
            result = helper._stop_sidebar_parent(
                1234,
                data_dir=Path("data").resolve(),
                expected_creation_date="start-a",
                expected_process_start="parent-marker",
            )

        terminate.assert_not_called()
        self.assertFalse(result["verified_stopped"])
        self.assertEqual(result["state"], "identity_mismatch")

    def test_sidebar_parent_pid_reuse_is_not_terminated(self) -> None:
        info = {
            "status": "running",
            "pid": 1234,
            "image": r"C:\Python\python.exe",
            "command_line": r"python scripts\start_sidebar_frontend.py",
            "creation_date": "start-b",
        }
        with mock.patch.object(helper, "process_start_marker", return_value="parent-marker"), mock.patch.object(
            helper, "_process_info", return_value=info
        ), mock.patch.object(helper, "_terminate_pid") as terminate:
            result = helper._stop_sidebar_parent(
                1234,
                data_dir=Path("data").resolve(),
                expected_creation_date="start-a",
                expected_process_start="parent-marker",
            )

        terminate.assert_not_called()
        self.assertFalse(result["verified_stopped"])
        self.assertEqual(result["state"], "pid_reused_or_creation_time_changed")

    def test_sidebar_parent_pid_reuse_during_child_inventory_is_not_terminated(self) -> None:
        parent = {
            "status": "running",
            "pid": 1234,
            "image": r"C:\Python\python.exe",
            "command_line": r"python scripts\start_sidebar_frontend.py",
            "creation_date": "start-a",
        }
        reused = {
            "status": "running",
            "pid": 1234,
            "image": r"C:\Windows\System32\notepad.exe",
            "command_line": "notepad.exe",
            "creation_date": "start-b",
        }
        with mock.patch.object(helper, "process_start_marker", return_value="parent-marker"), mock.patch.object(
            helper, "_process_info", side_effect=[parent, reused]
        ), mock.patch.object(
            helper, "_process_descendants", return_value=[]
        ), mock.patch.object(helper, "_terminate_pid") as terminate:
            result = helper._stop_sidebar_parent(
                1234,
                data_dir=Path("data").resolve(),
                expected_creation_date="start-a",
                expected_process_start="parent-marker",
            )

        terminate.assert_not_called()
        self.assertFalse(result["verified_stopped"])
        self.assertEqual(result["state"], "pid_reused_or_creation_time_changed_before_terminate")

    def test_sidebar_parent_exit_during_inventory_still_stops_writer_children(self) -> None:
        data_dir = Path(r"C:\chatbot\data")
        parent = {
            "status": "running",
            "pid": 1234,
            "image": r"C:\Python\python.exe",
            "command_line": r"python scripts\start_sidebar_frontend.py",
            "creation_date": "parent-start-a",
        }
        child = {"pid": 2345, "parent_pid": 1234, "creation_date": "child-start-a"}
        child_info = {
            "status": "running",
            "pid": 2345,
            "image": r"C:\Python\python.exe",
            "command_line": rf"python -m app.personal_wechat_bot.runtime.send_bridge_worker --data-dir {data_dir}",
            "creation_date": "child-start-a",
        }
        with mock.patch.object(helper, "process_start_marker", return_value="parent-marker"), mock.patch.object(
            helper,
            "_process_info",
            side_effect=[
                parent,
                {"status": "absent", "pid": 1234},
                {"status": "absent", "pid": 1234},
                child_info,
            ],
        ), mock.patch.object(helper, "_process_descendants", side_effect=[[child], [child]]), mock.patch.object(
            helper.time, "monotonic", side_effect=[0.0, 4.0]
        ), mock.patch.object(
            helper, "_terminate_pid", return_value={"attempted": True, "returncode": 0}
        ) as terminate, mock.patch.object(helper, "_wait_for_pid_exit", return_value=True):
            result = helper._stop_sidebar_parent(
                1234,
                data_dir=data_dir,
                expected_creation_date="parent-start-a",
                expected_process_start="parent-marker",
            )

        terminate.assert_called_once_with(2345, tree=False, expected_process_start="parent-marker")
        self.assertTrue(result["verified_stopped"])
        self.assertEqual(result["termination"]["reason"], "already_exited_before_terminate")

    def test_sidebar_parent_already_exited_still_stops_verified_writer_children(self) -> None:
        data_dir = Path(r"C:\chatbot\data")
        child = {
            "pid": 2345,
            "parent_pid": 1234,
            "creation_date": "child-start-a",
        }
        child_info = {
            "status": "running",
            "pid": 2345,
            "image": r"C:\Python\python.exe",
            "command_line": rf"python -m app.personal_wechat_bot.runtime.send_bridge_worker --data-dir {data_dir}",
            "creation_date": "child-start-a",
        }
        with mock.patch.object(helper, "process_start_marker", return_value="parent-marker"), mock.patch.object(
            helper,
            "_process_info",
            side_effect=[{"status": "absent", "pid": 1234}, {"status": "absent", "pid": 1234}, child_info],
        ), mock.patch.object(
            helper, "_process_descendants", return_value=[child]
        ), mock.patch.object(helper.time, "monotonic", side_effect=[0.0, 4.0]), mock.patch.object(
            helper, "_terminate_pid", return_value={"attempted": True, "returncode": 0}
        ) as terminate, mock.patch.object(helper, "_wait_for_pid_exit", return_value=True):
            result = helper._stop_sidebar_parent(
                1234,
                data_dir=data_dir,
                expected_creation_date="parent-start-a",
                expected_process_start="parent-marker",
            )

        terminate.assert_called_once_with(2345, tree=False, expected_process_start="parent-marker")
        self.assertTrue(result["verified_stopped"])
        self.assertEqual(result["state"], "already_exited")

    def test_sidebar_parent_already_exited_fails_closed_when_child_inventory_fails(self) -> None:
        with mock.patch.object(helper, "process_start_marker", return_value="parent-marker"), mock.patch.object(
            helper, "_process_info", return_value={"status": "absent", "pid": 1234}
        ), mock.patch.object(
            helper, "_process_descendants", side_effect=RuntimeError("inventory unavailable")
        ):
            result = helper._stop_sidebar_parent(
                1234,
                data_dir=Path(r"C:\chatbot\data"),
                expected_creation_date="parent-start-a",
                expected_process_start="parent-marker",
            )

        self.assertFalse(result["verified_stopped"])
        self.assertEqual(result["state"], "child_inventory_failed")

    def test_shutdown_authorization_binds_config_lock_owner_and_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            status_path = runtime_dir / "history_reset_shutdown.json"
            parent_pid = os.getppid()
            parent_process_start = helper.process_start_marker(parent_pid)
            owner_token = "shutdown-owner"
            (runtime_dir / "history_reset_shutdown.lock").write_text(
                json.dumps(
                    {
                        "helper_pid": os.getpid(),
                        "helper_process_start": helper.process_start_marker(os.getpid()),
                        "owner_pid": parent_pid,
                        "owner_process_start": parent_process_start,
                        "owner_token": owner_token,
                        "data_dir": str(data_dir.resolve()),
                        "updated_at_epoch": time.time(),
                        "status_file": str(status_path),
                    }
                ),
                encoding="utf-8",
            )

            result = helper._verify_shutdown_authorization(
                data_dir.resolve(),
                parent_pid=parent_pid,
                parent_process_start=parent_process_start,
                shutdown_owner_token=owner_token,
            )

            self.assertTrue(result["authorized"])

    def test_shutdown_authorization_rejects_reused_helper_pid_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            status_path = runtime_dir / "history_reset_shutdown.json"
            parent_pid = os.getppid()
            parent_process_start = helper.process_start_marker(parent_pid)
            owner_token = "shutdown-owner"
            (runtime_dir / "history_reset_shutdown.lock").write_text(
                json.dumps(
                    {
                        "helper_pid": os.getpid(),
                        "helper_process_start": "stale-start",
                        "owner_pid": parent_pid,
                        "owner_process_start": parent_process_start,
                        "owner_token": owner_token,
                        "data_dir": str(data_dir.resolve()),
                        "updated_at_epoch": time.time(),
                        "status_file": str(status_path),
                    }
                ),
                encoding="utf-8",
            )

            result = helper._verify_shutdown_authorization(
                data_dir.resolve(),
                parent_pid=parent_pid,
                parent_process_start=parent_process_start,
                shutdown_owner_token=owner_token,
            )

            self.assertFalse(result["authorized"])
            self.assertEqual(result["reason"], "helper_process_start_mismatch")

    def test_shutdown_authorization_binds_owner_process_start_and_owner_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            status_path = runtime_dir / "history_reset_shutdown.json"
            parent_pid = os.getppid()
            parent_process_start = helper.process_start_marker(parent_pid)
            lock_path = runtime_dir / "history_reset_shutdown.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "helper_pid": os.getpid(),
                        "helper_process_start": helper.process_start_marker(os.getpid()),
                        "owner_pid": parent_pid,
                        "owner_process_start": "reused-parent",
                        "owner_token": "owner-a",
                        "data_dir": str(data_dir),
                        "updated_at_epoch": time.time(),
                        "status_file": str(status_path),
                    }
                ),
                encoding="utf-8",
            )

            result = helper._verify_shutdown_authorization(
                data_dir,
                parent_pid=parent_pid,
                parent_process_start=parent_process_start,
                shutdown_owner_token="owner-a",
            )

            self.assertFalse(result["authorized"])
            self.assertEqual(result["reason"], "owner_process_start_mismatch")

            result = helper._verify_shutdown_authorization(
                data_dir,
                parent_pid=parent_pid,
                parent_process_start="reused-parent",
                shutdown_owner_token="owner-b",
            )
            self.assertFalse(result["authorized"])
            self.assertEqual(result["reason"], "shutdown_owner_token_mismatch")

    def test_shutdown_lock_refresh_and_remove_require_matching_owner_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True)
            lock_path = runtime_dir / "history_reset_shutdown.lock"
            payload = {
                "helper_pid": os.getpid(),
                "helper_process_start": helper.process_start_marker(os.getpid()),
                "owner_token": "owner-a",
                "updated_at_epoch": 1.0,
            }
            lock_path.write_text(json.dumps(payload), encoding="utf-8")

            helper._refresh_shutdown_lock(data_dir, shutdown_owner_token="owner-b")
            self.assertEqual(json.loads(lock_path.read_text(encoding="utf-8")), payload)
            helper._remove_shutdown_lock(data_dir, shutdown_owner_token="owner-b")
            self.assertTrue(lock_path.exists())

            helper._refresh_shutdown_lock(data_dir, shutdown_owner_token="owner-a")
            refreshed = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertGreater(refreshed["updated_at_epoch"], 1.0)
            helper._remove_shutdown_lock(data_dir, shutdown_owner_token="owner-a")
            self.assertFalse(lock_path.exists())

    def test_data_dir_validation_is_lexical_and_never_resolves_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            (data_dir / "runtime").mkdir(parents=True)
            (data_dir / "runtime_locks").mkdir()
            lexical_input = str(data_dir / "runtime" / "..")
            expected = Path(os.path.abspath(os.path.normpath(lexical_input)))

            with mock.patch.object(Path, "resolve", side_effect=AssertionError("resolve must not be called")):
                result = helper._validated_data_dir(lexical_input)

            self.assertEqual(result, expected)

    def test_data_dir_validation_rejects_root_runtime_and_lock_reparse_points(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            (data_dir / "runtime").mkdir(parents=True)
            (data_dir / "runtime_locks").mkdir()
            for unsafe_path in (data_dir, data_dir / "runtime", data_dir / "runtime_locks"):
                with self.subTest(unsafe_path=unsafe_path), mock.patch.object(
                    helper,
                    "_path_is_reparse_or_symlink",
                    side_effect=lambda path, path_stat, unsafe_path=unsafe_path: path == unsafe_path,
                ):
                    with self.assertRaises(ValueError):
                        helper._validated_data_dir(data_dir)

            fake_stat = mock.Mock(st_mode=stat.S_IFDIR, st_file_attributes=helper._FILE_ATTRIBUTE_REPARSE_POINT)
            self.assertTrue(helper._path_is_reparse_or_symlink(data_dir, fake_stat))

    def test_data_dir_validation_rejects_filesystem_root(self) -> None:
        filesystem_root = Path(Path.cwd().anchor)
        with self.assertRaises(ValueError):
            helper._validated_data_dir(filesystem_root)

    def test_status_atomic_replace_does_not_mutate_external_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True)
            external = Path(tmp) / "external.json"
            external.write_text("sentinel", encoding="utf-8")
            status_path = runtime_dir / "history_reset_shutdown.json"
            os.link(external, status_path)

            helper._write_status(
                data_dir,
                {"status": "running", "phase": "test"},
                shutdown_owner_token="",
            )

            self.assertEqual(external.read_text(encoding="utf-8"), "sentinel")
            self.assertEqual(json.loads(status_path.read_text(encoding="utf-8"))["phase"], "test")
            self.assertNotEqual(status_path.stat().st_ino, external.stat().st_ino)

    def test_write_status_always_persists_current_helper_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True)

            with mock.patch.object(helper, "process_start_marker", return_value="helper-current-start") as marker:
                helper._write_status(
                    data_dir,
                    {
                        "status": "running",
                        "phase": "clearing_history",
                        "helper_pid": 9999,
                        "helper_process_start": "untrusted",
                    },
                    shutdown_owner_token="",
                )

            payload = json.loads((runtime_dir / "history_reset_shutdown.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["helper_pid"], os.getpid())
            self.assertEqual(payload["helper_process_start"], "helper-current-start")
            marker.assert_called_once_with(os.getpid())

    def test_refresh_refuses_hardlinked_shutdown_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True)
            external = Path(tmp) / "external-lock.json"
            payload = {
                "helper_pid": os.getpid(),
                "helper_process_start": helper.process_start_marker(os.getpid()),
                "owner_token": "owner-a",
                "updated_at_epoch": 1.0,
            }
            external.write_text(json.dumps(payload), encoding="utf-8")
            os.link(external, runtime_dir / "history_reset_shutdown.lock")

            helper._refresh_shutdown_lock(data_dir, shutdown_owner_token="owner-a")

            self.assertEqual(json.loads(external.read_text(encoding="utf-8")), payload)

    def test_helper_without_authorization_does_not_write_or_stop_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            status_path = data_dir / "runtime" / "history_reset_shutdown.json"
            with mock.patch.object(
                helper,
                "_preflight_sidebar_parent",
                return_value={"verified_identity": True, "creation_date": "start-a"},
            ), mock.patch.object(helper, "_close_sidebar_windows") as close, mock.patch.object(
                helper, "_stop_weflow"
            ) as stop_weflow, mock.patch.object(sidebar_api, "clear_sidebar_history_data") as clear:
                code = helper.main(
                    [
                        "--data-dir",
                        str(data_dir),
                        "--parent-pid",
                        "1234",
                        "--parent-process-start",
                        "parent-start",
                        "--shutdown-owner-token",
                        "owner-token",
                        "--weflow",
                        "on",
                        "--response-delay-seconds",
                        "0.1",
                    ]
                )

            self.assertEqual(code, 2)
            close.assert_not_called()
            stop_weflow.assert_not_called()
            clear.assert_not_called()
            self.assertFalse(status_path.exists())

    def test_authorized_preflight_failure_writes_status_and_removes_owned_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            status_path = runtime_dir / "history_reset_shutdown.json"
            lock_path = runtime_dir / "history_reset_shutdown.lock"
            parent_pid = os.getppid()
            parent_process_start = helper.process_start_marker(parent_pid)
            owner_token = "shutdown-owner"
            lock_path.write_text(
                json.dumps(
                    {
                        "helper_pid": os.getpid(),
                        "helper_process_start": helper.process_start_marker(os.getpid()),
                        "owner_pid": parent_pid,
                        "owner_process_start": parent_process_start,
                        "owner_token": owner_token,
                        "data_dir": str(data_dir.resolve()),
                        "updated_at_epoch": time.time(),
                        "status_file": str(status_path),
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                helper,
                "_preflight_sidebar_parent",
                return_value={"verified_identity": False, "state": "identity_mismatch"},
            ), mock.patch.object(helper, "_close_sidebar_windows") as close, mock.patch.object(
                helper, "_stop_weflow"
            ) as stop_weflow, mock.patch.object(sidebar_api, "clear_sidebar_history_data") as clear:
                code = helper.main(
                    [
                        "--data-dir",
                        str(data_dir),
                        "--parent-pid",
                        str(parent_pid),
                        "--parent-process-start",
                        parent_process_start,
                        "--shutdown-owner-token",
                        owner_token,
                        "--weflow",
                        "on",
                        "--response-delay-seconds",
                        "0.1",
                    ]
                )

            self.assertEqual(code, 2)
            close.assert_not_called()
            stop_weflow.assert_not_called()
            clear.assert_not_called()
            self.assertFalse(lock_path.exists())
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["phase"], "parent_preflight_failed")

    def test_unrelated_port_listener_blocks_without_being_terminated(self) -> None:
        unrelated = {
            "status": "running",
            "pid": 9001,
            "image": r"C:\Program Files\Other\server.exe",
            "command_line": "server.exe --port 5031",
        }
        with mock.patch.object(helper, "_project_weflow_pids", return_value=[]), mock.patch.object(
            helper, "_pids_listening_on_port", return_value=[9001]
        ), mock.patch.object(
            helper, "_process_info", return_value=unrelated
        ), mock.patch.object(
            helper, "process_start_marker", return_value="listener-start"
        ), mock.patch.object(helper, "_terminate_pid") as terminate, mock.patch.object(
            helper, "_wait_for_port_release", return_value=False
        ):
            result = helper._stop_weflow(0, 5031)

        terminate.assert_not_called()
        self.assertFalse(result["verified_stopped"])
        self.assertEqual(result["remaining_port_pids"], [9001])
        self.assertEqual(result["blocked_pids"][0]["state"], "identity_mismatch")

    def test_project_weflow_listener_is_terminated_and_verified(self) -> None:
        weflow = {
            "status": "running",
            "pid": 9002,
            "image": str(helper.ROOT / "vendor" / "reference" / "WeFlow-gitcode" / "node_modules" / "electron.exe"),
            "command_line": "electron.exe .",
        }
        with mock.patch.object(helper, "_project_weflow_pids", return_value=[]), mock.patch.object(
            helper, "_pids_listening_on_port", side_effect=[[9002], []]
        ), mock.patch.object(
            helper, "_process_info", return_value=weflow
        ), mock.patch.object(
            helper, "process_start_marker", return_value="weflow-start"
        ), mock.patch.object(
            helper, "_terminate_pid", return_value={"attempted": True, "returncode": 0}
        ) as terminate, mock.patch.object(helper, "_wait_for_pid_exit", return_value=True), mock.patch.object(
            helper, "_wait_for_port_release", return_value=True
        ):
            result = helper._stop_weflow(0, 5031)

        terminate.assert_called_once_with(9002, tree=False, expected_process_start="weflow-start")
        self.assertTrue(result["verified_stopped"])
        self.assertEqual(result["remaining_port_pids"], [])

    def test_project_scan_stops_weflow_on_a_custom_unreported_port(self) -> None:
        weflow = {
            "status": "running",
            "pid": 9010,
            "parent_pid": 1,
            "image": str(helper.WEFLOW_DIR / "node_modules" / "electron.exe"),
            "command_line": str(helper.WEFLOW_DIR / "node_modules" / "electron.exe"),
            "creation_date": "weflow-created",
        }
        with mock.patch.object(helper, "_pids_listening_on_port", side_effect=[[], []]), mock.patch.object(
            helper, "_process_table", side_effect=[[weflow], [], []]
        ), mock.patch.object(helper, "_process_info", return_value=weflow), mock.patch.object(
            helper, "process_start_marker", return_value="weflow-start"
        ), mock.patch.object(
            helper, "_terminate_pid", return_value={"attempted": True, "returncode": 0}
        ) as terminate, mock.patch.object(helper, "_wait_for_pid_exit", return_value=True), mock.patch.object(
            helper, "_wait_for_port_release", return_value=True
        ):
            result = helper._stop_weflow(0, 5031)

        terminate.assert_called_once_with(9010, tree=False, expected_process_start="weflow-start")
        self.assertTrue(result["verified_stopped"])
        self.assertEqual(result["project_scan_pids"], [9010])

    def test_weflow_children_are_individually_creation_checked_and_terminated(self) -> None:
        child = {"pid": 9011, "parent_pid": 9010, "creation_date": "child-created"}
        current_child = {
            "status": "running",
            "pid": 9011,
            "parent_pid": 9010,
            "creation_date": "child-created",
            "process_start": "child-start",
        }
        with mock.patch.object(helper, "process_start_marker", return_value="root-start"), mock.patch.object(
            helper, "_process_descendants", return_value=[child]
        ), mock.patch.object(helper, "_stable_process_sample", return_value=current_child), mock.patch.object(
            helper, "_terminate_pid", return_value={"attempted": True, "returncode": 0}
        ) as terminate, mock.patch.object(helper, "_wait_for_pid_exit", return_value=True):
            result = helper._stop_verified_weflow_child_tree(
                9010,
                expected_root_process_start="root-start",
            )

        terminate.assert_called_once_with(9011, tree=False, expected_process_start="child-start")
        self.assertTrue(result["verified_stopped"])
        self.assertEqual(result["terminated_pids"], [9011])

    def test_weflow_child_pid_reuse_is_never_terminated(self) -> None:
        child = {"pid": 9011, "parent_pid": 9010, "creation_date": "child-created"}
        reused_child = {
            "status": "running",
            "pid": 9011,
            "parent_pid": 7777,
            "creation_date": "reused-created",
            "process_start": "reused-start",
        }
        with mock.patch.object(helper, "process_start_marker", return_value="root-start"), mock.patch.object(
            helper, "_process_descendants", return_value=[child]
        ), mock.patch.object(helper, "_stable_process_sample", return_value=reused_child), mock.patch.object(
            helper, "_terminate_pid"
        ) as terminate:
            result = helper._stop_verified_weflow_child_tree(
                9010,
                expected_root_process_start="root-start",
            )

        terminate.assert_not_called()
        self.assertFalse(result["verified_stopped"])
        self.assertEqual(result["blocked"][0]["state"], "pid_reused_or_creation_time_changed")

    def test_other_weflow_checkout_does_not_match_project_identity(self) -> None:
        matched, identity = helper._matches_weflow_process(
            {
                "image": r"C:\other\WeFlow-gitcode\node_modules\electron.exe",
                "command_line": r"C:\other\WeFlow-gitcode\node_modules\electron.exe .",
            }
        )

        self.assertFalse(matched)
        self.assertEqual(identity, "not_weflow_process")

    def test_reused_known_pid_with_marker_mismatch_is_not_terminated(self) -> None:
        unrelated = {
            "status": "running",
            "pid": 9003,
            "image": r"C:\Windows\System32\notepad.exe",
            "command_line": "notepad.exe",
        }
        with mock.patch.object(helper, "_project_weflow_pids", return_value=[]), mock.patch.object(
            helper, "_pids_listening_on_port", side_effect=[[], []]
        ), mock.patch.object(
            helper, "_process_info", return_value=unrelated
        ), mock.patch.object(
            helper, "process_start_marker", return_value="current-start"
        ), mock.patch.object(helper, "_terminate_pid") as terminate, mock.patch.object(
            helper, "_wait_for_port_release", return_value=True
        ):
            result = helper._stop_weflow(9003, 5031, "recorded-start")

        terminate.assert_not_called()
        self.assertTrue(result["verified_stopped"])
        self.assertEqual(result["inspected"][0]["state"], "start_marker_mismatch")

    def test_matching_known_process_marker_does_not_authorize_generic_launcher(self) -> None:
        launcher = {
            "status": "running",
            "pid": 9004,
            "image": r"C:\Program Files\nodejs\node.exe",
            "command_line": "npm-cli.js run electron:dev",
        }
        with mock.patch.object(helper, "_project_weflow_pids", return_value=[]), mock.patch.object(
            helper, "_pids_listening_on_port", side_effect=[[], []]
        ), mock.patch.object(
            helper, "_process_info", return_value=launcher
        ), mock.patch.object(
            helper, "process_start_marker", return_value="recorded-start"
        ), mock.patch.object(
            helper, "_terminate_pid", return_value={"attempted": True, "returncode": 0}
        ) as terminate, mock.patch.object(helper, "_wait_for_pid_exit", return_value=True), mock.patch.object(
            helper, "_wait_for_port_release", return_value=True
        ):
            result = helper._stop_weflow(9004, 5031, "recorded-start")

        terminate.assert_not_called()
        self.assertFalse(result["verified_stopped"])
        self.assertEqual(
            result["blocked_pids"][0]["reason"],
            "known_pid_missing_project_or_listener_identity",
        )

    def test_matching_known_process_marker_and_port_authorize_launcher(self) -> None:
        launcher = {
            "status": "running",
            "pid": 9004,
            "image": r"C:\Program Files\nodejs\node.exe",
            "command_line": "npm-cli.js run electron:dev",
        }
        with mock.patch.object(helper, "_project_weflow_pids", return_value=[]), mock.patch.object(
            helper, "_pids_listening_on_port", side_effect=[[9004], []]
        ), mock.patch.object(helper, "_process_info", return_value=launcher), mock.patch.object(
            helper, "process_start_marker", return_value="recorded-start"
        ), mock.patch.object(
            helper, "_terminate_pid", return_value={"attempted": True, "returncode": 0}
        ) as terminate, mock.patch.object(helper, "_wait_for_pid_exit", return_value=True), mock.patch.object(
            helper, "_wait_for_port_release", return_value=True
        ):
            result = helper._stop_weflow(9004, 5031, "recorded-start")

        terminate.assert_called_once_with(9004, tree=False, expected_process_start="recorded-start")
        self.assertTrue(result["verified_stopped"])
        self.assertEqual(result["inspected"][0]["identity"], "weflow_launch_marker_and_port_listener")

    def test_generic_known_launcher_without_marker_blocks_clear(self) -> None:
        launcher = {
            "status": "running",
            "pid": 9005,
            "image": r"C:\Program Files\nodejs\node.exe",
            "command_line": "npm-cli.js run electron:dev",
        }
        with mock.patch.object(helper, "_project_weflow_pids", return_value=[]), mock.patch.object(
            helper, "_pids_listening_on_port", side_effect=[[], []]
        ), mock.patch.object(
            helper, "_process_info", return_value=launcher
        ), mock.patch.object(helper, "_terminate_pid") as terminate, mock.patch.object(
            helper, "_wait_for_port_release", return_value=True
        ):
            result = helper._stop_weflow(9005, 5031)

        terminate.assert_not_called()
        self.assertFalse(result["verified_stopped"])
        self.assertEqual(result["blocked_pids"][0]["reason"], "missing_known_process_start")

    def test_known_weflow_marker_query_failure_blocks_even_when_path_matches(self) -> None:
        launcher = {
            "status": "running",
            "pid": 9006,
            "image": r"C:\chatbot-win\vendor\reference\WeFlow-gitcode\node.exe",
            "command_line": r"C:\chatbot-win\vendor\reference\WeFlow-gitcode\node.exe electron:dev",
        }
        with mock.patch.object(helper, "_project_weflow_pids", return_value=[]), mock.patch.object(
            helper, "_pids_listening_on_port", side_effect=[[], []]
        ), mock.patch.object(
            helper, "_process_info", return_value=launcher
        ), mock.patch.object(helper, "process_start_marker", return_value=""), mock.patch.object(
            helper, "_terminate_pid"
        ) as terminate, mock.patch.object(helper, "_wait_for_port_release", return_value=True):
            result = helper._stop_weflow(9006, 5031, "recorded-start")

        terminate.assert_not_called()
        self.assertFalse(result["verified_stopped"])
        self.assertEqual(result["blocked_pids"][0]["reason"], "process_start_query_failed")

    def test_listener_query_matches_the_exact_port(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout="\n".join(
                [
                    "  TCP    127.0.0.1:15031    0.0.0.0:0    LISTENING    1111",
                    "  TCP    127.0.0.1:5031     0.0.0.0:0    LISTENING    2222",
                    "  TCP    [::]:5031          [::]:0       LISTENING    3333",
                ]
            ),
        )
        with mock.patch.object(helper.os, "name", "nt"), mock.patch.object(
            helper.subprocess, "run", return_value=completed
        ):
            result = helper._pids_listening_on_port(5031)

        self.assertEqual(result, [2222, 3333])

    def test_windows_termination_dispatch_never_invokes_taskkill(self) -> None:
        expected = {"attempted": True, "returncode": 0, "exited": True}
        with mock.patch.object(helper.os, "name", "nt"), mock.patch.object(
            helper, "_terminate_windows_process_handle", return_value=expected
        ) as terminate_handle, mock.patch.object(helper.subprocess, "run") as subprocess_run:
            result = helper._terminate_pid(
                1234,
                tree=True,
                expected_process_start="win:123",
            )

        self.assertEqual(result, expected)
        terminate_handle.assert_called_once_with(
            1234,
            expected_process_start="win:123",
            tree_requested=True,
        )
        subprocess_run.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Win32 process-handle contract")
    def test_windows_termination_verifies_and_terminates_on_the_same_handle(self) -> None:
        import ctypes

        class FakeFunction:
            def __init__(self, implementation):
                self.implementation = implementation

            def __call__(self, *args):
                return self.implementation(*args)

        class FakeKernel32:
            def __init__(self, *, creation_ticks: int, terminate: bool = True):
                self.handle = 4321
                self.handles: list[tuple[str, int]] = []
                self.wait_results = iter([258, 0])
                self.terminate_enabled = terminate
                self.OpenProcess = FakeFunction(lambda access, inherit, pid: self.handle)
                self.GetProcessTimes = FakeFunction(self.get_process_times)
                self.TerminateProcess = FakeFunction(self.terminate_process)
                self.WaitForSingleObject = FakeFunction(self.wait_for_single_object)
                self.CloseHandle = FakeFunction(self.close_handle)
                self.creation_ticks = creation_ticks

            def get_process_times(self, handle, created, exited, kernel, user):
                self.handles.append(("times", int(handle)))
                created._obj.dwHighDateTime = self.creation_ticks >> 32
                created._obj.dwLowDateTime = self.creation_ticks & 0xFFFFFFFF
                return 1

            def terminate_process(self, handle, exit_code):
                self.handles.append(("terminate", int(handle)))
                return 1 if self.terminate_enabled else 0

            def wait_for_single_object(self, handle, timeout):
                self.handles.append(("wait", int(handle)))
                return next(self.wait_results)

            def close_handle(self, handle):
                self.handles.append(("close", int(handle)))
                return 1

        kernel32 = FakeKernel32(creation_ticks=123)
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel32):
            result = helper._terminate_windows_process_handle(
                1234,
                expected_process_start="win:123",
                tree_requested=True,
            )

        self.assertTrue(result["attempted"])
        self.assertTrue(result["exited"])
        self.assertEqual({handle for _operation, handle in kernel32.handles}, {kernel32.handle})
        self.assertEqual([operation for operation, _handle in kernel32.handles].count("terminate"), 1)

        reused_kernel32 = FakeKernel32(creation_ticks=456)
        with mock.patch.object(ctypes, "WinDLL", return_value=reused_kernel32):
            reused_result = helper._terminate_windows_process_handle(
                1234,
                expected_process_start="win:123",
                tree_requested=False,
            )

        self.assertFalse(reused_result["attempted"])
        self.assertEqual(reused_result["reason"], "process_start_mismatch")
        self.assertNotIn("terminate", [operation for operation, _handle in reused_kernel32.handles])

    def test_sidebar_parent_allows_ordinary_non_writer_child(self) -> None:
        parent = {
            "status": "running",
            "pid": 1234,
            "image": r"C:\Python\python.exe",
            "command_line": r"python scripts\start_sidebar_frontend.py",
            "creation_date": "start-a",
        }
        child = {"status": "running", "pid": 2345, "parent_pid": 1234, "image": r"C:\Other\tool.exe"}
        with mock.patch.object(helper, "process_start_marker", return_value="parent-marker"), mock.patch.object(
            helper,
            "_process_info",
            side_effect=[parent, parent, {"status": "absent", "pid": 1234}],
        ), mock.patch.object(
            helper, "_process_descendants", side_effect=[[child], [child]]
        ), mock.patch.object(
            helper, "_terminate_pid", return_value={"attempted": True, "returncode": 0}
        ), mock.patch.object(helper, "_wait_for_pid_exit", return_value=True), mock.patch.object(
            helper,
            "_stop_verified_sidebar_children",
            return_value={
                "verified_stopped": True,
                "state": "stopped",
                "blocked": [],
                "ignored": [{"pid": 2345, "state": "ignored_non_writer"}],
            },
        ):
            result = helper._stop_sidebar_parent(
                1234,
                data_dir=Path("data").resolve(),
                expected_creation_date="start-a",
                expected_process_start="parent-marker",
            )

        self.assertTrue(result["verified_stopped"])
        self.assertEqual(result["state"], "stopped")

    def test_data_profile_browser_child_is_verified_and_terminated(self) -> None:
        data_dir = Path(r"C:\chatbot\data")
        child = {"pid": 2345, "parent_pid": 1234, "creation_date": "child-start-a"}
        child_info = {
            "status": "running",
            "pid": 2345,
            "image": r"C:\Program Files\WebView\msedgewebview2.exe",
            "command_line": rf'msedgewebview2.exe --user-data-dir="{data_dir}\webview-profile"',
            "creation_date": "child-start-a",
        }
        with mock.patch.object(helper, "process_start_marker", return_value="child-marker"), mock.patch.object(
            helper.time, "monotonic", side_effect=[0.0, 4.0]
        ), mock.patch.object(
            helper, "_process_info", return_value=child_info
        ), mock.patch.object(
            helper, "_terminate_pid", return_value={"attempted": True, "returncode": 0}
        ) as terminate, mock.patch.object(helper, "_wait_for_pid_exit", return_value=True):
            result = helper._stop_verified_sidebar_children([child], data_dir=data_dir)

        terminate.assert_called_once_with(2345, tree=False, expected_process_start="child-marker")
        self.assertTrue(result["verified_stopped"])
        self.assertEqual(result["terminated"][0]["identity"], "data_writer_path")

    def test_reused_sidebar_writer_child_pid_is_not_terminated(self) -> None:
        data_dir = Path(r"C:\chatbot\data")
        child = {"pid": 2345, "parent_pid": 1234, "creation_date": "child-start-a"}
        child_info = {
            "status": "running",
            "pid": 2345,
            "image": r"C:\Python\python.exe",
            "command_line": rf"python -m app.personal_wechat_bot.runtime.send_bridge_worker --data-dir {data_dir}",
            "creation_date": "child-start-b",
        }
        with mock.patch.object(helper, "process_start_marker", return_value="child-marker"), mock.patch.object(
            helper.time, "monotonic", side_effect=[0.0, 4.0]
        ), mock.patch.object(
            helper, "_process_info", return_value=child_info
        ), mock.patch.object(helper, "_terminate_pid") as terminate:
            result = helper._stop_verified_sidebar_children([child], data_dir=data_dir)

        terminate.assert_not_called()
        self.assertFalse(result["verified_stopped"])
        self.assertEqual(result["blocked"][0]["state"], "pid_reused_or_creation_time_changed")

    def test_ordinary_ui_child_is_recorded_without_blocking(self) -> None:
        child = {"pid": 2345, "parent_pid": 1234}
        child_info = {
            "status": "running",
            "pid": 2345,
            "image": r"C:\Program Files\WebView\msedgewebview2.exe",
            "command_line": "msedgewebview2.exe --type=renderer",
        }
        with mock.patch.object(helper, "process_start_marker", return_value="child-marker"), mock.patch.object(
            helper.time, "monotonic", side_effect=[0.0, 4.0]
        ), mock.patch.object(
            helper, "_process_info", return_value=child_info
        ), mock.patch.object(helper, "_terminate_pid") as terminate:
            result = helper._stop_verified_sidebar_children([child], data_dir=Path(r"C:\chatbot\data"))

        terminate.assert_not_called()
        self.assertTrue(result["verified_stopped"])
        self.assertEqual(result["ignored"][0]["state"], "ignored_non_writer")

    def test_child_with_unavailable_identity_fields_blocks_clear(self) -> None:
        child = {"pid": 2345, "parent_pid": 1234, "creation_date": "child-start-a"}
        child_info = {
            "status": "running",
            "pid": 2345,
            "image": "",
            "command_line": "",
            "creation_date": "child-start-a",
        }
        with mock.patch.object(helper, "process_start_marker", return_value="child-marker"), mock.patch.object(
            helper.time, "monotonic", side_effect=[0.0, 4.0]
        ), mock.patch.object(
            helper, "_process_info", return_value=child_info
        ), mock.patch.object(helper, "_terminate_pid") as terminate:
            result = helper._stop_verified_sidebar_children([child], data_dir=Path(r"C:\chatbot\data"))

        terminate.assert_not_called()
        self.assertFalse(result["verified_stopped"])
        self.assertEqual(result["blocked"][0]["reason"], "process_identity_fields_unavailable")

    def test_sidebar_child_inventory_excludes_helper_subtree(self) -> None:
        table = [
            {"pid": os.getpid(), "parent_pid": 1234},
            {"pid": 3456, "parent_pid": os.getpid()},
            {"pid": 4567, "parent_pid": 1234},
        ]
        with mock.patch.object(helper, "_process_table", return_value=table):
            descendants = helper._process_descendants(1234)

        self.assertEqual([item["pid"] for item in descendants], [4567])

    def test_weflow_start_lock_live_unverified_owner_is_not_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            lock_path = data_dir / "runtime" / "weflow_start.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(
                json.dumps({"pid": 9876, "process_start": "start-9876", "owner_token": "new-owner"}),
                encoding="utf-8",
            )
            with mock.patch.object(helper, "_pid_state", return_value=True), mock.patch.object(
                helper, "process_start_marker", return_value="start-9876"
            ):
                result = helper._finalize_weflow_start_lock(data_dir, {"terminated_pids": [9876]})

            self.assertFalse(result["verified"])
            self.assertEqual(result["state"], "live_unverified_owner")
            self.assertTrue(lock_path.exists())

    def test_weflow_start_lock_marker_query_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            lock_path = data_dir / "runtime" / "weflow_start.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(
                json.dumps({"pid": 9876, "process_start": "start-9876", "owner_token": "new-owner"}),
                encoding="utf-8",
            )
            with mock.patch.object(helper, "_pid_state", return_value=True), mock.patch.object(
                helper, "process_start_marker", return_value=""
            ):
                result = helper._finalize_weflow_start_lock(data_dir, {"terminated_pids": []})

            self.assertFalse(result["verified"])
            self.assertEqual(result["state"], "live_unverified_owner")
            self.assertTrue(lock_path.exists())


if __name__ == "__main__":
    unittest.main()

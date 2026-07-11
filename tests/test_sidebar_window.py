from __future__ import annotations

import tempfile
import threading
import time
import unittest
import subprocess
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.control import sidebar_window
from app.personal_wechat_bot.config.loader import create_default_config
from app.personal_wechat_bot.runtime.history_fence import active_history_writer_leases
from app.personal_wechat_bot.wechat_driver.windows_readonly import WindowInfo


class SidebarWindowTest(unittest.TestCase):
    def test_sidebar_window_entrypoint_is_importable(self) -> None:
        self.assertTrue(callable(sidebar_window.run_sidebar_window))

    def test_queue_helpers_flatten_counts(self) -> None:
        state = {
            "queues": {
                "pending": {
                    "count": 1,
                    "items": [{"queue_id": "q1", "reply": {"conversation_id": "c1", "text": "hello"}}],
                },
                "approved": {"count": 0, "items": []},
                "queued_to_bridge": {"count": 1, "items": [{"queue_id": "q2"}]},
                "failed": {"count": 0, "items": []},
            },
        }

        self.assertEqual(sidebar_window.queue_counts(state)["pending"], 1)
        self.assertEqual(sidebar_window.queue_counts(state)["queued_to_bridge"], 1)
        self.assertEqual(sidebar_window.flatten_queue_items(state)[0]["queue_id"], "q1")
        self.assertEqual(sidebar_window.flatten_queue_items(state)[1]["status"], "queued_to_bridge")

    def test_sidebar_geometry_uses_default_when_wechat_missing(self) -> None:
        original = sidebar_window._wechat_anchor
        sidebar_window._wechat_anchor = lambda data_dir=None: None
        try:
            geometry = sidebar_window._sidebar_geometry(width=420, height=700)
        finally:
            sidebar_window._wechat_anchor = original

        self.assertEqual(geometry, {"x": 80, "y": 80, "width": 420, "height": 700})

    def test_sidebar_geometry_places_inside_wechat_when_right_side_has_no_room(self) -> None:
        original_anchor = sidebar_window._wechat_anchor
        original_work_area = sidebar_window._work_area
        sidebar_window._wechat_anchor = lambda data_dir=None: {"left": 100, "top": 80, "right": 1000, "bottom": 780}
        sidebar_window._work_area = lambda: {"left": 0, "top": 0, "right": 1024, "bottom": 768}
        try:
            geometry = sidebar_window._sidebar_geometry(width=420, height=700)
        finally:
            sidebar_window._wechat_anchor = original_anchor
            sidebar_window._work_area = original_work_area

        self.assertEqual(geometry["x"], 572)
        self.assertEqual(geometry["y"], 68)
        self.assertEqual(geometry["height"], 700)

    def test_wechat_anchor_filters_offscreen_tray_windows(self) -> None:
        class _Probe:
            def find_wechat_windows(self):
                return [
                    WindowInfo(hwnd=1, title="微信", width=157, height=25, left=-16000, top=-16000, right=-15843, bottom=-15975, process_name="Weixin.exe"),
                    WindowInfo(hwnd=2, title="微信", width=1000, height=700, left=100, top=100, right=1100, bottom=800, process_name="Weixin.exe"),
                ]

        original_probe = sidebar_window.Win32WindowProbe
        sidebar_window.Win32WindowProbe = lambda include_invisible=False: _Probe()
        try:
            anchor = sidebar_window._wechat_anchor()
        finally:
            sidebar_window.Win32WindowProbe = original_probe

        self.assertEqual(anchor, {"left": 100, "top": 100, "right": 1100, "bottom": 800})

    def test_launch_result_json_is_stable(self) -> None:
        payload = sidebar_window.result_as_json(
            sidebar_window.SidebarLaunchResult(
                status="ok",
                url="http://127.0.0.1:1/",
                host="127.0.0.1",
                port=1,
                browser="chrome",
                pid=123,
                geometry={"x": 1, "y": 2, "width": 3, "height": 4},
            )
        )

        self.assertIn('"status": "ok"', payload)

    def test_external_browser_fallback_keeps_server_alive_until_controlled_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            stop_event = threading.Event()
            server = mock.Mock()
            server_thread = mock.Mock()
            server_thread.is_alive.return_value = False
            result = sidebar_window.SidebarLaunchResult(
                status="opened_external_browser",
                url="http://127.0.0.1:1/",
                host="127.0.0.1",
                port=1,
                browser="default",
                pid=None,
                geometry={"x": 1, "y": 2, "width": 3, "height": 4},
                browser_profile=str(data_dir / "runtime" / "sidebar_browser_profile"),
                _server=server,
                _server_thread=server_thread,
            )
            with mock.patch.object(sidebar_window, "launch_sidebar_window", return_value=result):
                runner = threading.Thread(
                    target=sidebar_window.run_sidebar_window,
                    kwargs={"data_dir": data_dir, "stop_event": stop_event},
                )
                runner.start()
                time.sleep(0.1)
                self.assertTrue(runner.is_alive())
                stop_event.set()
                runner.join(timeout=2.0)

            self.assertFalse(runner.is_alive())
            server.shutdown.assert_called_once_with()
            server.server_close.assert_called_once_with()

    def test_sidebar_window_holds_history_lease_for_full_server_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            stop_event = threading.Event()
            server = mock.Mock()
            server_thread = mock.Mock()
            server_thread.is_alive.return_value = False
            result = sidebar_window.SidebarLaunchResult(
                status="opened_external_browser",
                url="http://127.0.0.1:1/",
                host="127.0.0.1",
                port=1,
                browser="default",
                pid=None,
                geometry={"x": 1, "y": 2, "width": 3, "height": 4},
                browser_profile=str(data_dir / "runtime" / "sidebar_browser_profile"),
                _server=server,
                _server_thread=server_thread,
            )
            observed: list[bool] = []

            def launch(*args, **kwargs):
                observed.append(bool(active_history_writer_leases(data_dir)))
                stop_event.set()
                return result

            with mock.patch.object(sidebar_window, "launch_sidebar_window", side_effect=launch):
                sidebar_window.run_sidebar_window(data_dir, stop_event=stop_event)

            self.assertEqual(observed, [True])
            self.assertEqual(active_history_writer_leases(data_dir), [])

    def test_window_tracker_stop_shuts_server_and_owned_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            process = mock.Mock(pid=7001)
            process.poll.return_value = None
            process.wait.return_value = 0
            server = mock.Mock()
            server_thread = mock.Mock()
            server_thread.is_alive.return_value = False
            result = sidebar_window.SidebarLaunchResult(
                status="ok",
                url="http://127.0.0.1:1/",
                host="127.0.0.1",
                port=1,
                browser=r"C:\Chrome\chrome.exe",
                pid=7001,
                geometry={"x": 1, "y": 2, "width": 3, "height": 4},
                browser_process_start="browser:start",
                browser_executable=r"C:\Chrome\chrome.exe",
                browser_profile=str(data_dir / "runtime" / "sidebar_browser_profile"),
                owned_browser=True,
                _server=server,
                _server_thread=server_thread,
                _browser_process=process,
            )

            def close_tracker(*args):
                args[-1].set()

            with mock.patch.object(sidebar_window, "launch_sidebar_window", return_value=result), mock.patch.object(
                sidebar_window, "_track_sidebar_window", side_effect=close_tracker
            ), mock.patch.object(sidebar_window, "_find_sidebar_window", return_value=None), mock.patch.object(
                sidebar_window,
                "inspect_sidebar_browser_runtime",
                return_value={"inventory_verified": True, "profile_processes": []},
            ):
                sidebar_window.run_sidebar_window(data_dir, poll_interval_ms=250)

            server.shutdown.assert_called_once_with()
            process.wait.assert_called()

    def test_missing_window_sets_tracker_stop_event(self) -> None:
        stopped = threading.Event()
        with mock.patch.object(sidebar_window.sys, "platform", "win32"), mock.patch.object(
            sidebar_window, "WINDOW_MISSING_GRACE_SECONDS", 0.5
        ), mock.patch.object(sidebar_window, "_find_sidebar_window", return_value=None):
            sidebar_window._track_sidebar_window(
                7001,
                250,
                430,
                760,
                Path("data"),
                stopped,
            )

        self.assertTrue(stopped.is_set())

    def test_launch_records_owned_browser_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            process = mock.Mock(pid=7001)
            process.poll.return_value = None
            process.wait.return_value = 0
            server = mock.Mock(server_address=("127.0.0.1", 8123))
            server_thread = mock.Mock()
            server_thread.is_alive.return_value = False
            states: list[dict[str, object]] = []
            with mock.patch.object(sidebar_window, "ThreadingHTTPServer", return_value=server), mock.patch.object(
                sidebar_window.threading, "Thread", return_value=server_thread
            ), mock.patch.object(
                sidebar_window,
                "inspect_sidebar_browser_runtime",
                side_effect=[
                    {"verified": True, "inventory_verified": True, "profile_processes": []},
                    {
                        "inventory_verified": True,
                        "browser_process_tree": [
                            {
                                "pid": 7001,
                                "parent_pid": 0,
                                "process_start": "browser:start",
                                "image": r"C:\Chrome\chrome.exe",
                                "root_pid": 7001,
                                "identity_verified": True,
                            }
                        ],
                    },
                ],
            ), mock.patch.object(sidebar_window, "_sidebar_geometry", return_value={"x": 1, "y": 2, "width": 3, "height": 4}), mock.patch.object(
                sidebar_window, "_close_existing_sidebar_windows"
            ), mock.patch.object(
                sidebar_window,
                "_open_app_window",
                return_value=(r"C:\Chrome\chrome.exe", process),
            ), mock.patch.object(sidebar_window, "process_start_marker", return_value="browser:start"):
                result = sidebar_window.launch_sidebar_window(
                    data_dir,
                    browser_state_callback=states.append,
                )

            self.assertTrue(result.owned_browser)
            self.assertEqual(result.browser_process_start, "browser:start")
            self.assertEqual(states[0]["browser_pid"], 7001)
            self.assertTrue(states[0]["browser_job_owned"])
            self.assertEqual(states[0]["browser_descendants"][0]["pid"], 7001)
            self.assertEqual(states[0]["browser_profile"], str((data_dir / "runtime" / "sidebar_browser_profile").resolve()))

    def test_launch_rejects_unsafe_or_reparse_profile_before_browser_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            server = mock.Mock(server_address=("127.0.0.1", 8123))
            server_thread = mock.Mock()
            with mock.patch.object(sidebar_window, "ThreadingHTTPServer", return_value=server), mock.patch.object(
                sidebar_window.threading, "Thread", return_value=server_thread
            ), mock.patch.object(
                sidebar_window,
                "inspect_sidebar_browser_runtime",
                return_value={
                    "verified": False,
                    "inventory_verified": True,
                    "profile_processes": [],
                    "blockers": [{"reason": "unsafe_sidebar_browser_profile"}],
                },
            ), mock.patch.object(
                sidebar_window,
                "_sidebar_geometry",
                return_value={"x": 1, "y": 2, "width": 3, "height": 4},
            ), mock.patch.object(sidebar_window, "_open_app_window") as spawn:
                with self.assertRaisesRegex(RuntimeError, "profile preflight"):
                    sidebar_window.launch_sidebar_window(data_dir)

            spawn.assert_not_called()
            server.shutdown.assert_called_once_with()
            server.server_close.assert_called_once_with()

    def test_launch_rejects_recorded_descendant_after_profile_root_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            server = mock.Mock(server_address=("127.0.0.1", 8123))
            server_thread = mock.Mock()
            with mock.patch.object(sidebar_window, "ThreadingHTTPServer", return_value=server), mock.patch.object(
                sidebar_window.threading, "Thread", return_value=server_thread
            ), mock.patch.object(
                sidebar_window,
                "inspect_sidebar_browser_runtime",
                return_value={
                    "verified": True,
                    "inventory_verified": True,
                    "profile_processes": [],
                    "browser_process_tree": [
                        {
                            "pid": 7002,
                            "parent_pid": 7001,
                            "process_start": "renderer:start",
                            "image": r"C:\Chrome\chrome.exe",
                            "identity_verified": True,
                        }
                    ],
                    "blockers": [{"reason": "active_sidebar_browser_descendant", "pid": 7002}],
                },
            ), mock.patch.object(
                sidebar_window,
                "_sidebar_geometry",
                return_value={"x": 1, "y": 2, "width": 3, "height": 4},
            ), mock.patch.object(sidebar_window, "_open_app_window") as spawn:
                with self.assertRaisesRegex(RuntimeError, "profile is already active"):
                    sidebar_window.launch_sidebar_window(data_dir)

            spawn.assert_not_called()
            server.shutdown.assert_called_once_with()
            server.server_close.assert_called_once_with()
            server_thread.join.assert_called_once_with(timeout=3.0)

    def test_profile_reparse_is_rejected_by_final_pre_spawn_check(self) -> None:
        reparse_dir = mock.Mock(
            st_mode=sidebar_window.stat.S_IFDIR,
            st_file_attributes=0x400,
        )
        with mock.patch.object(sidebar_window, "_safe_lstat", return_value=reparse_dir):
            with self.assertRaisesRegex(RuntimeError, "unsafe sidebar browser runtime"):
                sidebar_window._prepare_private_browser_profile(Path("C:/data/runtime/sidebar_browser_profile"))

    def test_geometry_failure_after_server_start_still_closes_listener(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = mock.Mock(server_address=("127.0.0.1", 8123))
            server_thread = mock.Mock()
            with mock.patch.object(sidebar_window, "ThreadingHTTPServer", return_value=server), mock.patch.object(
                sidebar_window.threading, "Thread", return_value=server_thread
            ), mock.patch.object(
                sidebar_window,
                "_sidebar_geometry",
                side_effect=PermissionError("window inventory denied"),
            ):
                with self.assertRaises(PermissionError):
                    sidebar_window.launch_sidebar_window(Path(tmp) / "data")

            server.shutdown.assert_called_once_with()
            server.server_close.assert_called_once_with()
            server_thread.join.assert_called_once_with(timeout=3.0)

    def test_app_window_disables_browser_background_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            browser = Path(tmp) / "chrome.exe"
            process = mock.Mock(pid=7001)
            with mock.patch.object(sidebar_window.sys, "platform", "win32"), mock.patch.object(
                sidebar_window, "_browser_candidates", return_value=[browser]
            ), mock.patch.object(sidebar_window, "start_windows_job_process", return_value=process) as spawn:
                selected, returned_process = sidebar_window._open_app_window(
                    "http://127.0.0.1:8123/",
                    geometry={"x": 1, "y": 2, "width": 3, "height": 4},
                    profile_dir=profile,
                )

            command = spawn.call_args.args[0]
            self.assertIn("--disable-background-mode", command)
            self.assertIn(f"--user-data-dir={profile}", command)
            self.assertIs(returned_process, process)
            self.assertEqual(Path(selected).name.lower(), "chrome.exe")

    def test_browser_shutdown_uses_graceful_handle_termination_before_force(self) -> None:
        process = mock.Mock(pid=7001)
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="browser", timeout=3.0),
            0,
        ]

        sidebar_window._wait_or_terminate_browser_process(process)

        process.terminate.assert_called_once_with()
        process.kill.assert_not_called()
        self.assertEqual(process.wait.call_count, 2)

    def test_browser_shutdown_force_kills_and_reaps_unresponsive_process(self) -> None:
        process = mock.Mock(pid=7001)
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="browser", timeout=3.0),
            subprocess.TimeoutExpired(cmd="browser", timeout=3.0),
            0,
        ]

        sidebar_window._wait_or_terminate_browser_process(process)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 3)

    def test_title_match_from_unowned_browser_pid_is_never_used(self) -> None:
        with mock.patch.object(sidebar_window, "_window_for_pid", return_value=None) as by_pid:
            self.assertIsNone(sidebar_window._find_sidebar_window(7001))

        by_pid.assert_called_once_with(7001)

    def test_close_targets_only_verified_browser_pids(self) -> None:
        with mock.patch.object(sidebar_window.sys, "platform", "win32"), mock.patch.object(
            sidebar_window,
            "_window_for_pid",
            side_effect=lambda pid: 101 if pid == 7001 else None,
        ) as by_pid, mock.patch.object(sidebar_window, "_post_close_window") as close, mock.patch.object(
            sidebar_window.time, "sleep"
        ):
            sidebar_window._close_existing_sidebar_windows((7001,))

        by_pid.assert_called_once_with(7001)
        close.assert_called_once_with(101)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.config.loader import create_default_config
from app.personal_wechat_bot.control import sidebar_browser_runtime
from app.personal_wechat_bot.control.audit import build_artifact_cleanup_report
from app.personal_wechat_bot.control.sidebar_api import clear_sidebar_history_data
from app.personal_wechat_bot.runtime.history_fence import history_writer_lease


class SidebarBrowserRuntimeTest(unittest.TestCase):
    def _fixture(self, root: Path, *, executable: str = r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        data_dir = root / "data"
        create_default_config(data_dir)
        profile = data_dir / "runtime" / "sidebar_browser_profile"
        profile.mkdir(parents=True)
        state = {
            "pid": 9001,
            "process_start": "parent:start",
            "data_dir": str(data_dir.resolve()),
            "browser_owned": True,
            "browser_pid": 7001,
            "browser_process_start": "browser:start",
            "browser_executable": executable,
            "browser_profile": str(profile.resolve()),
            "browser_job_owned": True,
            "browser_descendants": [
                {
                    "pid": 7001,
                    "parent_pid": 9001,
                    "process_start": "browser:start",
                    "executable": executable,
                    "root_pid": 7001,
                }
            ],
        }
        (data_dir / "runtime" / "sidebar_launch.json").write_text(
            json.dumps(state),
            encoding="utf-8",
        )
        record = {
            "pid": 7001,
            "parent_pid": 9001,
            "image": executable,
            "command_line": f'"{executable}" --user-data-dir="{profile.resolve()}"',
            "argv": [executable, f"--user-data-dir={profile.resolve()}"],
            "creation_date": "created:1",
        }
        return data_dir, profile, record

    def test_direct_clear_blocks_live_project_browser_after_parent_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, profile, record = self._fixture(Path(tmp))
            with mock.patch.object(
                sidebar_browser_runtime,
                "sidebar_browser_process_inventory",
                return_value=[record],
            ), mock.patch.object(
                sidebar_browser_runtime,
                "process_start_marker",
                return_value="browser:start",
            ):
                result = clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "blocked")
            blocker = next(item for item in result["active_workers"] if item["worker"] == "sidebar_browser_profile")
            self.assertEqual(blocker["pid"], 7001)
            self.assertEqual(blocker["reason"], "active_sidebar_browser_profile")
            self.assertTrue(profile.exists())

    def test_reused_recorded_pid_without_profile_argument_is_not_terminated_or_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, profile, record = self._fixture(Path(tmp))
            record["argv"] = [record["image"], "--user-data-dir=C:\\unrelated"]
            record["command_line"] = " ".join(record["argv"])
            with mock.patch.object(
                sidebar_browser_runtime,
                "sidebar_browser_process_inventory",
                return_value=[record],
            ), mock.patch.object(
                sidebar_browser_runtime,
                "process_start_marker",
                return_value="browser:reused",
            ):
                inspection = sidebar_browser_runtime.inspect_sidebar_browser_runtime(data_dir)
                result = clear_sidebar_history_data(data_dir)

            self.assertEqual(inspection["recorded_process"]["state"], "pid_reused")
            self.assertEqual(inspection["blockers"], [])
            self.assertEqual(result["status"], "ok")
            self.assertFalse(profile.exists())

    def test_inventory_failure_retains_profile_and_blocks_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, profile, _record = self._fixture(Path(tmp))
            with mock.patch.object(
                sidebar_browser_runtime,
                "sidebar_browser_process_inventory",
                side_effect=RuntimeError("CIM unavailable"),
            ):
                clear_result = clear_sidebar_history_data(data_dir)
                cleanup = build_artifact_cleanup_report(data_dir, apply=True)

            self.assertEqual(clear_result["status"], "blocked")
            self.assertTrue(profile.exists())
            retained = next(item for item in cleanup["retained"] if item["relative_path"] == "runtime/sidebar_browser_profile")
            self.assertEqual(retained["action"], "retain_active")

    def test_artifact_cleanup_retains_profile_while_sidebar_lifecycle_lease_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            profile = data_dir / "runtime" / "sidebar_browser_profile"
            profile.mkdir(parents=True)
            with mock.patch.object(
                sidebar_browser_runtime,
                "sidebar_browser_process_inventory",
                return_value=[],
            ):
                with history_writer_lease(data_dir, label="sidebar_browser_lifecycle"):
                    cleanup = build_artifact_cleanup_report(data_dir, apply=True)

            self.assertTrue(profile.exists())
            retained = next(item for item in cleanup["retained"] if item["relative_path"] == "runtime/sidebar_browser_profile")
            self.assertEqual(retained["action"], "retain_active")

    def test_wrong_executable_with_exact_profile_is_a_fail_closed_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, profile, record = self._fixture(Path(tmp))
            record["image"] = r"C:\Windows\System32\notepad.exe"
            with mock.patch.object(
                sidebar_browser_runtime,
                "sidebar_browser_process_inventory",
                return_value=[record],
            ), mock.patch.object(
                sidebar_browser_runtime,
                "process_start_marker",
                return_value="browser:start",
            ):
                inspection = sidebar_browser_runtime.inspect_sidebar_browser_runtime(data_dir)

            self.assertTrue(profile.exists())
            self.assertEqual(inspection["profile_processes"][0]["identity_reason"], "profile_process_executable_mismatch")
            self.assertEqual(inspection["blockers"][0]["reason"], "profile_process_executable_mismatch")

    def test_missing_process_marker_with_exact_profile_is_a_fail_closed_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, _profile, record = self._fixture(Path(tmp))
            with mock.patch.object(
                sidebar_browser_runtime,
                "sidebar_browser_process_inventory",
                return_value=[record],
            ), mock.patch.object(
                sidebar_browser_runtime,
                "process_start_marker",
                return_value="",
            ), mock.patch.object(
                sidebar_browser_runtime,
                "process_pid_alive",
                return_value=True,
            ):
                inspection = sidebar_browser_runtime.inspect_sidebar_browser_runtime(data_dir)

            reasons = {item["reason"] for item in inspection["blockers"]}
            self.assertIn("browser_process_start_unavailable", reasons)

    def test_recorded_live_browser_missing_from_inventory_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, _profile, _record = self._fixture(Path(tmp))
            with mock.patch.object(
                sidebar_browser_runtime,
                "sidebar_browser_process_inventory",
                return_value=[],
            ), mock.patch.object(
                sidebar_browser_runtime,
                "process_start_marker",
                return_value="browser:start",
            ), mock.patch.object(
                sidebar_browser_runtime,
                "process_pid_alive",
                return_value=True,
            ):
                inspection = sidebar_browser_runtime.inspect_sidebar_browser_runtime(data_dir)

            reasons = {item["reason"] for item in inspection["blockers"]}
            self.assertIn("recorded_browser_missing_from_process_inventory", reasons)

    def test_recorded_descendant_blocks_clear_after_profile_root_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, profile, record = self._fixture(Path(tmp))
            state_path = data_dir / "runtime" / "sidebar_launch.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["browser_descendants"].append(
                {
                    "pid": 7002,
                    "parent_pid": 7001,
                    "process_start": "renderer:start",
                    "executable": record["image"],
                    "root_pid": 7001,
                }
            )
            state_path.write_text(json.dumps(state), encoding="utf-8")
            renderer = {
                "pid": 7002,
                "parent_pid": 4,
                "image": record["image"],
                "command_line": f'"{record["image"]}" --type=renderer',
                "argv": [record["image"], "--type=renderer"],
                "creation_date": "created:2",
            }

            def marker(pid: int) -> str:
                return "renderer:start" if pid == 7002 else ""

            with mock.patch.object(
                sidebar_browser_runtime,
                "sidebar_browser_process_inventory",
                return_value=[renderer],
            ), mock.patch.object(
                sidebar_browser_runtime,
                "process_start_marker",
                side_effect=marker,
            ), mock.patch.object(
                sidebar_browser_runtime,
                "process_pid_alive",
                side_effect=lambda pid: pid == 7002,
            ):
                inspection = sidebar_browser_runtime.inspect_sidebar_browser_runtime(data_dir)
                result = clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "blocked")
            self.assertTrue(profile.exists())
            self.assertIn(7002, {int(item["pid"]) for item in inspection["browser_process_tree"]})
            self.assertIn("active_sidebar_browser_descendant", {item["reason"] for item in inspection["blockers"]})

    def test_unrelated_browser_without_profile_or_recorded_identity_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, _profile, record = self._fixture(Path(tmp))
            unrelated = {
                "pid": 8001,
                "parent_pid": 4,
                "image": record["image"],
                "command_line": f'"{record["image"]}" --user-data-dir=C:\\Users\\user\\Chrome',
                "argv": [record["image"], r"--user-data-dir=C:\Users\user\Chrome"],
                "creation_date": "created:other",
            }

            def marker(pid: int) -> str:
                return "unrelated:start" if pid == 8001 else ""

            with mock.patch.object(
                sidebar_browser_runtime,
                "sidebar_browser_process_inventory",
                return_value=[unrelated],
            ), mock.patch.object(
                sidebar_browser_runtime,
                "process_start_marker",
                side_effect=marker,
            ), mock.patch.object(
                sidebar_browser_runtime,
                "process_pid_alive",
                side_effect=lambda pid: pid == 8001,
            ):
                inspection = sidebar_browser_runtime.inspect_sidebar_browser_runtime(data_dir)

            self.assertEqual(inspection["profile_processes"], [])
            self.assertEqual(inspection["browser_process_tree"], [])
            self.assertEqual(inspection["blockers"], [])

    def test_profile_argument_requires_exact_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, profile, record = self._fixture(Path(tmp))
            record["argv"] = [record["image"], f"--user-data-dir={profile}2"]

            self.assertFalse(sidebar_browser_runtime.command_uses_sidebar_profile(record, profile))

    def test_stale_profile_without_launch_state_clears_when_inventory_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            profile = data_dir / "runtime" / "sidebar_browser_profile"
            profile.mkdir(parents=True)
            (profile / "stale-cache").write_text("stale", encoding="utf-8")
            with mock.patch.object(
                sidebar_browser_runtime,
                "sidebar_browser_process_inventory",
                return_value=[],
            ):
                result = clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertFalse(profile.exists())


if __name__ == "__main__":
    unittest.main()

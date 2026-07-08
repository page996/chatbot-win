from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.sidebar_history_reset_shutdown as helper
from app.personal_wechat_bot.config.loader import create_default_config
from app.personal_wechat_bot.control import sidebar_api


class SidebarHistoryResetShutdownHelperTest(unittest.TestCase):
    def test_helper_stops_processes_clears_history_and_does_not_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            stale_weflow_lock = data_dir / "runtime" / "weflow_start.lock"
            stale_weflow_lock.parent.mkdir(parents=True, exist_ok=True)
            stale_weflow_lock.write_text(json.dumps({"pid": 5678}), encoding="utf-8")
            calls: list[str] = []

            def fake_clear(path: Path, payload: dict[str, object]) -> dict[str, object]:
                calls.append("clear")
                self.assertEqual(Path(path), data_dir.resolve())
                self.assertFalse(payload["shutdown_processes"])
                return {"status": "ok", "removed_count": 3}

            with mock.patch.object(helper, "_close_sidebar_windows", side_effect=lambda: calls.append("close_sidebar")), mock.patch.object(
                helper, "_stop_weflow", side_effect=lambda pid, port: calls.append(f"stop_weflow:{pid}:{port}")
            ), mock.patch.object(
                helper, "_terminate_pid", side_effect=lambda pid, tree: calls.append(f"terminate_parent:{pid}:{tree}")
            ), mock.patch.object(
                helper, "_wait_for_pid_exit", side_effect=lambda pid, timeout_seconds: calls.append(f"wait_parent:{pid}")
            ), mock.patch.object(
                sidebar_api, "clear_sidebar_history_data", side_effect=fake_clear
            ):
                code = helper.main(
                    [
                        "--data-dir",
                        str(data_dir),
                        "--parent-pid",
                        "1234",
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
                    "stop_weflow:5678:5031",
                    "terminate_parent:1234:False",
                    "wait_parent:1234",
                    "clear",
                ],
            )
            status = json.loads((data_dir / "runtime" / "history_reset_shutdown.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "ok")
            self.assertEqual(status["phase"], "stopped_after_clear")
            self.assertTrue(status["manual_reopen_required"])
            self.assertNotIn("restart_result", status)
            self.assertFalse(stale_weflow_lock.exists())


if __name__ == "__main__":
    unittest.main()

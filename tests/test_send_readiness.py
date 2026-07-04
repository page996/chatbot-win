from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.config.loader import create_default_config
from app.personal_wechat_bot.control.send_commands import set_send_controls
from app.personal_wechat_bot.control.send_readiness import build_send_readiness_report


ROOT = Path(__file__).resolve().parent


class SendReadinessTest(unittest.TestCase):
    def test_current_project_is_blocked_until_real_send_driver_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            report = build_send_readiness_report(data_dir)
            blockers = {item["id"] for item in report["checks"] if item["status"] == "blocker"}

            self.assertEqual(report["status"], "blocked")
            self.assertIn("real_send_driver", blockers)
            self.assertIn("send_enabled", blockers)
            self.assertIn("wechat_write_access", blockers)
            self.assertIn("send_driver_name", blockers)
            self.assertFalse(report["send_policy"]["send_enabled"])

    def test_send_readiness_cli_returns_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            completed = subprocess.run(
                [sys.executable, "-m", "app.personal_wechat_bot.main", "--data-dir", str(data_dir), "init"],
                cwd=ROOT.parent,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
            )
            self.assertIn("initialized", completed.stdout)

            output = subprocess.run(
                [sys.executable, "-m", "app.personal_wechat_bot.main", "--data-dir", str(data_dir), "send-readiness"],
                cwd=ROOT.parent,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
            )
            payload = json.loads(output.stdout)

            self.assertEqual(payload["status"], "blocked")
            self.assertFalse(payload["send_policy"]["send_enabled"])

    def test_bridge_outbox_driver_removes_send_driver_blockers_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox")

            report = build_send_readiness_report(data_dir)
            blockers = {item["id"] for item in report["checks"] if item["status"] == "blocker"}

            self.assertNotIn("real_send_driver", blockers)
            self.assertNotIn("send_enabled", blockers)
            self.assertNotIn("wechat_write_access", blockers)
            self.assertNotIn("send_driver_name", blockers)
            # bridge_outbox no longer requires a manual foreground binding (wcf delivers by wxid).
            self.assertNotIn("manual_bridge_channels", blockers)
            self.assertIn("keep confirm mode active", " ".join(report["recommended_rollout"]))
            rollout = next(item for item in report["checks"] if item["id"] == "rollout_mode")
            self.assertEqual(rollout["status"], "warn")
            self.assertIn("confirm mode is active", rollout["detail"])

    def test_unregistered_driver_name_blocks_consistently_with_real_send_check(self) -> None:
        # Regression: send_driver_name must key off the driver registry, not a
        # weak "!= not_implemented" test, so an unregistered driver name blocks
        # consistently with the real_send_driver check (no contradiction).
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="some_unregistered_driver")

            report = build_send_readiness_report(data_dir)
            by_id = {item["id"]: item for item in report["checks"]}

            self.assertEqual(by_id["send_driver_name"]["status"], "blocker")
            self.assertEqual(by_id["real_send_driver"]["status"], "blocker")


if __name__ == "__main__":
    unittest.main()

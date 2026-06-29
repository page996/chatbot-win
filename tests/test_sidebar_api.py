from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.config.loader import create_default_config, load_config
from app.personal_wechat_bot.control.sidebar_api import (
    build_sidebar_state,
    sidebar_queue_action,
    update_sidebar_controls,
)
from app.personal_wechat_bot.domain.models import ReplyCandidate
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue


class SidebarApiTest(unittest.TestCase):
    def test_sidebar_state_contains_controls_queues_readiness_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            ConfirmQueue(data_dir / "confirm_queue.jsonl").enqueue(_reply())

            state = build_sidebar_state(data_dir)

            self.assertEqual(state["status"], "ok")
            self.assertIn("config", state)
            self.assertEqual(state["queues"]["pending"]["count"], 1)
            self.assertIn("readiness", state)
            self.assertIn("driver_probe", state)
            self.assertIn("audit", state)

    def test_sidebar_controls_update_mode_and_send_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            result = update_sidebar_controls(
                data_dir,
                {"mode": "confirm", "send_enabled": True, "send_driver": "windows_guarded"},
            )
            config = load_config(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(config.mode, "confirm")
            self.assertTrue(config.send_enabled)
            self.assertEqual(config.send_driver, "windows_guarded")

    def test_sidebar_queue_action_approves_and_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply())

            approved = sidebar_queue_action(data_dir, "approve", queue_id, {"reviewer": "test"})

            self.assertEqual(approved["item"]["status"], "approved")


def _reply() -> ReplyCandidate:
    return ReplyCandidate(
        message_id="message-1",
        conversation_id="private-1",
        text="hello",
        send_mode="confirm",
        model="fake",
    )


if __name__ == "__main__":
    unittest.main()

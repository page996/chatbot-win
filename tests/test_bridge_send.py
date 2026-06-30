from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.wechat_driver.bridge_send import (
    BridgeOutboxSendDriver,
    bridge_ack,
    bridge_state,
)
from app.personal_wechat_bot.wechat_driver.window_binding import WeChatWindowBindingStore


class BridgeSendTest(unittest.TestCase):
    def test_disabled_bridge_send_fails_without_writing_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            driver = BridgeOutboxSendDriver(send_enabled=False, data_dir=data_dir)

            result = driver.send_message("private-1", "hello")
            state = bridge_state(data_dir)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "send_enabled_false")
            self.assertEqual(state["count"], 0)

    def test_enabled_bridge_send_writes_outbox_and_ack_updates_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _write_manual_binding(data_dir, "private-1")
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)

            result = driver.send_message("private-1", "hello bridge")
            queued = bridge_state(data_dir, limit=10)
            ack = bridge_ack(data_dir, result.message_id, status="sent", reason="external_bridge_sent")
            confirmed = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "queued_to_bridge")
            self.assertIn("queued_to_non_foreground_bridge", result.reason)
            self.assertEqual(queued["pending_count"], 1)
            self.assertEqual(queued["manual_bound_count"], 1)
            self.assertEqual(queued["items"][0]["text"], "hello bridge")
            self.assertEqual(queued["items"][0]["manual_binding"]["conversation_id"], "private-1")
            self.assertEqual(ack["status"], "ok")
            self.assertEqual(confirmed["pending_count"], 0)
            self.assertEqual(confirmed["items"][0]["status"], "sent")

    def test_bridge_send_requires_manual_captured_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)

            result = driver.send_message("private-1", "hello bridge")
            state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "bridge_requires_manual_captured_channel")
            self.assertEqual(state["count"], 0)
            self.assertEqual(state["manual_bound_count"], 0)

    def test_bridge_probe_reports_paths_and_send_enabled_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            driver = BridgeOutboxSendDriver(send_enabled=False, data_dir=data_dir)

            probe = driver.probe()

            self.assertEqual(probe.driver, "bridge_outbox")
            self.assertTrue(probe.implemented)
            self.assertEqual(probe.health, "blocked")
            self.assertIn("send_enabled_false", probe.blockers)
            self.assertIn("no_manual_captured_channels", probe.blockers)
            self.assertTrue(probe.outbox_path.endswith("outbox.jsonl"))

    def test_bridge_probe_ready_when_enabled_and_manual_channel_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            _write_manual_binding(data_dir, "private-1")
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)

            probe = driver.probe()

            self.assertEqual(probe.health, "ready")
            self.assertEqual(probe.manual_bound_count, 1)
            self.assertEqual(probe.blockers, [])


def _write_manual_binding(data_dir: Path, conversation_id: str) -> None:
    store = WeChatWindowBindingStore(data_dir)
    now = "2026-06-30T00:00:00+00:00"
    store._write(
        {
            "bindings": [
                {
                    "conversation_id": conversation_id,
                    "conversation_type": "private",
                    "chat_title": "PAGE",
                    "hwnd": 100,
                    "title": "微信",
                    "process_id": 200,
                    "process_name": "Weixin.exe",
                    "class_name": "WeChatMainWndForPC",
                    "width": 1000,
                    "height": 700,
                    "left": 100,
                    "top": 100,
                    "right": 1100,
                    "bottom": 800,
                    "bound_at": now,
                    "last_seen_at": now,
                    "status": "active",
                }
            ],
            "updated_at": now,
        }
    )


if __name__ == "__main__":
    unittest.main()

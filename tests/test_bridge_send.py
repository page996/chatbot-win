from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.wechat_driver.bridge_send import (
    BridgeOutboxSendDriver,
    bridge_ack,
    bridge_state,
)


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
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)

            result = driver.send_message("private-1", "hello bridge")
            queued = bridge_state(data_dir, limit=10)
            ack = bridge_ack(data_dir, result.message_id, status="sent", reason="wcf_sent")
            confirmed = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "queued_to_bridge")
            self.assertIn("queued_to_non_foreground_bridge", result.reason)
            self.assertEqual(queued["pending_count"], 1)
            self.assertEqual(queued["items"][0]["text"], "hello bridge")
            self.assertEqual(ack["status"], "ok")
            self.assertEqual(confirmed["pending_count"], 0)
            self.assertEqual(confirmed["items"][0]["status"], "sent")

    def test_bridge_send_queues_without_manual_binding(self) -> None:
        # wcf sends by wxid/roomid, so no manual foreground binding is required.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)

            result = driver.send_message("private-1", "hello bridge")
            state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "queued_to_bridge")
            self.assertIn("queued_to_non_foreground_bridge", result.reason)
            self.assertEqual(state["pending_count"], 1)
            self.assertEqual(state["items"][0]["text"], "hello bridge")
            self.assertEqual(state["items"][0]["manual_binding"], {})

    def test_bridge_send_file_queues_file_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)
            target = data_dir / "report.pdf"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"%PDF-1.4 test")

            result = driver.send_file("private-1", str(target), caption="see attached")
            state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "queued_to_bridge")
            item = state["items"][0]
            self.assertEqual(item["kind"], "file")
            self.assertEqual(item["path"], str(target))
            self.assertEqual(item["name"], "report.pdf")
            self.assertEqual(item["caption"], "see attached")

    def test_bridge_probe_reports_paths_and_send_enabled_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            driver = BridgeOutboxSendDriver(send_enabled=False, data_dir=data_dir)

            probe = driver.probe()

            self.assertEqual(probe.driver, "bridge_outbox")
            self.assertTrue(probe.implemented)
            self.assertEqual(probe.health, "blocked")
            self.assertIn("send_enabled_false", probe.blockers)
            self.assertEqual(probe.authorization, "conversation_whitelist")
            self.assertTrue(probe.outbox_path.endswith("outbox.jsonl"))

    def test_bridge_probe_ready_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)

            probe = driver.probe()

            self.assertEqual(probe.health, "ready")
            self.assertEqual(probe.blockers, [])


if __name__ == "__main__":
    unittest.main()

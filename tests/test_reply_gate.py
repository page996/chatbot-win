from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.domain.models import ReplyCandidate
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue
from app.personal_wechat_bot.reply_gate.gate import ReplyGate
from app.personal_wechat_bot.reply_gate.send_executor import GuardedSendExecutor
from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.domain.models import SendResult


class ReplyGateTest(unittest.TestCase):
    def test_confirm_mode_writes_pending_reply_to_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = ConfirmQueue(Path(tmp) / "confirm_queue.jsonl")
            gate = ReplyGate(mode="confirm", confirm_queue=queue)
            reply = ReplyCandidate(
                message_id="message-1",
                conversation_id="private-1",
                text="hello",
                send_mode="confirm",
                model="fake",
            )

            result = gate.handle(reply)
            pending = queue.list_pending()

            self.assertEqual(result.status, "queued_for_confirm")
            self.assertIn("confirm_required:", result.reason)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["reply"]["text"], "hello")

    def test_confirm_queue_can_approve_and_reject_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = ConfirmQueue(Path(tmp) / "confirm_queue.jsonl")
            first = _reply("message-1", "hello")
            second = _reply("message-2", "bye")
            first_id = queue.enqueue(first)
            second_id = queue.enqueue(second)

            approved = queue.approve(first_id, reviewer="tester", note="ok")
            rejected = queue.reject(second_id, reviewer="tester", note="no")

            self.assertEqual(approved["status"], "approved")
            self.assertEqual(rejected["status"], "rejected")
            self.assertEqual(queue.list_pending(), [])
            self.assertEqual(len(queue.list_by_status("approved")), 1)
            self.assertEqual(len(queue.list_by_status("rejected")), 1)

    def test_confirm_queue_rejects_invalid_terminal_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = ConfirmQueue(Path(tmp) / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-1", "hello"))
            queue.approve(queue_id, reviewer="tester")
            queue.mark_send_result(queue_id, "sent", "fake_sent")

            with self.assertRaises(ValueError):
                queue.approve(queue_id, reviewer="tester")

    def test_guarded_send_executor_blocks_when_send_disabled(self) -> None:
        config = BotConfig(send_enabled=False, send_driver="fake")
        executor = GuardedSendExecutor(config, _SendingDriver())

        result = executor.execute_confirmed(_reply("message-1", "hello"))

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "send_enabled_false")

    def test_guarded_send_executor_calls_driver_when_enabled_and_configured(self) -> None:
        driver = _SendingDriver()
        config = BotConfig(send_enabled=True, send_driver="fake")
        executor = GuardedSendExecutor(config, driver)

        result = executor.execute_confirmed(_reply("message-1", "hello"))

        self.assertEqual(result.status, "sent")
        self.assertEqual(driver.sent_texts, ["hello"])

    def test_auto_mode_uses_guarded_send_executor(self) -> None:
        driver = _SendingDriver()
        config = BotConfig(send_enabled=True, send_driver="fake", send_confirm_required=False)
        executor = GuardedSendExecutor(config, driver)
        gate = ReplyGate(mode="auto", auto_executor=executor)

        result = gate.handle(_reply("message-1", "hello"))

        self.assertEqual(result.status, "sent")
        self.assertEqual(driver.sent_texts, ["hello"])

    def test_auto_mode_still_honors_confirm_required_guard(self) -> None:
        driver = _SendingDriver()
        config = BotConfig(send_enabled=True, send_driver="fake", send_confirm_required=True)
        executor = GuardedSendExecutor(config, driver)
        gate = ReplyGate(mode="auto", auto_executor=executor)

        result = gate.handle(_reply("message-1", "hello"))

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "confirm_required")
        self.assertEqual(driver.sent_texts, [])


def _reply(message_id: str, text: str) -> ReplyCandidate:
    return ReplyCandidate(
        message_id=message_id,
        conversation_id="private-1",
        text=text,
        send_mode="confirm",
        model="fake",
    )


class _SendingDriver:
    def __init__(self) -> None:
        self.sent_texts: list[str] = []

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        self.sent_texts.append(text)
        return SendResult(message_id="sent-id", conversation_id=conversation_id, status="sent", reason="fake_sent")


if __name__ == "__main__":
    unittest.main()

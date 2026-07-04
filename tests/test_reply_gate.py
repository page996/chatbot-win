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

    def test_executor_sends_reply_files_after_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.pdf"
            target.write_bytes(b"%PDF-1.4")
            driver = _FileSendingDriver()
            config = BotConfig(send_enabled=True, send_driver="fake")
            executor = GuardedSendExecutor(config, driver)
            reply = ReplyCandidate(
                message_id="m-1",
                conversation_id="private-1",
                text="see attached",
                send_mode="confirm",
                model="fake",
                attachments=[{"path": str(target), "name": "report.pdf", "status": "indexed"}],
            )

            result = executor.execute_confirmed(reply)

            self.assertEqual(result.status, "sent")
            self.assertEqual(driver.sent_texts, ["see attached"])
            self.assertEqual(driver.sent_files, [(str(target))])

    def test_executor_can_send_file_only_reply_with_empty_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "photo.png"
            target.write_bytes(b"\x89PNG")
            driver = _FileSendingDriver()
            config = BotConfig(send_enabled=True, send_driver="fake")
            executor = GuardedSendExecutor(config, driver)
            reply = ReplyCandidate(
                message_id="m-2",
                conversation_id="private-1",
                text="",
                send_mode="confirm",
                model="fake",
                attachments=[{"path": str(target), "name": "photo.png"}],
            )

            result = executor.execute_confirmed(reply)

            self.assertNotEqual(result.reason, "empty_reply")
            self.assertEqual(driver.sent_files, [str(target)])

    def test_executor_skips_files_for_driver_without_send_file(self) -> None:
        driver = _SendingDriver()  # no send_file method
        config = BotConfig(send_enabled=True, send_driver="fake")
        executor = GuardedSendExecutor(config, driver)
        reply = ReplyCandidate(
            message_id="m-3",
            conversation_id="private-1",
            text="hi",
            send_mode="confirm",
            model="fake",
            attachments=[{"path": "/nonexistent/x.pdf", "name": "x.pdf"}],
        )

        result = executor.execute_confirmed(reply)

        self.assertEqual(result.status, "sent")
        self.assertEqual(driver.sent_texts, ["hi"])


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


class _FileSendingDriver(_SendingDriver):
    def __init__(self) -> None:
        super().__init__()
        self.sent_files: list[str] = []

    def send_file(self, conversation_id: str, path: str, caption: str = "") -> SendResult:
        self.sent_files.append(path)
        return SendResult(message_id="file-id", conversation_id=conversation_id, status="sent", reason="fake_file_sent")


if __name__ == "__main__":
    unittest.main()

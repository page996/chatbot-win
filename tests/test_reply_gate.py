from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
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

    def test_confirm_queue_serializes_concurrent_enqueues_and_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = ConfirmQueue(Path(tmp) / "confirm_queue.jsonl")

            def enqueue(index: int) -> str:
                return queue.enqueue(_reply(f"message-{index}", f"hello {index}"))

            with ThreadPoolExecutor(max_workers=8) as pool:
                queue_ids = list(pool.map(enqueue, range(20)))

            self.assertEqual(len(queue_ids), 20)
            self.assertEqual(len(queue._read_all()), 20)

            def approve_and_mark(queue_id: str) -> str:
                queue.approve(queue_id, reviewer="concurrent-test")
                queue.mark_send_result(
                    queue_id,
                    "queued_to_bridge",
                    f"queued_to_non_foreground_bridge:bridge:{queue_id}",
                    reviewer="concurrent-test",
                )
                return queue_id

            with ThreadPoolExecutor(max_workers=8) as pool:
                completed_ids = list(pool.map(approve_and_mark, queue_ids))

            records = queue._read_all()
            self.assertEqual(set(completed_ids), set(queue_ids))
            self.assertEqual(len(records), 20)
            self.assertEqual({item["queue_id"] for item in records}, set(queue_ids))
            self.assertEqual({item["status"] for item in records}, {"queued_to_bridge"})
            self.assertTrue(all("queued_to_non_foreground_bridge:" in item.get("note", "") for item in records))

    def test_confirm_queue_imports_legacy_jsonl_into_sqlite_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "confirm_queue.jsonl"
            legacy = {
                "queue_id": "legacy-message:2026-07-10T00:00:00Z",
                "status": "pending",
                "created_at": "2026-07-10T00:00:00Z",
                "reply": {
                    "message_id": "legacy-message",
                    "conversation_id": "private-1",
                    "text": "legacy hello",
                    "send_mode": "confirm",
                    "model": "fake",
                },
            }
            queue_path.write_text(json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8")

            queue = ConfirmQueue(queue_path)
            pending = queue.list_pending()
            approved = queue.approve(legacy["queue_id"], reviewer="migration-test")
            projection = [json.loads(line) for line in queue_path.read_text(encoding="utf-8").splitlines() if line]

            self.assertTrue((Path(tmp) / "confirm_queue.sqlite").exists())
            self.assertEqual(pending[0]["queue_id"], legacy["queue_id"])
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(projection[0]["status"], "approved")

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

    def test_auto_mode_bypasses_human_confirm_required_guard(self) -> None:
        driver = _SendingDriver()
        config = BotConfig(send_enabled=True, send_driver="fake", send_confirm_required=True)
        executor = GuardedSendExecutor(config, driver)
        gate = ReplyGate(mode="auto", auto_executor=executor)

        result = gate.handle(_reply("message-1", "hello"))

        self.assertEqual(result.status, "sent")
        self.assertEqual(driver.sent_texts, ["hello"])

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
            self.assertIn("text:sent:fake_sent", result.reason)
            self.assertIn("report.pdf:sent:fake_file_sent", result.reason)
            self.assertEqual(result.details["text"]["message_id"], "sent-id")
            self.assertEqual(result.details["files"][0]["message_id"], "file-id")
            self.assertEqual(result.details["bridge_ids"], ["sent-id", "file-id"])

    def test_executor_does_not_hide_file_failure_after_text_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.pdf"
            target.write_bytes(b"%PDF-1.4")
            driver = _FileSendingDriver(file_status="failed", file_reason="file_backend_missing")
            config = BotConfig(send_enabled=True, send_driver="fake")
            executor = GuardedSendExecutor(config, driver)
            reply = ReplyCandidate(
                message_id="m-file-fail",
                conversation_id="private-1",
                text="see attached",
                send_mode="confirm",
                model="fake",
                attachments=[{"path": str(target), "name": "report.pdf"}],
            )

            result = executor.execute_confirmed(reply)

            self.assertEqual(result.status, "failed")
            self.assertEqual(driver.sent_texts, ["see attached"])
            self.assertEqual(driver.sent_files, [str(target)])
            self.assertIn("text:sent:fake_sent", result.reason)
            self.assertIn("report.pdf:failed:file_backend_missing", result.reason)

    def test_executor_reports_accepted_when_text_or_file_is_unverified_accept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.pdf"
            target.write_bytes(b"%PDF-1.4")
            driver = _FileSendingDriver(text_status="accepted", text_reason="native_accepted_unverified")
            config = BotConfig(send_enabled=True, send_driver="fake")
            executor = GuardedSendExecutor(config, driver)
            reply = ReplyCandidate(
                message_id="m-accepted",
                conversation_id="private-1",
                text="see attached",
                send_mode="confirm",
                model="fake",
                attachments=[{"path": str(target), "name": "report.pdf"}],
            )

            result = executor.execute_confirmed(reply)

            self.assertEqual(result.status, "accepted")
            self.assertIn("text:accepted:native_accepted_unverified", result.reason)
            self.assertIn("report.pdf:sent:fake_file_sent", result.reason)

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
            self.assertEqual(driver.sent_texts, [])
            self.assertEqual(driver.sent_files, [str(target)])
            self.assertEqual(result.details["kind"], "file_send")
            self.assertEqual(result.details["files"][0]["name"], "photo.png")

    def test_executor_file_only_requires_file_capable_driver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "photo.png"
            target.write_bytes(b"\x89PNG")
            driver = _SendingDriver()
            config = BotConfig(send_enabled=True, send_driver="fake")
            executor = GuardedSendExecutor(config, driver)
            reply = ReplyCandidate(
                message_id="m-4",
                conversation_id="private-1",
                text="",
                send_mode="confirm",
                model="fake",
                attachments=[{"path": str(target), "name": "photo.png"}],
            )

            result = executor.execute_confirmed(reply)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "file_send_driver_missing")
            self.assertEqual(driver.sent_texts, [])

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
    def __init__(self, *, text_status: str = "sent", text_reason: str = "fake_sent") -> None:
        self.sent_texts: list[str] = []
        self.text_status = text_status
        self.text_reason = text_reason

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        self.sent_texts.append(text)
        return SendResult(
            message_id="sent-id",
            conversation_id=conversation_id,
            status=self.text_status,
            reason=self.text_reason,
        )


class _FileSendingDriver(_SendingDriver):
    def __init__(
        self,
        *,
        text_status: str = "sent",
        text_reason: str = "fake_sent",
        file_status: str = "sent",
        file_reason: str = "fake_file_sent",
    ) -> None:
        super().__init__(text_status=text_status, text_reason=text_reason)
        self.sent_files: list[str] = []
        self.file_status = file_status
        self.file_reason = file_reason

    def send_file(self, conversation_id: str, path: str, caption: str = "") -> SendResult:
        self.sent_files.append(path)
        return SendResult(
            message_id="file-id",
            conversation_id=conversation_id,
            status=self.file_status,
            reason=self.file_reason,
        )


if __name__ == "__main__":
    unittest.main()

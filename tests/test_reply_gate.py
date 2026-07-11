from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.domain.models import ReplyCandidate
from app.personal_wechat_bot.reply_gate.confirm_queue import (
    ConfirmQueue,
    ConfirmQueueClaimConflict,
    SEND_CLAIM_CONFLICT,
    SEND_CLAIM_OWNER_EXITED,
)
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

    def test_confirm_queue_atomically_claims_one_sender_and_fences_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "confirm_queue.jsonl"
            queue = ConfirmQueue(queue_path)
            queue_id = queue.enqueue(_reply("message-claim", "hello"))
            queue.approve(queue_id, reviewer="tester")
            start = threading.Barrier(2)

            def claim() -> dict:
                start.wait(timeout=10.0)
                return ConfirmQueue(queue_path).claim_approved_for_send(queue_id, owner="test")

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _: claim(), range(2)))

            winner = next(item for item in outcomes if item["claimed"])
            loser = next(item for item in outcomes if not item["claimed"])
            authoritative = queue.get(queue_id)
            projected = json.loads(queue_path.read_text(encoding="utf-8").strip())

            self.assertEqual(loser["reason"], SEND_CLAIM_CONFLICT)
            self.assertEqual(authoritative["send_claim"]["token"], winner["token"])
            self.assertEqual(projected, authoritative)
            with self.assertRaises(ConfirmQueueClaimConflict):
                queue.mark_send_result(queue_id, "sent", "wrong owner", claim_token="wrong-token")
            with self.assertRaises(ConfirmQueueClaimConflict):
                queue.reject(queue_id, reviewer="racing-reviewer")
            with self.assertRaises(ConfirmQueueClaimConflict):
                queue.remove(queue_id)

            sent = queue.mark_send_result(
                queue_id,
                "sent",
                "claimed send complete",
                claim_token=winner["token"],
            )

            self.assertEqual(sent["status"], "sent")
            self.assertNotIn("send_claim", sent)
            self.assertEqual(json.loads(queue_path.read_text(encoding="utf-8").strip()), sent)

    def test_confirm_queue_releases_safe_claim_and_retires_dead_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = ConfirmQueue(Path(tmp) / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-release", "hello"))
            queue.approve(queue_id, reviewer="tester")
            first = queue.claim_approved_for_send(queue_id, owner="test")

            released = queue.release_send_claim(
                queue_id,
                first["token"],
                reason="send_enabled_false",
            )
            second = queue.claim_approved_for_send(queue_id, owner="test-retry")

            self.assertEqual(released["status"], "approved")
            self.assertNotIn("send_claim", released)
            self.assertTrue(second["claimed"])
            with mock.patch(
                "app.personal_wechat_bot.reply_gate.confirm_queue._send_claim_owner_is_alive",
                return_value=False,
            ):
                recovered = queue.claim_approved_for_send(queue_id, owner="after-crash")

            self.assertFalse(recovered["claimed"])
            self.assertEqual(recovered["reason"], SEND_CLAIM_OWNER_EXITED)
            self.assertEqual(recovered["item"]["status"], "failed")
            self.assertNotIn("send_claim", recovered["item"])

    def test_confirm_queue_projection_does_not_repopulate_sqlite_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "confirm_queue.jsonl"
            projection_only = {
                "queue_id": "projection-message:2026-07-10T00:00:00Z",
                "status": "pending",
                "created_at": "2026-07-10T00:00:00Z",
                "reply": {
                    "message_id": "projection-message",
                    "conversation_id": "private-1",
                    "text": "projection only",
                    "send_mode": "confirm",
                    "model": "fake",
                },
            }
            queue_path.write_text(json.dumps(projection_only, ensure_ascii=False) + "\n", encoding="utf-8")

            queue = ConfirmQueue(queue_path)
            pending = queue.list_pending()
            current_id = queue.enqueue(_reply("current-message", "current"))
            projection = [json.loads(line) for line in queue_path.read_text(encoding="utf-8").splitlines() if line]

            self.assertTrue((Path(tmp) / "confirm_queue.sqlite").exists())
            self.assertEqual(pending, [])
            self.assertEqual([item["queue_id"] for item in projection], [current_id])

    def test_confirm_queue_projection_failure_keeps_enqueue_idempotent_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "confirm_queue.jsonl"
            queue = ConfirmQueue(queue_path)
            reply = _reply("message-1", "hello")
            write_projection = queue._write_projection_unlocked
            attempts = 0

            def flaky_projection(records: list[dict]) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("projection temporarily unavailable")
                write_projection(records)

            with mock.patch.object(queue, "_write_projection_unlocked", side_effect=flaky_projection):
                first_id = queue.enqueue(reply)
                second_id = queue.enqueue(reply)

            pending = queue.list_pending()
            projection = [json.loads(line) for line in queue_path.read_text(encoding="utf-8").splitlines() if line]

            self.assertEqual(first_id, second_id)
            self.assertEqual([item["queue_id"] for item in pending], [first_id])
            self.assertEqual([item["queue_id"] for item in projection], [first_id])
            self.assertEqual(attempts, 2)

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
            self.assertEqual(result.details["bridge_ids"], [])

    def test_executor_never_sends_attachment_blocked_outside_allowed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "secret.txt"
            target.write_text("secret", encoding="utf-8")
            driver = _FileSendingDriver()
            executor = GuardedSendExecutor(BotConfig(send_enabled=True, send_driver="fake"), driver)
            reply = ReplyCandidate(
                message_id="m-path-blocked",
                conversation_id="private-1",
                text="",
                send_mode="confirm",
                model="fake",
                attachments=[
                    {
                        "path": str(target),
                        "name": target.name,
                        "status": "blocked",
                        "reason": f"PermissionError: path outside allowed roots: {target}",
                    }
                ],
            )

            result = executor.execute_confirmed(reply)

            self.assertEqual(result.status, "failed")
            self.assertEqual(driver.sent_files, [])
            self.assertIn("outgoing_attachment_path_not_allowed", result.reason)

    def test_executor_still_sends_blocked_parse_or_extension_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "artifact.bin"
            target.write_bytes(b"artifact")
            driver = _FileSendingDriver()
            executor = GuardedSendExecutor(BotConfig(send_enabled=True, send_driver="fake"), driver)
            reply = ReplyCandidate(
                message_id="m-parse-blocked",
                conversation_id="private-1",
                text="",
                send_mode="confirm",
                model="fake",
                attachments=[
                    {
                        "path": str(target),
                        "name": target.name,
                        "status": "blocked",
                        "reason": "PermissionError: file extension not allowed: .bin",
                    }
                ],
            )

            result = executor.execute_confirmed(reply)

            self.assertEqual(result.status, "sent")
            self.assertEqual(driver.sent_files, [str(target)])

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
            self.assertEqual(result.details["bridge_ids"], [])

    def test_executor_keeps_staged_file_result_when_later_file_driver_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.txt"
            second = Path(tmp) / "second.txt"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")

            class _StageThenRaiseDriver:
                def __init__(self) -> None:
                    self.calls = 0

                def send_message(self, conversation_id: str, text: str) -> SendResult:
                    raise AssertionError("unexpected text send")

                def send_file(self, conversation_id: str, path: str, caption: str = "") -> SendResult:
                    self.calls += 1
                    if self.calls == 1:
                        return SendResult(
                            "bridge:private-1:first",
                            conversation_id,
                            "queued_to_bridge",
                            "queued_file_to_non_foreground_bridge:bridge:private-1:first",
                        )
                    raise OSError("second file unavailable")

            driver = _StageThenRaiseDriver()
            executor = GuardedSendExecutor(BotConfig(send_enabled=True, send_driver="fake"), driver)
            reply = ReplyCandidate(
                message_id="m-staged-file-exception",
                conversation_id="private-1",
                text="",
                send_mode="confirm",
                model="fake",
                attachments=[{"path": str(first)}, {"path": str(second)}],
            )

            result = executor.execute_confirmed(reply)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.details["bridge_ids"], ["bridge:private-1:first"])
            self.assertEqual(result.details["files"][1]["status"], "failed")
            self.assertIn("send_file_exception:OSError", result.details["files"][1]["reason"])

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

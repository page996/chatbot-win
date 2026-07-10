from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.config.loader import create_default_config, load_config
from app.personal_wechat_bot.conversation.channel_registry_store import ChannelRegistryStore
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.conversation.segment import conversation_segment
from app.personal_wechat_bot.control.send_commands import (
    approve_confirm_item,
    clear_send_audit,
    list_confirm_queue,
    list_send_audit,
    probe_send_controls,
    reject_confirm_item,
    remove_confirm_item,
    retry_bridge_item,
    send_approved_confirm_item,
    set_send_controls,
    sync_bridge_ack_to_send_state,
)
from app.personal_wechat_bot.domain.models import ReplyCandidate, SendResult
from app.personal_wechat_bot.logging.jsonl_rotation import jsonl_operation_lock
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue
from app.personal_wechat_bot.reply_gate.send_audit import SendAuditLog
from app.personal_wechat_bot.tasks.manager import TaskStatusStore
from app.personal_wechat_bot.wechat_driver.bridge_send import BridgeAckStatus, BridgeOutboxStore, bridge_state


ROOT = Path(__file__).resolve().parent


class SendCommandsTest(unittest.TestCase):
    def test_set_send_controls_persists_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            payload = set_send_controls(
                data_dir,
                enabled=True,
                driver="fake",
                confirm_required=False,
                max_chars=64,
                min_interval_seconds=2,
            )
            config = load_config(data_dir)

            self.assertTrue(payload["send_enabled"])
            self.assertEqual(payload["send_driver"], "fake")
            self.assertFalse(config.send_confirm_required)
            self.assertEqual(config.send_max_chars, 64)
            self.assertEqual(config.send_min_interval_seconds, 2)

    def test_set_send_controls_persists_weflow_http_backend_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            payload = set_send_controls(
                data_dir,
                enabled=True,
                driver="bridge_outbox",
                backend="weflow_http",
                weflow_base_url="http://127.0.0.1:5031",
                weflow_token_env="WEFLOW_TEST_TOKEN",
                weflow_send_text_path="/send/custom-text",
                weflow_send_file_path="/send/custom-file",
                weflow_send_timeout_seconds=4.0,
            )
            config = load_config(data_dir)

            self.assertEqual(payload["send_backend"], "weflow_http")
            self.assertEqual(config.send_backend, "weflow_http")
            self.assertEqual(config.weflow_token_env, "WEFLOW_TEST_TOKEN")
            self.assertEqual(config.weflow_send_text_path, "/send/custom-text")
            self.assertEqual(config.weflow_send_file_path, "/send/custom-file")
            self.assertEqual(config.weflow_send_timeout_seconds, 4.0)

    def test_set_send_controls_persists_wechat_native_http_backend_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            payload = set_send_controls(
                data_dir,
                enabled=True,
                driver="bridge_outbox",
                backend="wechat_native_http",
                wechat_native_base_url="http://127.0.0.1:30001",
                wechat_native_send_text_path="/custom-text",
                wechat_native_send_image_path="/custom-image",
                wechat_native_send_file_path="/custom-file",
                wechat_native_status_path="/custom-status",
                wechat_native_timeout_seconds=4.0,
            )
            config = load_config(data_dir)

            self.assertEqual(payload["send_backend"], "wechat_native_http")
            self.assertEqual(config.send_backend, "wechat_native_http")
            self.assertEqual(config.wechat_native_base_url, "http://127.0.0.1:30001")
            self.assertEqual(config.wechat_native_send_text_path, "/custom-text")
            self.assertEqual(config.wechat_native_send_image_path, "/custom-image")
            self.assertEqual(config.wechat_native_send_file_path, "/custom-file")
            self.assertEqual(config.wechat_native_status_path, "/custom-status")
            self.assertEqual(config.wechat_native_timeout_seconds, 4.0)

    def test_set_send_controls_auto_mode_disables_confirm_gate_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            set_send_controls(data_dir, mode="auto", enabled=True, driver="fake")
            config = load_config(data_dir)

            self.assertEqual(config.mode, "auto")
            self.assertFalse(config.send_confirm_required)

    def test_list_confirm_queue_retires_obsolete_sidebar_test_approved_in_auto_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="auto", enabled=True, driver="bridge_outbox", backend="wechat_native_http")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            stale_id = queue.enqueue(_reply("sidebar_channel_test_reply:old", "old probe"))
            normal_id = queue.enqueue(_reply("message-normal", "normal reply"))
            queue.approve(stale_id, reviewer="tester")
            queue.approve(normal_id, reviewer="tester")
            TaskStatusStore(data_dir).create(
                {
                    "task_id": "send-sidebar_channel_test_replyold",
                    "title": "Send reply",
                    "kind": "send",
                    "status": "queued",
                    "conversation_id": "private-1",
                    "scope": "conversation:private-1",
                    "resource_class": "send_bridge",
                    "detail": "confirm_approved",
                    "metadata": {"message_id": "sidebar_channel_test_reply:old"},
                }
            )

            approved = list_confirm_queue(data_dir, status="approved")
            failed = list_confirm_queue(data_dir, status="failed")
            task = next(
                item
                for item in TaskStatusStore(data_dir).state()["tasks"]
                if item["metadata"].get("message_id") == "sidebar_channel_test_reply:old"
            )
            audit = list_send_audit(data_dir, limit=10, include_resolved=True)

            self.assertEqual(approved["count"], 1)
            self.assertEqual(approved["items"][0]["queue_id"], normal_id)
            self.assertEqual(failed["count"], 1)
            self.assertEqual(failed["items"][0]["queue_id"], stale_id)
            self.assertIn("obsolete_sidebar_confirm_test_task", failed["items"][0]["note"])
            self.assertEqual(task["status"], "cancelled")
            self.assertTrue(any(item.get("action") == "confirm_queue_repaired" for item in audit["items"]))

    def test_list_confirm_queue_retires_reopened_sidebar_test_after_stale_worker_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="auto", enabled=True, driver="bridge_outbox", backend="wechat_native_http")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            stale_id = queue.enqueue(_reply("sidebar_channel_test_reply:old", "old probe"))
            normal_id = queue.enqueue(_reply("message-normal", "normal reply"))
            queue.approve(stale_id, reviewer="tester")
            queue.approve(normal_id, reviewer="tester")
            TaskStatusStore(data_dir).create(
                {
                    "task_id": "send-sidebar_channel_test_replyold",
                    "title": "Send reply",
                    "kind": "send",
                    "status": "queued",
                    "conversation_id": "private-1",
                    "scope": "conversation:private-1",
                    "resource_class": "send_bridge",
                    "detail": "obsolete_bridge_worker_stale_config:current_backend=wechat_native_http",
                    "phase": "发送阻断已解除，等待重新投递",
                    "metadata": {"message_id": "sidebar_channel_test_reply:old"},
                }
            )

            approved = list_confirm_queue(data_dir, status="approved")
            failed = list_confirm_queue(data_dir, status="failed")
            task = next(
                item
                for item in TaskStatusStore(data_dir).state()["tasks"]
                if item["metadata"].get("message_id") == "sidebar_channel_test_reply:old"
            )

            self.assertEqual(approved["count"], 1)
            self.assertEqual(approved["items"][0]["queue_id"], normal_id)
            self.assertEqual(failed["count"], 1)
            self.assertEqual(failed["items"][0]["queue_id"], stale_id)
            self.assertIn("obsolete_sidebar_confirm_test_task", failed["items"][0]["note"])
            self.assertEqual(task["status"], "cancelled")

    def test_confirm_helpers_approve_reject_and_send_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            first_id = queue.enqueue(_reply("message-1", "hello"))
            second_id = queue.enqueue(_reply("message-2", "bye"))

            pending = list_confirm_queue(data_dir)
            approved = approve_confirm_item(data_dir, first_id, reviewer="tester", note="ok")
            rejected = reject_confirm_item(data_dir, second_id, reviewer="tester", note="no")
            send_result = send_approved_confirm_item(data_dir, first_id)

            self.assertEqual(pending["count"], 2)
            self.assertEqual(approved["item"]["status"], "approved")
            self.assertEqual(rejected["item"]["status"], "rejected")
            self.assertEqual(send_result["status"], "blocked")
            self.assertEqual(send_result["send_result"]["reason"], "send_enabled_false")
            self.assertEqual(queue.get(first_id)["status"], "approved")
            audit = list_send_audit(data_dir, limit=10)
            actions = [item["action"] for item in audit["items"]]
            self.assertIn("confirm_approve", actions)
            self.assertIn("confirm_reject", actions)
            self.assertIn("confirm_send_blocked", actions)

    def test_clear_send_audit_truncates_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-1", "hello"))
            approve_confirm_item(data_dir, queue_id, reviewer="tester", note="ok")
            self.assertGreater(list_send_audit(data_dir, limit=10)["count"], 0)

            result = clear_send_audit(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["cleared_count"], 1)
            self.assertEqual(list_send_audit(data_dir, limit=10)["items"], [])

    def test_clear_send_audit_waits_for_jsonl_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            audit_path = data_dir / "send_audit.jsonl"
            audit = SendAuditLog(audit_path)
            audit.append("confirm_approve", queue_id="queue-1", status="approved")

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                with jsonl_operation_lock(audit_path):
                    future = executor.submit(clear_send_audit, data_dir)
                    time.sleep(0.05)
                    self.assertFalse(future.done())
                result = future.result(timeout=2)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["cleared_count"], 1)
            self.assertEqual(list_send_audit(data_dir, limit=10)["items"], [])

    def test_send_audit_jsonl_projection_does_not_repopulate_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            path = data_dir / "send_audit.jsonl"
            path.write_text(
                'not-json\n{"action":"confirm_approve","queue_id":"queue-1","status":"approved"}\n',
                encoding="utf-8",
            )

            result = list_send_audit(data_dir, limit=10)
            SendAuditLog(path).append("confirm_reject", queue_id="queue-2", status="rejected")
            current = list_send_audit(data_dir, limit=10)

            self.assertEqual(result["count"], 0)
            self.assertEqual(current["count"], 1)
            self.assertEqual(current["items"][0]["action"], "confirm_reject")

    def test_remove_confirm_item_deletes_only_queue_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            first_reply = _reply("message-1", "hello")
            entry = ConversationLedgerStore(data_dir).append_reply(first_reply)
            first_id = queue.enqueue(first_reply)
            second_id = queue.enqueue(_reply("message-2", "bye"))

            removed = remove_confirm_item(data_dir, first_id, reviewer="tester", note="cleanup")

            self.assertEqual(removed["status"], "ok")
            self.assertEqual(removed["ledger_sync_error"], "")
            self.assertTrue(removed["removed"])
            self.assertIsNone(queue.get(first_id))
            self.assertIsNotNone(queue.get(second_id))
            updated = ConversationLedgerStore(data_dir).read_entries(first_reply.conversation_id)[-1]
            self.assertEqual(updated.entry_id, entry.entry_id)
            self.assertEqual(updated.send["status"], "removed")
            self.assertEqual(updated.send["reason"], "cleanup")
            audit = list_send_audit(data_dir, limit=5)
            self.assertIn("confirm_remove", [item["action"] for item in audit["items"]])

    def test_send_approved_confirm_item_calls_driver_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="fake")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-1", "hello"))
            queue.approve(queue_id, reviewer="tester")
            driver = _SendingDriver()

            result = send_approved_confirm_item(data_dir, queue_id, driver=driver)

            self.assertEqual(result["status"], "sent")
            self.assertEqual(driver.sent_texts, ["hello"])
            self.assertEqual(queue.get(queue_id)["status"], "sent")

    def test_send_approved_confirm_item_updates_reply_ledger_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="fake")
            reply = _reply("message-1", "hello")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_reply(reply)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")

            result = send_approved_confirm_item(data_dir, queue_id, driver=_SendingDriver())
            updated = ledger.read_entries("private-1")[0]

            self.assertEqual(result["status"], "sent")
            self.assertEqual(updated.send["status"], "sent")
            self.assertEqual(updated.send["reason"], "fake_sent")
            self.assertEqual(updated.send["message_id"], "sent-id")

    def test_send_approved_confirm_item_survives_ledger_sync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="fake")
            reply = _reply("message-1", "hello")
            ConversationLedgerStore(data_dir).append_reply(reply)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")

            with mock.patch.object(
                ConversationLedgerStore,
                "update_reply_send_result_for_candidate",
                side_effect=OSError("ledger locked"),
            ):
                result = send_approved_confirm_item(data_dir, queue_id, driver=_SendingDriver())

            # The send succeeded even though the ledger sync raised.
            self.assertEqual(result["status"], "sent")
            self.assertIn("OSError", result["ledger_sync_error"])
            self.assertEqual(queue.get(queue_id)["status"], "sent")
            audit = list_send_audit(data_dir)
            self.assertTrue(
                any(item.get("action") == "ledger_sync_failed" for item in audit["items"]),
                msg=f"expected ledger_sync_failed audit entry, got {audit['items']}",
            )

    def test_list_send_audit_keeps_unresolved_ledger_sync_failure_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            audit = SendAuditLog(data_dir / "send_audit.jsonl")
            audit.append(
                "ledger_sync_failed",
                queue_id="queue-1",
                status="queued_to_bridge",
                reason="OSError: ledger locked",
            )

            visible = list_send_audit(data_dir, limit=10)

            self.assertEqual(visible["count"], 1)
            self.assertEqual(visible["items"][0]["action"], "ledger_sync_failed")
            self.assertFalse(visible["items"][0]["resolved"])
            self.assertTrue(visible["items"][0]["problem"])
            self.assertEqual(visible["items"][0]["severity"], "error")
            self.assertEqual(visible["items"][0]["audit_state"], "open_error")

    def test_list_send_audit_hides_resolved_ledger_sync_failure_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            audit = SendAuditLog(data_dir / "send_audit.jsonl")
            audit.append(
                "ledger_sync_failed",
                queue_id="queue-1",
                status="queued_to_bridge",
                reason="NameError: name '_compact' is not defined",
            )
            audit.append(
                "confirm_remove",
                queue_id="queue-1",
                status="sent",
                note="sidebar_remove_queue_item",
            )

            visible = list_send_audit(data_dir, limit=10)
            full_history = list_send_audit(data_dir, limit=10, include_resolved=True)
            resolved = next(item for item in full_history["items"] if item["action"] == "ledger_sync_failed")

            self.assertEqual([item["action"] for item in visible["items"]], ["confirm_remove"])
            self.assertFalse(visible["items"][0]["problem"])
            self.assertEqual(visible["items"][0]["severity"], "info")
            self.assertEqual(visible["items"][0]["audit_state"], "history")
            self.assertTrue(resolved["resolved"])
            self.assertEqual(resolved["resolved_by"], "confirm_remove")
            self.assertFalse(resolved["problem"])
            self.assertEqual(resolved["severity"], "resolved")

    def test_list_send_audit_can_compact_queue_transition_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            audit = SendAuditLog(data_dir / "send_audit.jsonl")
            audit.append("confirm_approve", queue_id="queue-1", status="approved")
            audit.append("confirm_send_attempt", queue_id="queue-1", status="queued_to_bridge")
            audit.append("confirm_remove", queue_id="queue-1", status="sent", note="cleanup")
            audit.append(
                "ledger_sync_failed",
                queue_id="queue-2",
                status="approved",
                reason="OSError: ledger locked",
            )
            audit.append("confirm_approve", queue_id="queue-2", status="approved")

            compacted = list_send_audit(data_dir, limit=10, compact_transitions=True)

            self.assertEqual(
                [(item["queue_id"], item["action"]) for item in compacted["items"]],
                [("queue-1", "confirm_remove"), ("queue-2", "ledger_sync_failed"), ("queue-2", "confirm_approve")],
            )
            self.assertFalse(compacted["items"][0]["problem"])
            self.assertEqual(compacted["items"][0]["severity"], "info")

    def test_list_send_audit_marks_removed_failed_queue_as_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            audit = SendAuditLog(data_dir / "send_audit.jsonl")
            audit.append("confirm_send_attempt", queue_id="queue-1", status="failed", reason="old bridge failure")
            audit.append("confirm_remove", queue_id="queue-1", status="failed", note="sidebar cleanup")

            compacted = list_send_audit(data_dir, limit=10, compact_transitions=True)

            self.assertEqual([(item["queue_id"], item["action"]) for item in compacted["items"]], [("queue-1", "confirm_remove")])
            self.assertFalse(compacted["items"][0]["problem"])
            self.assertEqual(compacted["items"][0]["severity"], "info")
            self.assertEqual(compacted["items"][0]["audit_state"], "history")

    def test_successful_later_ledger_sync_records_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            reply = _reply("message-1", "hello")
            ConversationLedgerStore(data_dir).append_reply(reply)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            audit = SendAuditLog(data_dir / "send_audit.jsonl")
            audit.append(
                "ledger_sync_failed",
                queue_id=queue_id,
                status="approved",
                reason="OSError: ledger locked",
            )

            approve_confirm_item(data_dir, queue_id, reviewer="tester", note="retry")
            full_history = list_send_audit(data_dir, limit=10, include_resolved=True)

            actions = [item["action"] for item in full_history["items"]]
            self.assertIn("ledger_sync_recovered", actions)
            failure = next(item for item in full_history["items"] if item["action"] == "ledger_sync_failed")
            self.assertTrue(failure["resolved"])
            self.assertEqual(failure["resolved_by"], "ledger_sync_recovered")

    def test_send_approved_confirm_item_blocks_pending_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-1", "hello"))

            result = send_approved_confirm_item(data_dir, queue_id, driver=_SendingDriver())

            self.assertEqual(result["status"], "blocked")
            self.assertIn("pending", result["reason"])
            self.assertEqual(queue.get(queue_id)["status"], "pending")

    def test_send_approved_confirm_item_uses_factory_and_blocks_unknown_driver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="unknown-real-driver")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-1", "hello"))
            queue.approve(queue_id, reviewer="tester")

            result = send_approved_confirm_item(data_dir, queue_id)

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["send_result"]["reason"], "send_driver_missing")
            self.assertEqual(queue.get(queue_id)["status"], "approved")

    def test_send_approved_confirm_item_can_queue_to_bridge_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            _register_authorized_private_channel(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-1", "hello bridge"))
            queue.approve(queue_id, reviewer="tester")

            result = send_approved_confirm_item(data_dir, queue_id)
            bridge = bridge_state(data_dir, limit=10)

            self.assertEqual(result["status"], "queued_to_bridge")
            self.assertEqual(result["send_result"]["status"], "queued_to_bridge")
            self.assertEqual(queue.get(queue_id)["status"], "queued_to_bridge")
            self.assertEqual(bridge["pending_count"], 1)
            self.assertEqual(bridge["items"][0]["text"], "hello bridge")

    def test_bridge_send_task_is_finished_by_terminal_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            _register_authorized_private_channel(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            reply = _reply("message-bridge-task", "hello bridge")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")

            result = send_approved_confirm_item(data_dir, queue_id)
            bridge_id = result["send_result"]["message_id"]
            queued = next(item for item in TaskStatusStore(data_dir).state()["tasks"] if item["task_id"] == "send-message-bridge-task")

            self.assertEqual(queued["status"], "queued")
            self.assertEqual(queued["external_id"], bridge_id)

            sync_bridge_ack_to_send_state(data_dir, bridge_id, status=BridgeAckStatus.SENT, reason="native_sent")
            completed = next(item for item in TaskStatusStore(data_dir).state()["tasks"] if item["task_id"] == "send-message-bridge-task")

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["progress"], 100)

    def test_multi_part_bridge_ack_keeps_queue_and_task_open_until_all_parts_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            _register_authorized_private_channel(data_dir)
            file_path = Path(tmp) / "probe.txt"
            file_path.write_text("file payload", encoding="utf-8")
            reply = ReplyCandidate(
                message_id="message-multipart",
                conversation_id="private-1",
                text="hello with file",
                send_mode="confirm",
                model="fake",
                attachments=[{"path": str(file_path), "name": "probe.txt"}],
            )
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_reply(reply)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")

            result = send_approved_confirm_item(data_dir, queue_id)
            details = result["send_result"]["details"]
            text_bridge_id = details["text"]["message_id"]
            file_bridge_id = details["files"][0]["message_id"]

            queued_task = next(item for item in TaskStatusStore(data_dir).state()["tasks"] if item["task_id"] == "send-message-multipart")
            self.assertEqual(queued_task["status"], "queued")
            self.assertEqual(queued_task["metadata"]["bridge_ids"], [text_bridge_id, file_bridge_id])

            sync_bridge_ack_to_send_state(
                data_dir,
                file_bridge_id,
                status=BridgeAckStatus.SENT,
                reason="wechat_native_http_send_file_verified",
                external_message_id="ext-file",
            )
            after_file_ack = queue.get(queue_id)
            task_after_file_ack = next(item for item in TaskStatusStore(data_dir).state()["tasks"] if item["task_id"] == "send-message-multipart")
            entry_after_file_ack = ledger.read_entries("private-1")[0]

            self.assertEqual(after_file_ack["status"], "queued_to_bridge")
            self.assertEqual(after_file_ack["bridge_acks"][file_bridge_id]["status"], "sent")
            self.assertEqual(task_after_file_ack["status"], "queued")
            self.assertEqual(entry_after_file_ack.send["status"], "queued_to_bridge")
            self.assertEqual(entry_after_file_ack.attachments[0]["send"]["status"], "sent")

            sync_bridge_ack_to_send_state(
                data_dir,
                text_bridge_id,
                status=BridgeAckStatus.SENT,
                reason="wechat_native_http_send_text_verified",
                external_message_id="ext-text",
            )
            completed_task = next(item for item in TaskStatusStore(data_dir).state()["tasks"] if item["task_id"] == "send-message-multipart")
            completed_entry = ledger.read_entries("private-1")[0]

            self.assertEqual(queue.get(queue_id)["status"], "sent")
            self.assertEqual(completed_task["status"], "completed")
            self.assertEqual(completed_task["progress"], 100)
            self.assertEqual(completed_entry.send["status"], "sent")
            self.assertEqual(completed_entry.send["details"]["text"]["external_message_id"], "ext-text")

    def test_concurrent_multi_part_bridge_acks_keep_all_queue_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            _register_authorized_private_channel(data_dir)
            file_path = Path(tmp) / "probe.txt"
            file_path.write_text("file payload", encoding="utf-8")
            reply = ReplyCandidate(
                message_id="message-multipart-concurrent",
                conversation_id="private-1",
                text="hello with file",
                send_mode="confirm",
                model="fake",
                attachments=[{"path": str(file_path), "name": "probe.txt"}],
            )
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_reply(reply)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")

            result = send_approved_confirm_item(data_dir, queue_id)
            text_bridge_id = result["send_result"]["details"]["text"]["message_id"]
            file_bridge_id = result["send_result"]["details"]["files"][0]["message_id"]

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        sync_bridge_ack_to_send_state,
                        data_dir,
                        text_bridge_id,
                        status=BridgeAckStatus.SENT,
                        reason="wechat_native_http_send_text_verified",
                        external_message_id="ext-text",
                    ),
                    executor.submit(
                        sync_bridge_ack_to_send_state,
                        data_dir,
                        file_bridge_id,
                        status=BridgeAckStatus.SENT,
                        reason="wechat_native_http_send_file_verified",
                        external_message_id="ext-file",
                    ),
                ]
                results = [future.result(timeout=5) for future in futures]

            self.assertEqual([item["status"] for item in results], ["ok", "ok"])
            queue_item = queue.get(queue_id)
            self.assertEqual(queue_item["status"], "sent")
            self.assertEqual(set(queue_item["bridge_acks"].keys()), {text_bridge_id, file_bridge_id})
            task = next(
                item
                for item in TaskStatusStore(data_dir).state()["tasks"]
                if item["task_id"] == "send-message-multipart-concurrent"
            )
            self.assertEqual(task["status"], "completed")
            entry = ledger.read_entries("private-1")[0]
            self.assertEqual(entry.send["status"], "sent")
            self.assertEqual(entry.send["details"]["text"]["external_message_id"], "ext-text")
            self.assertEqual(entry.attachments[0]["send"]["external_message_id"], "ext-file")

    def test_retry_bridge_item_requeues_queue_ledger_and_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            _register_authorized_private_channel(data_dir)
            reply = _reply("message-retry", "hello bridge")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_reply(reply, chat_title="Alice")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")

            sent = send_approved_confirm_item(data_dir, queue_id)
            old_bridge_id = sent["send_result"]["message_id"]
            BridgeOutboxStore(data_dir).append_ack(
                old_bridge_id,
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
            )
            projection_dir = ledger.conversation_markdown_path("private-1").parent
            (projection_dir / "messages.jsonl").unlink()
            (projection_dir / "conversation.md").unlink()

            retried = retry_bridge_item(data_dir, old_bridge_id, reviewer="tester", note="test_retry")
            new_bridge_id = retried["new_bridge_id"]
            queue_item = queue.get(queue_id)
            entry = ledger.read_entries("private-1")[0]
            task = next(item for item in TaskStatusStore(data_dir).state()["tasks"] if item["task_id"] == "send-message-retry")

            self.assertNotEqual(new_bridge_id, old_bridge_id)
            self.assertEqual(queue_item["status"], "queued_to_bridge")
            self.assertIn(new_bridge_id, queue_item["note"])
            self.assertEqual(entry.send["status"], "queued_to_bridge")
            self.assertEqual(entry.send["message_id"], new_bridge_id)
            self.assertEqual(entry.send["retry_of"], old_bridge_id)
            self.assertEqual(task["status"], "queued")
            self.assertEqual(task["external_id"], new_bridge_id)
            self.assertEqual(task["metadata"]["bridge_id"], new_bridge_id)
            self.assertEqual(bridge_state(data_dir, limit=20)["pending_count"], 1)

    def test_send_approved_confirm_item_bridge_queues_by_channel_receiver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            _register_authorized_private_channel(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-1", "hello bridge"))
            queue.approve(queue_id, reviewer="tester")

            result = send_approved_confirm_item(data_dir, queue_id)
            bridge = bridge_state(data_dir, limit=10)

            self.assertEqual(result["status"], "queued_to_bridge")
            self.assertEqual(queue.get(queue_id)["status"], "queued_to_bridge")
            self.assertEqual(bridge["pending_count"], 1)
            self.assertEqual(bridge["items"][0]["text"], "hello bridge")

    def test_send_approved_confirm_item_keeps_approved_when_native_backend_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="wechat_native_http")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-1", "hello bridge"))
            queue.approve(queue_id, reviewer="tester")

            with mock.patch(
                "app.personal_wechat_bot.wechat_driver.bridge_send.wechat_native_http_status",
                return_value={"available": False, "reason": "ConnectionRefusedError:refused"},
            ):
                result = send_approved_confirm_item(data_dir, queue_id)

            self.assertEqual(result["status"], "blocked")
            self.assertIn("wechat_native_backend_unavailable", result["reason"])
            self.assertEqual(queue.get(queue_id)["status"], "approved")

    def test_nonterminal_bridge_ack_does_not_mark_queue_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            bridge_id = "bridge:private-1:test"
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-1", "hello bridge"))
            queue.approve(queue_id, reviewer="tester")
            queue.mark_send_result(queue_id, "queued_to_bridge", f"queued_to_non_foreground_bridge:{bridge_id}")

            result = sync_bridge_ack_to_send_state(
                data_dir,
                bridge_id,
                status=BridgeAckStatus.RETRY,
                reason="native_unavailable",
            )

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "bridge_ack_not_terminal")
            self.assertEqual(queue.get(queue_id)["status"], "queued_to_bridge")

    def test_accepted_bridge_ack_marks_queue_and_ledger_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            bridge_id = "bridge:private-1:test"
            reply = _reply("message-1", "hello bridge")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_reply(reply)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")
            queue.mark_send_result(queue_id, "queued_to_bridge", f"queued_to_non_foreground_bridge:{bridge_id}")

            result = sync_bridge_ack_to_send_state(
                data_dir,
                bridge_id,
                status=BridgeAckStatus.ACCEPTED,
                reason="wechat_native_http_send_text_accepted_unverified",
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(queue.get(queue_id)["status"], "accepted")
            entry = ledger.read_entries("private-1")[0]
            self.assertEqual(entry.send["status"], "accepted")
            self.assertEqual(entry.send.get("sent_at", ""), "")
            audit = list_send_audit(data_dir, limit=5)
            bridge_events = [item for item in audit["items"] if item.get("action") == "bridge_ack_sync"]
            self.assertEqual(len(bridge_events), 1)
            self.assertEqual(bridge_events[0]["queue_id"], queue_id)
            self.assertEqual(bridge_events[0]["status"], "accepted")
            self.assertEqual(bridge_events[0]["audit_state"], "accepted_unverified")
            self.assertEqual(bridge_events[0]["severity"], "warning")
            self.assertFalse(bridge_events[0]["problem"])
            self.assertEqual(bridge_events[0]["payload"]["ack_status"], BridgeAckStatus.ACCEPTED)
            self.assertEqual(bridge_events[0]["payload"]["bridge_id"], bridge_id)

    def test_sent_bridge_ack_repairs_failed_queue_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            bridge_id = "bridge:private-1:test"
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-1", "hello bridge"))
            queue.approve(queue_id, reviewer="tester")
            queue.mark_send_result(queue_id, "queued_to_bridge", f"queued_to_non_foreground_bridge:{bridge_id}")
            queue.mark_send_result(queue_id, "failed", f"old_failure:{bridge_id}")

            result = sync_bridge_ack_to_send_state(
                data_dir,
                bridge_id,
                status=BridgeAckStatus.SENT,
                reason="native_sent",
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["queue_sync_status"], "updated")
            self.assertEqual(queue.get(queue_id)["status"], "sent")

    def test_failed_bridge_ack_does_not_downgrade_sent_queue_or_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            bridge_id = "bridge:private-1:test"
            reply = _reply("message-1", "hello bridge")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_reply(reply)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")
            queue.mark_send_result(queue_id, "queued_to_bridge", f"queued_to_non_foreground_bridge:{bridge_id}")
            queue.mark_send_result(queue_id, "sent", f"native_sent:{bridge_id}")

            result = sync_bridge_ack_to_send_state(
                data_dir,
                bridge_id,
                status=BridgeAckStatus.FAILED,
                reason="stale_failed",
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["queue_sync_status"], "preserved_sent")
            self.assertEqual(queue.get(queue_id)["status"], "sent")
            entry = ledger.read_entries("private-1")[0]
            self.assertEqual(entry.send["status"], "sent")
            self.assertEqual(entry.send["reason"], f"native_sent:{bridge_id}")

    def test_bridge_ack_fanout_updates_ledger_under_readable_segment_without_queue_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            bridge_id = "bridge:private-1:test"
            reply = _reply("message-1", "hello bridge")
            ledger = ConversationLedgerStore(data_dir)
            entry = ledger.append_reply(reply, chat_title="Alice")
            ledger.update_reply_send_result(
                "private-1",
                entry.entry_id,
                {
                    "message_id": bridge_id,
                    "status": "queued_to_bridge",
                    "reason": f"queued_to_non_foreground_bridge:{bridge_id}",
                },
            )
            projection_dir = ledger.conversation_markdown_path("private-1").parent
            (projection_dir / "messages.jsonl").unlink()
            (projection_dir / "conversation.md").unlink()

            result = sync_bridge_ack_to_send_state(
                data_dir,
                bridge_id,
                status=BridgeAckStatus.SENT,
                reason="native_sent",
            )

            self.assertEqual(result["status"], "ok")
            self.assertFalse(result["queue_item_found"])
            self.assertEqual(result["ledger_updates"], ["private-1"])
            updated = ledger.read_entries("private-1")[0]
            self.assertEqual(updated.send["status"], "sent")
            self.assertEqual(updated.send["reason"], "native_sent")
            self.assertTrue((projection_dir / "messages.jsonl").exists())
            self.assertTrue((projection_dir / "conversation.md").exists())

    def test_probe_send_controls_reports_configured_driver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=False, driver="bridge_outbox")

            result = probe_send_controls(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["probe"]["normalized_driver"], "bridge_outbox")
            self.assertTrue(result["probe"]["registered"])
            self.assertTrue(result["probe"]["real_send_implemented"])

    def test_probe_send_controls_can_override_driver_without_persisting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            result = probe_send_controls(data_dir, driver="bridge_outbox")
            config = load_config(data_dir)

            self.assertEqual(result["probe"]["normalized_driver"], "bridge_outbox")
            self.assertEqual(config.send_driver, "bridge_outbox")

    def test_set_send_controls_cli_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._run("--data-dir", str(data_dir), "init")

            payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "set-send-controls",
                    "--enable",
                    "--driver",
                    "fake",
                    "--confirm-required",
                    "false",
                    "--max-chars",
                    "64",
                    "--min-interval-seconds",
                    "2",
                )
            )
            config = load_config(data_dir)

            self.assertEqual(payload["status"], "ok")
            self.assertTrue(config.send_enabled)
            self.assertEqual(config.send_driver, "fake")
            self.assertFalse(config.send_confirm_required)
            self.assertEqual(config.send_max_chars, 64)
            self.assertEqual(config.send_min_interval_seconds, 2)

    def test_set_send_controls_cli_updates_wechat_native_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._run("--data-dir", str(data_dir), "init")

            payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "set-send-controls",
                    "--enable",
                    "--driver",
                    "bridge_outbox",
                    "--backend",
                    "wechat_native_http",
                    "--wechat-native-send-text-path",
                    "/custom-text",
                    "--wechat-native-verify-timeout-seconds",
                    "2.5",
                    "--wechat-native-file-verify-timeout-seconds",
                    "33",
                )
            )
            config = load_config(data_dir)

            self.assertEqual(payload["send_controls"]["send_backend"], "wechat_native_http")
            self.assertEqual(payload["send_controls"]["wechat_native_file_verify_timeout_seconds"], 33.0)
            self.assertEqual(config.send_backend, "wechat_native_http")
            self.assertEqual(config.wechat_native_send_text_path, "/custom-text")
            self.assertEqual(config.wechat_native_verify_timeout_seconds, 2.5)
            self.assertEqual(config.wechat_native_file_verify_timeout_seconds, 33.0)

    def test_confirm_cli_approve_reject_and_send_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._run("--data-dir", str(data_dir), "init")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            first_id = queue.enqueue(_reply("message-1", "hello"))
            second_id = queue.enqueue(_reply("message-2", "bye"))

            pending = json.loads(self._run("--data-dir", str(data_dir), "confirm-list"))
            approved = json.loads(
                self._run("--data-dir", str(data_dir), "confirm-approve", first_id, "--reviewer", "tester")
            )
            rejected = json.loads(
                self._run("--data-dir", str(data_dir), "confirm-reject", second_id, "--reviewer", "tester")
            )
            send_result = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "confirm-send-approved",
                    first_id,
                )
            )
            failed = json.loads(self._run("--data-dir", str(data_dir), "confirm-list", "--status", "failed"))
            approved_after_send = json.loads(
                self._run("--data-dir", str(data_dir), "confirm-list", "--status", "approved")
            )

            self.assertEqual(pending["count"], 2)
            self.assertEqual(approved["item"]["status"], "approved")
            self.assertEqual(rejected["item"]["status"], "rejected")
            self.assertEqual(send_result["status"], "blocked")
            self.assertEqual(send_result["send_result"]["reason"], "send_enabled_false")
            self.assertEqual(failed["count"], 0)
            self.assertEqual(approved_after_send["count"], 1)
            audit = json.loads(self._run("--data-dir", str(data_dir), "send-audit", "--limit", "5"))
            self.assertGreaterEqual(audit["count"], 3)

    def test_send_driver_probe_cli_returns_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._run("--data-dir", str(data_dir), "init")
            self._run("--data-dir", str(data_dir), "set-send-controls", "--driver", "bridge_outbox")

            payload = json.loads(
                self._run("--data-dir", str(data_dir), "send-driver-probe")
            )

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["probe"]["normalized_driver"], "bridge_outbox")
            self.assertTrue(payload["probe"]["registered"])

    def test_send_driver_probe_cli_can_override_driver_without_persisting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._run("--data-dir", str(data_dir), "init")

            payload = json.loads(
                self._run("--data-dir", str(data_dir), "send-driver-probe", "--driver", "bridge_outbox")
            )
            config = load_config(data_dir)

            self.assertEqual(payload["probe"]["normalized_driver"], "bridge_outbox")
            self.assertEqual(config.send_driver, "bridge_outbox")

    def test_native_migration_probe_cli_forwards_portable_probe_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            with mock.patch(
                "app.personal_wechat_bot.control.cli.sidebar_native_migration_probe",
                return_value={"schema": "native_migration_probe_v1", "status": "ready", "report_path": ""},
            ) as probe:
                from app.personal_wechat_bot.control.cli import main as cli_main

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    cli_main(
                        [
                            "--data-dir",
                            str(data_dir),
                            "native-migration-probe",
                            "--no-persist",
                            "--force-scan",
                            "--timeout-seconds",
                            "1.25",
                            "--max-depth",
                            "2",
                            "--max-entries",
                            "100",
                            "--limit",
                            "3",
                            "--no-cleanup-sizes",
                        ]
                    )

            result = json.loads(stdout.getvalue())
            called_data_dir, called_payload = probe.call_args.args

            self.assertEqual(result["schema"], "native_migration_probe_v1")
            self.assertEqual(called_data_dir, str(data_dir))
            self.assertFalse(called_payload["persist"])
            self.assertTrue(called_payload["force_scan"])
            self.assertFalse(called_payload["include_cleanup_sizes"])
            self.assertEqual(called_payload["timeout_seconds"], 1.25)
            self.assertEqual(called_payload["max_depth"], 2)
            self.assertEqual(called_payload["max_entries"], 100)
            self.assertEqual(called_payload["limit"], 3)

    def test_send_bridge_cli_returns_state_and_accepts_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._run("--data-dir", str(data_dir), "init")

            state = json.loads(self._run("--data-dir", str(data_dir), "send-bridge-state"))
            ack = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "send-bridge-ack",
                    "bridge:test",
                    "--status",
                    "sent",
                    "--reason",
                    "manual",
                )
            )
            updated = json.loads(self._run("--data-dir", str(data_dir), "send-bridge-state"))

            self.assertEqual(state["status"], "ok")
            self.assertEqual(ack["status"], "ok")
            self.assertEqual(updated["ack_count"], 1)

    def test_send_bridge_retry_cli_requeues_failed_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._run("--data-dir", str(data_dir), "init")
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("private-1", "retry from cli")
            store.append_ack(
                rec["bridge_id"],
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
            )

            retry = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "send-bridge-retry",
                    rec["bridge_id"],
                    "--note",
                    "cli_retry",
                )
            )
            state = json.loads(self._run("--data-dir", str(data_dir), "send-bridge-state", "--limit", "10"))

            self.assertEqual(retry["status"], "ok")
            self.assertNotEqual(retry["new_bridge_id"], rec["bridge_id"])
            self.assertTrue(any(item["bridge_id"] == retry["new_bridge_id"] for item in state["items"]))
            self.assertEqual(state["pending_count"], 1)

    def _run(self, *args: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-m", "app.personal_wechat_bot.main", *args],
            cwd=ROOT.parent,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )
        return completed.stdout


def _reply(message_id: str, text: str) -> ReplyCandidate:
    return ReplyCandidate(
        message_id=message_id,
        conversation_id="private-1",
        text=text,
        send_mode="confirm",
        model="fake",
    )


def _register_authorized_private_channel(
    data_dir: Path,
    *,
    conversation_id: str = "private-1",
    receiver: str = "wxid_test_friend",
) -> None:
    chat_title = "Test Friend"
    segment = conversation_segment(conversation_id, chat_title)
    channel_dir = data_dir / "conversation_channels" / segment
    channel_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "conversation_id": conversation_id,
        "conversation_type": "private",
        "chat_title": chat_title,
        "conversation_key": receiver,
        "sender_wechat_ids": [receiver],
        "source_names": ["manual_backend_event"],
        "trusted_channel_source": True,
        "is_friend": True,
        "contact_authorization": "explicit_friend",
        "segment": segment,
    }
    ChannelRegistryStore(data_dir).upsert(payload)
    (channel_dir / "channel.json").write_text(json.dumps(payload), encoding="utf-8")
    (data_dir / "conversation_channels" / "index.json").write_text(
        json.dumps({"channels": [{"conversation_id": conversation_id, "chat_title": chat_title}]}),
        encoding="utf-8",
    )


class _SendingDriver:
    def __init__(self) -> None:
        self.sent_texts: list[str] = []

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        self.sent_texts.append(text)
        return SendResult(message_id="sent-id", conversation_id=conversation_id, status="sent", reason="fake_sent")


if __name__ == "__main__":
    unittest.main()

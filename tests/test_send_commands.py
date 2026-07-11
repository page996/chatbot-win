from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import multiprocessing
import subprocess
import sys
import tempfile
import threading
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
            reply = _reply("message-1", "hello")
            ConversationLedgerStore(data_dir).append_reply(reply)
            queue_id = queue.enqueue(reply)
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

    def test_concurrent_send_approved_claims_once_and_queues_one_outbox_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            _register_authorized_private_channel(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            reply = _reply("message-concurrent", "hello bridge")
            ConversationLedgerStore(data_dir).append_reply(reply)
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")

            from app.personal_wechat_bot.config.loader import load_config
            from app.personal_wechat_bot.wechat_driver.send_driver_factory import build_send_driver

            delegate = build_send_driver(load_config(data_dir))
            driver = _BlockingDelegatingDriver(delegate)
            first_started = threading.Event()
            driver.entered = first_started
            release = threading.Event()
            driver.release = release

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(send_approved_confirm_item, data_dir, queue_id, driver)
                self.assertTrue(first_started.wait(10.0), "first sender did not reach driver")
                second_future = pool.submit(send_approved_confirm_item, data_dir, queue_id, driver)
                second = second_future.result(timeout=10.0)
                self.assertEqual(second["status"], "blocked")
                self.assertTrue(second["claim_conflict"])
                release.set()
                first = first_future.result(timeout=20.0)

            state = BridgeOutboxStore(data_dir).state(limit=10)
            self.assertEqual(first["status"], "queued_to_bridge")
            self.assertEqual(driver.calls, 1)
            self.assertEqual(state["pending_count"], 1)
            self.assertEqual(len(state["items"]), 1)
            self.assertEqual(queue.get(queue_id)["status"], "queued_to_bridge")
            self.assertNotIn("send_claim", queue.get(queue_id))

    def test_terminal_race_before_activation_resyncs_current_projection_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            _register_authorized_private_channel(data_dir)
            reply = _reply("message-terminal-race", "projection must become failed")
            ConversationLedgerStore(data_dir).append_reply(reply)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")

            from app.personal_wechat_bot.config.loader import load_config
            from app.personal_wechat_bot.wechat_driver.send_driver_factory import build_send_driver

            delegate = build_send_driver(load_config(data_dir))
            result = send_approved_confirm_item(
                data_dir,
                queue_id,
                driver=_TerminalBeforeActivationDriver(data_dir, delegate),
            )

            item = queue.get(queue_id)
            outbox_item = BridgeOutboxStore(data_dir)._read_all(
                BridgeOutboxStore(data_dir).outbox_path
            )[0]
            self.assertEqual(result["status"], "failed")
            self.assertEqual(item["status"], "failed")
            self.assertEqual(outbox_item["expected_projections"], ["queue", "ledger", "task"])
            ledger_entry = ConversationLedgerStore(data_dir).read_entries(reply.conversation_id)[-1]
            self.assertEqual(ledger_entry.send["status"], "failed")

    def test_send_claim_conflict_is_cross_process_and_only_one_driver_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="fake")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-process-race", "hello"))
            queue.approve(queue_id, reviewer="tester")

            context = multiprocessing.get_context("spawn")
            start = context.Event()
            entered = context.Event()
            release = context.Event()
            calls = context.Value("i", 0)
            ready = context.Queue()
            results = context.Queue()
            processes = [
                context.Process(
                    target=_send_approved_process_worker,
                    args=(str(data_dir), queue_id, start, entered, release, calls, ready, results),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            try:
                self.assertEqual({ready.get(timeout=20.0), ready.get(timeout=20.0)}, {"ready"})
                start.set()
                self.assertTrue(entered.wait(20.0), "cross-process winner did not reach driver")
                first_result = results.get(timeout=20.0)
                self.assertEqual(first_result["status"], "blocked")
                self.assertTrue(first_result["claim_conflict"])
                release.set()
                second_result = results.get(timeout=20.0)
                self.assertEqual(second_result["status"], "sent")
            finally:
                release.set()
                for process in processes:
                    process.join(timeout=20.0)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5.0)

            self.assertEqual(calls.value, 1)
            self.assertEqual(queue.get(queue_id)["status"], "sent")

    def test_safe_send_failure_releases_claim_for_a_later_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-retry-claim", "hello"))
            queue.approve(queue_id, reviewer="tester")

            blocked = send_approved_confirm_item(data_dir, queue_id, driver=_SendingDriver())
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["reason"], "send_enabled_false")
            self.assertEqual(queue.get(queue_id)["status"], "approved")
            self.assertNotIn("send_claim", queue.get(queue_id))

            set_send_controls(data_dir, enabled=True, driver="fake")
            retried_driver = _SendingDriver()
            retried = send_approved_confirm_item(data_dir, queue_id, driver=retried_driver)
            self.assertEqual(retried["status"], "sent")
            self.assertEqual(retried_driver.sent_texts, ["hello"])

    def test_driver_exception_consumes_claim_without_allowing_duplicate_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="fake")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-driver-error", "hello"))
            queue.approve(queue_id, reviewer="tester")
            failing = _RaisingDriver()

            result = send_approved_confirm_item(data_dir, queue_id, driver=failing)
            self.assertEqual(result["status"], "failed")
            self.assertIn("outcome_unknown", result["send_result"]["reason"])
            self.assertEqual(queue.get(queue_id)["status"], "failed")
            self.assertNotIn("send_claim", queue.get(queue_id))

            retry = send_approved_confirm_item(data_dir, queue_id, driver=_SendingDriver())
            self.assertEqual(retry["status"], "blocked")
            self.assertIn("failed", retry["reason"])
            self.assertEqual(failing.calls, 1)

    def test_dead_send_claim_is_failed_and_projected_without_calling_driver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="fake")
            reply = _reply("message-dead-claim", "hello")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_reply(reply)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")
            queue.claim_approved_for_send(queue_id, owner="exited-sender")
            driver = _SendingDriver()

            with mock.patch(
                "app.personal_wechat_bot.reply_gate.confirm_queue._send_claim_owner_is_alive",
                return_value=False,
            ):
                result = send_approved_confirm_item(data_dir, queue_id, driver=driver)

            entry = ledger.read_entries(reply.conversation_id)[0]
            task = next(
                item
                for item in TaskStatusStore(data_dir).state()["tasks"]
                if item["task_id"] == "send-message-dead-claim"
            )
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["outcome_unknown"])
            self.assertEqual(driver.sent_texts, [])
            self.assertEqual(queue.get(queue_id)["status"], "failed")
            self.assertEqual(entry.send["status"], "failed")
            self.assertEqual(task["status"], "failed")
            audit = list_send_audit(data_dir, include_resolved=True)
            self.assertTrue(
                any(
                    item.get("action") == "confirm_send_attempt"
                    and item.get("reason") == "send_claim_owner_exited_outcome_unknown"
                    for item in audit["items"]
                )
            )

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
            failure = next(
                (item for item in audit["items"] if item.get("action") == "ledger_sync_failed"),
                None,
            )
            self.assertIsNotNone(
                failure,
                msg=f"expected ledger_sync_failed audit entry, got {audit['items']}",
            )
            self.assertEqual(failure["payload"]["conversation_id"], "private-1")

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
            reply = _reply("message-1", "hello bridge")
            ConversationLedgerStore(data_dir).append_reply(reply)
            queue_id = queue.enqueue(reply)
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
            ConversationLedgerStore(data_dir).append_reply(reply)
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
            bridge_store = BridgeOutboxStore(data_dir)
            bridge_store.append_ack(
                old_bridge_id,
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
            )
            sync_bridge_ack_to_send_state(
                data_dir,
                old_bridge_id,
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
            )
            self.assertEqual(queue.get(queue_id)["status"], "failed")
            projection_dir = ledger.conversation_markdown_path("private-1").parent
            (projection_dir / "messages.jsonl").unlink()
            (projection_dir / "conversation.md").unlink()

            retried = retry_bridge_item(data_dir, old_bridge_id, reviewer="tester", note="test_retry")
            new_bridge_id = retried["new_bridge_id"]
            queue_item = queue.get(queue_id)
            entry = ledger.read_entries("private-1")[0]
            task = next(item for item in TaskStatusStore(data_dir).state()["tasks"] if item["task_id"] == "send-message-retry")

            self.assertNotEqual(new_bridge_id, old_bridge_id)
            self.assertTrue(retried["created"])
            self.assertFalse(retried["reused_existing"])
            self.assertEqual(queue_item["status"], "queued_to_bridge")
            self.assertIn(new_bridge_id, queue_item["note"])
            self.assertEqual(entry.send["status"], "queued_to_bridge")
            self.assertEqual(entry.send["message_id"], new_bridge_id)
            self.assertEqual(entry.send["retry_of"], old_bridge_id)
            self.assertEqual(task["status"], "queued")
            self.assertEqual(task["external_id"], new_bridge_id)
            self.assertEqual(task["metadata"]["bridge_id"], new_bridge_id)
            self.assertEqual(bridge_state(data_dir, limit=20)["pending_count"], 1)

            bridge_store.append_ack(
                new_bridge_id,
                status=BridgeAckStatus.SENT,
                reason="wechat_native_http_send_text_verified",
                external_message_id="ext-retry",
            )
            sync_bridge_ack_to_send_state(
                data_dir,
                new_bridge_id,
                status=BridgeAckStatus.SENT,
                reason="wechat_native_http_send_text_verified",
                external_message_id="ext-retry",
            )
            final_queue = queue.get(queue_id)
            final_entry = ledger.read_entries("private-1")[0]
            final_task = next(
                item
                for item in TaskStatusStore(data_dir).state()["tasks"]
                if item["task_id"] == "send-message-retry"
            )

            self.assertEqual(final_queue["status"], "sent")
            self.assertEqual(final_queue["send_result"]["status"], "sent")
            self.assertEqual(final_entry.send["status"], "sent")
            self.assertEqual(final_task["status"], "completed")

    def test_retry_bridge_item_replay_repairs_projections_after_durable_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            _register_authorized_private_channel(data_dir)
            reply = _reply("message-retry-replay", "hello bridge")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_reply(reply, chat_title="Alice")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")
            sent = send_approved_confirm_item(data_dir, queue_id)
            old_bridge_id = sent["send_result"]["message_id"]
            bridge_store = BridgeOutboxStore(data_dir)
            bridge_store.append_ack(
                old_bridge_id,
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
            )
            sync_bridge_ack_to_send_state(
                data_dir,
                old_bridge_id,
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
            )
            self.assertEqual(queue.get(queue_id)["status"], "failed")

            durable = BridgeOutboxStore(data_dir).requeue_resolved(
                old_bridge_id,
                reason="interrupted_retry",
                staged=True,
            )
            self.assertFalse(durable["ready_for_delivery"])
            replay = retry_bridge_item(data_dir, old_bridge_id, reviewer="tester", note="resume_retry")
            queue_item = queue.get(queue_id)
            entry = ledger.read_entries("private-1")[0]
            task = next(
                item
                for item in TaskStatusStore(data_dir).state()["tasks"]
                if item["task_id"] == "send-message-retry-replay"
            )
            state = bridge_state(data_dir, limit=20)

            successor_id = str(durable["bridge_id"])
            self.assertFalse(durable["_reused_existing"])
            self.assertFalse(replay["created"])
            self.assertTrue(replay["reused_existing"])
            self.assertEqual(replay["new_bridge_id"], successor_id)
            self.assertEqual(queue_item["status"], "queued_to_bridge")
            self.assertIn(successor_id, queue_item["note"])
            self.assertEqual(entry.send["message_id"], successor_id)
            self.assertEqual(task["external_id"], successor_id)
            self.assertEqual(state["count"], 2)
            self.assertEqual(state["pending_count"], 1)
            successor = next(item for item in state["items"] if item["bridge_id"] == successor_id)
            self.assertTrue(successor["delivery_ready"])

    def test_retry_multipart_file_replaces_all_structured_projections_and_reaggregates_sent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            _register_authorized_private_channel(data_dir)
            file_path = Path(tmp) / "retry-file.txt"
            file_path.write_text("payload", encoding="utf-8")
            reply = ReplyCandidate(
                message_id="message-retry-multipart",
                conversation_id="private-1",
                text="text part",
                send_mode="confirm",
                model="fake",
                attachments=[{"path": str(file_path), "name": file_path.name}],
            )
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_reply(reply, chat_title="Alice")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")
            sent = send_approved_confirm_item(data_dir, queue_id)
            text_bridge_id = sent["send_result"]["details"]["text"]["message_id"]
            old_file_bridge_id = sent["send_result"]["details"]["files"][0]["message_id"]
            store = BridgeOutboxStore(data_dir)
            store.append_ack(text_bridge_id, status=BridgeAckStatus.SENT, reason="text_verified")
            sync_bridge_ack_to_send_state(
                data_dir,
                text_bridge_id,
                status=BridgeAckStatus.SENT,
                reason="text_verified",
                external_message_id="ext-text",
            )
            safe_failure = "wechat_native_http_send_file_error:ConnectionRefusedError:refused"
            store.append_ack(old_file_bridge_id, status=BridgeAckStatus.FAILED, reason=safe_failure)
            sync_bridge_ack_to_send_state(
                data_dir,
                old_file_bridge_id,
                status=BridgeAckStatus.FAILED,
                reason=safe_failure,
            )

            retried = retry_bridge_item(data_dir, old_file_bridge_id, reviewer="tester")
            new_file_bridge_id = retried["new_bridge_id"]
            queued = queue.get(queue_id)
            entry = ledger.read_entries("private-1")[0]
            task = next(
                item
                for item in TaskStatusStore(data_dir).state()["tasks"]
                if item["task_id"] == "send-message-retry-multipart"
            )

            self.assertEqual(queued["bridge_ids"], [text_bridge_id, new_file_bridge_id])
            self.assertEqual(set(queued["bridge_acks"]), {text_bridge_id})
            self.assertEqual(
                queued["send_result"]["details"]["bridge_ids"],
                [text_bridge_id, new_file_bridge_id],
            )
            self.assertEqual(
                queued["send_result"]["details"]["files"][0]["message_id"],
                new_file_bridge_id,
            )
            self.assertEqual(entry.send["details"]["files"][0]["message_id"], new_file_bridge_id)
            self.assertEqual(entry.attachments[0]["send"]["message_id"], new_file_bridge_id)
            self.assertEqual(task["metadata"]["bridge_ids"], [text_bridge_id, new_file_bridge_id])
            self.assertEqual(set(task["metadata"]["bridge_acks"]), {text_bridge_id})

            store.append_ack(new_file_bridge_id, status=BridgeAckStatus.SENT, reason="file_verified")
            sync_bridge_ack_to_send_state(
                data_dir,
                new_file_bridge_id,
                status=BridgeAckStatus.SENT,
                reason="file_verified",
                external_message_id="ext-file-retry",
            )
            completed_queue = queue.get(queue_id)
            completed_entry = ledger.read_entries("private-1")[0]
            completed_task = next(
                item
                for item in TaskStatusStore(data_dir).state()["tasks"]
                if item["task_id"] == "send-message-retry-multipart"
            )

            self.assertEqual(completed_queue["status"], "sent")
            self.assertEqual(completed_queue["send_result"]["status"], "sent")
            self.assertEqual(completed_entry.send["status"], "sent")
            self.assertEqual(completed_entry.attachments[0]["send"]["status"], "sent")
            self.assertEqual(completed_task["status"], "completed")

    def test_retry_from_stale_ancestor_repairs_projection_to_direct_parent_successor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            _register_authorized_private_channel(data_dir)
            reply = _reply("message-retry-lineage", "lineage")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_reply(reply, chat_title="Alice")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")
            sent = send_approved_confirm_item(data_dir, queue_id)
            original_id = sent["send_result"]["message_id"]
            store = BridgeOutboxStore(data_dir)
            safe_failure = "wechat_native_http_send_text_error:ConnectionRefusedError:refused"
            store.append_ack(original_id, status=BridgeAckStatus.FAILED, reason=safe_failure)
            sync_bridge_ack_to_send_state(data_dir, original_id, status=BridgeAckStatus.FAILED, reason=safe_failure)
            first = retry_bridge_item(data_dir, original_id, reviewer="tester")
            first_id = first["new_bridge_id"]
            store.append_ack(first_id, status=BridgeAckStatus.FAILED, reason=safe_failure)
            sync_bridge_ack_to_send_state(data_dir, first_id, status=BridgeAckStatus.FAILED, reason=safe_failure)
            staged = store.requeue_resolved(original_id, staged=True)

            replay = retry_bridge_item(data_dir, original_id, reviewer="tester")
            second_id = staged["bridge_id"]
            queue_item = queue.get(queue_id)
            entry = ledger.read_entries("private-1")[0]
            task = next(
                item
                for item in TaskStatusStore(data_dir).state()["tasks"]
                if item["task_id"] == "send-message-retry-lineage"
            )
            successor = next(
                item for item in bridge_state(data_dir, limit=20)["items"] if item["bridge_id"] == second_id
            )

            self.assertEqual(staged["retry_of"], first_id)
            self.assertTrue(replay["reused_existing"])
            self.assertEqual(replay["retry_parent_id"], first_id)
            self.assertEqual(queue_item["bridge_ids"], [second_id])
            self.assertEqual(entry.send["message_id"], second_id)
            self.assertEqual(task["external_id"], second_id)
            self.assertTrue(successor["delivery_ready"])

    def test_retry_projection_error_leaves_successor_staged_until_replay_repairs_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            _register_authorized_private_channel(data_dir)
            reply = _reply("message-retry-barrier", "barrier")
            ConversationLedgerStore(data_dir).append_reply(reply, chat_title="Alice")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")
            sent = send_approved_confirm_item(data_dir, queue_id)
            original_id = sent["send_result"]["message_id"]
            store = BridgeOutboxStore(data_dir)
            store.append_ack(
                original_id,
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
            )

            with mock.patch.object(
                ConversationLedgerStore,
                "requeue_bridge_send_result",
                side_effect=OSError("projection unavailable"),
            ):
                with self.assertRaisesRegex(RuntimeError, "ledger projection failed"):
                    retry_bridge_item(data_dir, original_id, reviewer="tester")

            staged_state = bridge_state(data_dir, limit=20)
            staged_successor = next(item for item in staged_state["items"] if item["bridge_id"] != original_id)
            self.assertFalse(staged_successor["delivery_ready"])

            replay = retry_bridge_item(data_dir, original_id, reviewer="tester")
            repaired = next(
                item
                for item in bridge_state(data_dir, limit=20)["items"]
                if item["bridge_id"] == replay["new_bridge_id"]
            )
            self.assertTrue(replay["reused_existing"])
            self.assertTrue(repaired["delivery_ready"])

    def test_retry_inherits_nested_projection_contract_and_fails_closed_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            original = store.enqueue("private-1", "missing projection", staged=True)
            store.activate_staged_record(
                original["bridge_id"],
                expected_projections=["queue"],
            )
            store.append_ack(
                original["bridge_id"],
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
            )

            with self.assertRaisesRegex(RuntimeError, "missing_queue"):
                retry_bridge_item(data_dir, original["bridge_id"], reviewer="tester")

            outbox = store._read_all(store.outbox_path)
            successor = next(item for item in outbox if item["bridge_id"] != original["bridge_id"])
            successor_ack = next(
                item for item in store._read_all(store.ack_path) if item["bridge_id"] == successor["bridge_id"]
            )
            self.assertFalse(successor["ready_for_delivery"])
            self.assertEqual(successor["expected_projections"], ["queue"])
            self.assertEqual(successor_ack["status"], BridgeAckStatus.FAILED)
            self.assertIn("missing_queue", successor_ack["reason"])

    def test_retry_replay_recognizes_task_already_projected_to_staged_successor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            original = store.enqueue("private-1", "projection committed", staged=True)
            store.activate_staged_record(
                original["bridge_id"],
                expected_projections=["task"],
            )
            store.append_ack(
                original["bridge_id"],
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
            )
            task_store = TaskStatusStore(data_dir)
            task_store.create(
                {
                    "task_id": "send-retry-projection-committed",
                    "title": "bridge send",
                    "kind": "send",
                    "status": "failed",
                    "external_id": original["bridge_id"],
                    "metadata": {
                        "bridge_id": original["bridge_id"],
                        "bridge_ids": [original["bridge_id"]],
                    },
                }
            )

            with mock.patch.object(
                TaskStatusStore,
                "_write_projection_from_sqlite",
                side_effect=OSError("projection file unavailable"),
            ):
                with self.assertRaisesRegex(RuntimeError, "projection file unavailable"):
                    retry_bridge_item(data_dir, original["bridge_id"], reviewer="tester")

            staged = next(
                item for item in store._read_all(store.outbox_path) if item["bridge_id"] != original["bridge_id"]
            )
            already_projected = next(
                item
                for item in task_store.scheduler_store.list_tasks(limit=100)
                if item["task_id"] == "send-retry-projection-committed"
            )
            replay = retry_bridge_item(data_dir, original["bridge_id"], reviewer="tester")

            self.assertEqual(already_projected["external_id"], staged["bridge_id"])
            self.assertTrue(replay["reused_existing"])
            self.assertEqual(replay["new_bridge_id"], staged["bridge_id"])
            self.assertEqual(len(replay["task_updates"]), 1)
            self.assertTrue(
                next(
                    item
                    for item in bridge_state(data_dir, limit=10)["items"]
                    if item["bridge_id"] == staged["bridge_id"]
                )["delivery_ready"]
            )

    def test_retry_updates_task_beyond_legacy_500_item_projection_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            original = store.enqueue("private-1", "oldest task", staged=True)
            store.activate_staged_record(
                original["bridge_id"],
                expected_projections=["task"],
            )
            store.append_ack(
                original["bridge_id"],
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
            )
            task_store = TaskStatusStore(data_dir)
            target = {
                "task_id": "send-oldest-bridge-task",
                "title": "old bridge task",
                "kind": "send",
                "status": "failed",
                "progress": 100,
                "external_id": original["bridge_id"],
                "created_at": "2000-01-01T00:00:00Z",
                "updated_at": "2000-01-01T00:00:00Z",
                "metadata": {
                    "bridge_id": original["bridge_id"],
                    "bridge_ids": [original["bridge_id"]],
                },
            }
            noise = [
                {
                    "task_id": f"noise-{index:04d}",
                    "title": "newer noise",
                    "kind": "operation",
                    "status": "completed",
                    "created_at": "2099-01-01T00:00:00Z",
                    "updated_at": "2099-01-01T00:00:00Z",
                }
                for index in range(500)
            ]
            task_store.scheduler_store.update_tasks_atomically(
                lambda _tasks: ([target, *noise], None, [])
            )

            retried = retry_bridge_item(data_dir, original["bridge_id"], reviewer="tester")

            authority = task_store.scheduler_store.list_tasks(limit=1000)
            updated_target = next(item for item in authority if item["task_id"] == target["task_id"])
            self.assertEqual(len(authority), 501)
            self.assertEqual(len(retried["task_updates"]), 1)
            self.assertEqual(updated_target["external_id"], retried["new_bridge_id"])
            self.assertEqual(updated_target["metadata"]["bridge_ids"], [retried["new_bridge_id"]])

    def test_partial_task_projection_update_keeps_ack_unsynced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            record = store.enqueue("private-1", "partial task sync", staged=True)
            store.activate_staged_record(
                record["bridge_id"],
                expected_projections=["task"],
            )
            task_store = TaskStatusStore(data_dir)
            for suffix in ("a", "b"):
                task_store.create(
                    {
                        "task_id": f"send-partial-{suffix}",
                        "title": "bridge send",
                        "kind": "send",
                        "status": "queued",
                        "external_id": record["bridge_id"],
                        "metadata": {
                            "bridge_id": record["bridge_id"],
                            "bridge_ids": [record["bridge_id"]],
                        },
                    }
                )
            scheduler_type = type(task_store.scheduler_store)
            original_upsert = scheduler_type.upsert_task

            def flaky_upsert(instance, task):
                if task.get("task_id") == "send-partial-b":
                    raise OSError("task projection unavailable")
                return original_upsert(instance, task)

            with mock.patch(
                "app.personal_wechat_bot.control.send_commands._sync_send_tasks_for_bridge_ack_atomically",
                return_value=None,
            ), mock.patch.object(scheduler_type, "upsert_task", new=flaky_upsert):
                result = sync_bridge_ack_to_send_state(
                    data_dir,
                    record["bridge_id"],
                    status=BridgeAckStatus.SENT,
                    reason="native_sent",
                )

            self.assertEqual(len(result["task_updates"]), 1)
            self.assertTrue(any("send-partial-b:OSError" in item for item in result["task_errors"]))
            self.assertIn("task_projection_coverage:1/2", result["task_errors"])
            self.assertFalse(result["sync_complete"])

    def test_ack_fallback_updates_old_task_without_truncating_sqlite_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            record = store.enqueue("private-1", "old fallback task", staged=True)
            store.activate_staged_record(
                record["bridge_id"],
                expected_projections=["task"],
            )
            task_store = TaskStatusStore(data_dir)
            target = {
                "task_id": "send-oldest-fallback-task",
                "title": "old bridge task",
                "kind": "send",
                "status": "queued",
                "external_id": record["bridge_id"],
                "created_at": "2000-01-01T00:00:00Z",
                "updated_at": "2000-01-01T00:00:00Z",
                "metadata": {
                    "bridge_id": record["bridge_id"],
                    "bridge_ids": [record["bridge_id"]],
                },
            }
            noise = [
                {
                    "task_id": f"fallback-noise-{index:04d}",
                    "title": "newer noise",
                    "kind": "operation",
                    "status": "completed",
                    "created_at": "2099-01-01T00:00:00Z",
                    "updated_at": "2099-01-01T00:00:00Z",
                }
                for index in range(500)
            ]
            task_store.scheduler_store.update_tasks_atomically(
                lambda _tasks: ([target, *noise], None, [])
            )

            with mock.patch(
                "app.personal_wechat_bot.control.send_commands._sync_send_tasks_for_bridge_ack_atomically",
                return_value=None,
            ):
                result = sync_bridge_ack_to_send_state(
                    data_dir,
                    record["bridge_id"],
                    status=BridgeAckStatus.SENT,
                    reason="native_sent",
                )

            authority = task_store.scheduler_store.list_tasks(limit=1000)
            updated_target = next(item for item in authority if item["task_id"] == target["task_id"])
            self.assertEqual(len(authority), 501)
            self.assertEqual(updated_target["status"], "completed")
            self.assertEqual(result["task_errors"], [])
            self.assertTrue(result["sync_complete"])

    def test_explicit_empty_staged_contract_is_complete_not_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            record = store.enqueue("private-1", "no projections", staged=True)
            store.set_staged_projection_contract(
                [record["bridge_id"]],
                expected_projections=[],
            )
            applied = store.append_terminal_ack_if_queued(
                record["bridge_id"],
                status=BridgeAckStatus.FAILED,
                reason="projection_publish_failed",
            )

            result = sync_bridge_ack_to_send_state(
                data_dir,
                record["bridge_id"],
                status=BridgeAckStatus.FAILED,
                reason="projection_publish_failed",
            )

            self.assertTrue(applied["applied"])
            self.assertTrue(result["projection_contract_present"])
            self.assertFalse(result["staged_without_contract"])
            self.assertTrue(result["projection_found"])
            self.assertTrue(result["sync_complete"])

    def test_send_approved_confirm_item_bridge_queues_by_channel_receiver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            _register_authorized_private_channel(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            reply = _reply("message-1", "hello bridge")
            ConversationLedgerStore(data_dir).append_reply(reply)
            queue_id = queue.enqueue(reply)
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


def _send_approved_process_worker(
    data_dir: str,
    queue_id: str,
    start,
    entered,
    release,
    calls,
    ready,
    results,
) -> None:
    class _ProcessBlockingDriver:
        def send_message(self, conversation_id: str, text: str) -> SendResult:
            with calls.get_lock():
                calls.value += 1
            entered.set()
            if not release.wait(20.0):
                raise TimeoutError("test did not release process driver")
            return SendResult("sent-process-id", conversation_id, "sent", "fake_sent")

    ready.put("ready")
    if not start.wait(20.0):
        results.put({"status": "error", "reason": "start timeout"})
        return
    result = send_approved_confirm_item(data_dir, queue_id, driver=_ProcessBlockingDriver())
    results.put(
        {
            "status": result.get("status"),
            "reason": result.get("reason", ""),
            "claim_conflict": bool(result.get("claim_conflict")),
        }
    )


class _SendingDriver:
    def __init__(self) -> None:
        self.sent_texts: list[str] = []

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        self.sent_texts.append(text)
        return SendResult(message_id="sent-id", conversation_id=conversation_id, status="sent", reason="fake_sent")


class _BlockingDelegatingDriver:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        self.calls += 1
        self.entered.set()
        if not self.release.wait(20.0):
            raise TimeoutError("test did not release driver")
        return self.delegate.send_message(conversation_id, text)

    def activate_send_result(self, result: SendResult, *, expected_projections=None) -> dict:
        return self.delegate.activate_send_result(result, expected_projections=expected_projections)

    def fail_staged_send_result(self, result: SendResult, *, reason: str, expected_projections=None) -> dict:
        return self.delegate.fail_staged_send_result(
            result,
            reason=reason,
            expected_projections=expected_projections,
        )


class _TerminalBeforeActivationDriver:
    def __init__(self, data_dir: Path, delegate) -> None:
        self.store = BridgeOutboxStore(data_dir)
        self.delegate = delegate

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        result = self.delegate.send_message(conversation_id, text)
        self.store.append_ack(
            result.message_id,
            status=BridgeAckStatus.FAILED,
            reason="staged_record_owner_exited_before_activation",
            payload={"delivery_attempted": False},
        )
        return result

    def activate_send_result(self, result: SendResult, *, expected_projections=None) -> dict:
        return self.delegate.activate_send_result(result, expected_projections=expected_projections)

    def fail_staged_send_result(self, result: SendResult, *, reason: str, expected_projections=None) -> dict:
        return self.delegate.fail_staged_send_result(
            result,
            reason=reason,
            expected_projections=expected_projections,
        )


class _RaisingDriver:
    def __init__(self) -> None:
        self.calls = 0

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        self.calls += 1
        raise OSError("driver response lost")


if __name__ == "__main__":
    unittest.main()

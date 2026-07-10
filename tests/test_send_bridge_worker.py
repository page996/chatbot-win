from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.config.loader import create_default_config, load_config
from app.personal_wechat_bot.control.send_commands import send_approved_confirm_item, set_send_controls
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.conversation.segment import conversation_segment
from app.personal_wechat_bot.domain.models import ReplyCandidate
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue
from app.personal_wechat_bot.reply_gate.send_executor import GuardedSendExecutor
from app.personal_wechat_bot.runtime.send_bridge_worker import BridgeWorker, bridge_worker_lock_alive, run_bridge_worker
from app.personal_wechat_bot.wechat_driver.bridge_send import BridgeAckStatus, BridgeOutboxSendDriver, BridgeOutboxStore
from app.personal_wechat_bot.wechat_driver.send_backends import DryRunSendBackend, SendOutcome


class _FlakyBackend:
    """Fails the first N attempts for a receiver, then succeeds."""

    name = "flaky"

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.attempts = 0

    def health_check(self) -> bool:
        return True

    def send_text(self, receiver: str, text: str) -> SendOutcome:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            return SendOutcome.failure("transient")
        return SendOutcome.success("ok")

    def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
        return SendOutcome.success("ok")

    def close(self) -> None:
        return None


def _write_channel(data_dir: Path, conversation_id: str, payload: dict) -> None:
    chat_title = str(payload.get("chat_title", "") or "")
    segment = conversation_segment(conversation_id, chat_title)
    channel_dir = data_dir / "conversation_channels" / segment
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / "channel.json").write_text(json.dumps(payload), encoding="utf-8")
    (data_dir / "conversation_channels" / "index.json").write_text(
        json.dumps({"channels": [{"conversation_id": conversation_id, "chat_title": chat_title}]}),
        encoding="utf-8",
    )


class SendBridgeWorkerTest(unittest.TestCase):
    def test_bridge_worker_lock_with_dead_pid_is_not_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            lock_path = data_dir / "send_bridge" / ".bridge_worker.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps({"pid": 999999, "label": "send_bridge_worker", "heartbeat_at": time.time()}),
                encoding="utf-8",
            )

            self.assertFalse(bridge_worker_lock_alive(data_dir))

    def test_worker_delivers_pending_text_and_acks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            record = store.enqueue("wxid_a", "hello there")

            backend = DryRunSendBackend()
            worker = BridgeWorker(data_dir, backend)
            processed = worker.run_once()

            self.assertEqual(processed, 1)
            self.assertEqual(backend.sent_texts, [("wxid_a", "hello there")])
            state = store.state(limit=10)
            self.assertEqual(state["pending_count"], 0)
            self.assertEqual(state["items"][0]["status"], "sent")
            self.assertEqual(state["items"][0]["bridge_id"], record["bridge_id"])
            self.assertEqual(state["items"][0]["ack"]["payload"]["backend"], "dry_run")
            self.assertEqual(state["items"][0]["ack"]["payload"]["operation"], "send_text")

    def test_worker_persists_backend_evidence_payload_on_terminal_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "hello there")

            class _EvidenceBackend:
                name = "evidence"

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    return SendOutcome.success(
                        "ok",
                        payload={"backend": self.name, "response": {"ret": 0, "retmsg": "success"}},
                    )

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    return SendOutcome.success("ok")

                def close(self) -> None:
                    return None

            BridgeWorker(data_dir, _EvidenceBackend()).run_once()

            item = store.state(limit=10)["items"][0]
            self.assertEqual(item["status"], "sent")
            self.assertEqual(item["ack"]["payload"]["backend"], "evidence")
            self.assertEqual(item["ack"]["payload"]["response"]["ret"], 0)

    def test_worker_marks_unverified_success_as_accepted_not_sent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "hello there")

            class _AcceptedBackend:
                name = "accepted"

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    return SendOutcome.accepted_unverified(
                        "wechat_native_http_send_text_accepted_unverified",
                        payload={"backend": "wechat_native_http", "delivery_verified": False},
                    )

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    return SendOutcome.accepted_unverified("wechat_native_http_send_file_accepted_unverified")

                def close(self) -> None:
                    return None

            BridgeWorker(data_dir, _AcceptedBackend()).run_once()

            state = store.state(limit=10)
            item = state["items"][0]
            self.assertEqual(item["status"], BridgeAckStatus.ACCEPTED)
            self.assertEqual(state["sent_count"], 0)
            self.assertEqual(state["accepted_count"], 1)
            self.assertEqual(state["pending_count"], 0)
            self.assertTrue(item["retryable"])

    def test_worker_late_reverifies_accepted_file_without_redelivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, backend="wechat_native_http")
            target = data_dir / "late.csv"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("late,file", encoding="utf-8")
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue_file("wxid_a", str(target), receiver="wxid_a")
            store.append_ack(
                rec["bridge_id"],
                status=BridgeAckStatus.ACCEPTED,
                reason="wechat_native_http_send_file_accepted_unverified",
                payload={
                    "backend": "wechat_native_http",
                    "delivery_verified": False,
                    "accepted_unverified": True,
                },
            )

            class _LateVerifiedBackend:
                name = "wechat_native_http"

                def __init__(self) -> None:
                    self.sent_files: list[tuple[str, str, str]] = []

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    return SendOutcome.failure("should_not_send_text")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    self.sent_files.append((receiver, path, caption))
                    return SendOutcome.failure("should_not_resend_file")

                def verify_accepted_bridge_record(self, record: dict, ack: dict) -> SendOutcome:
                    return SendOutcome.success(
                        "wechat_native_http_send_file_verified_late",
                        external_message_id="ext-late-file",
                        payload={"backend": "wechat_native_http", "delivery_verified": True, "late_delivery_verification": True},
                    )

                def close(self) -> None:
                    return None

            backend = _LateVerifiedBackend()
            BridgeWorker(data_dir, backend).run_once()

            state = store.state(limit=10)
            item = state["items"][0]
            self.assertEqual(backend.sent_files, [])
            self.assertEqual(item["status"], BridgeAckStatus.SENT)
            self.assertEqual(item["ack"]["reason"], "wechat_native_http_send_file_verified_late")
            self.assertEqual(item["ack"]["external_message_id"], "ext-late-file")
            self.assertTrue(item["ack"]["payload"]["late_delivery_verification"])

    def test_worker_prefers_record_receiver_over_conversation_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("hashed-conversation-id", "hello there", receiver="wxid_real_alice")

            backend = DryRunSendBackend()
            BridgeWorker(data_dir, backend).run_once()

            self.assertEqual(backend.sent_texts, [("wxid_real_alice", "hello there")])

    def test_worker_does_not_send_to_hashed_conversation_id_fallback(self) -> None:
        # No explicit receiver and no channel registered: the hashed
        # conversation_id is NOT a valid WeChat receiver, so the worker must treat
        # it as missing_receiver (retryable) rather than deliver to a bogus id.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("hashed-conversation-id", "should not send")

            backend = DryRunSendBackend()
            BridgeWorker(data_dir, backend).run_once()

            # Nothing delivered to a bogus receiver.
            self.assertEqual(backend.sent_texts, [])
            item = store.state(limit=10)["items"][0]
            # Still pending (retry ack), waiting for a real receiver.
            self.assertNotIn(item["status"], {"sent", "failed"})
            self.assertIn("missing_receiver", item["ack"]["reason"])

    def test_real_worker_blocks_raw_private_receiver_without_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, backend="wechat_native_http")
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_unidentified", "should not touch real backend")

            class _RealBackend:
                name = "wechat_native_http"

                def __init__(self) -> None:
                    self.calls = 0

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    self.calls += 1
                    return SendOutcome.success("should_not_send")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    self.calls += 1
                    return SendOutcome.success("should_not_send")

                def close(self) -> None:
                    return None

            backend = _RealBackend()
            BridgeWorker(data_dir, backend).run_once()

            self.assertEqual(backend.calls, 0)
            item = store.state(limit=10)["items"][0]
            self.assertEqual(item["status"], BridgeAckStatus.BLOCKED)
            self.assertEqual(item["ack"]["reason"], "receiver_not_authorized:missing_channel")

    def test_real_worker_respects_disabled_send_controls_before_wire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("probe", "should remain queued", receiver="filehelper")

            class _RealBackend:
                name = "wechat_native_http"

                def __init__(self) -> None:
                    self.calls = 0

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    self.calls += 1
                    return SendOutcome.success("should_not_send")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    self.calls += 1
                    return SendOutcome.success("should_not_send")

                def close(self) -> None:
                    return None

            backend = _RealBackend()
            BridgeWorker(data_dir, backend).run_once()

            state = store.state(limit=10)
            self.assertEqual(backend.calls, 0)
            self.assertEqual(state["ack_count"], 0)
            self.assertEqual(state["pending_count"], 1)
            self.assertEqual(state["items"][0]["status"], "queued")

    def test_real_worker_blocks_unidentified_legacy_private_channel_receiver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, backend="wechat_native_http")
            conversation_id = "legacy-unidentified-private"
            _write_channel(
                data_dir,
                conversation_id,
                {
                    "conversation_id": conversation_id,
                    "conversation_type": "private",
                    "chat_title": "wxid_unidentified",
                    "conversation_key": "wxid_unidentified",
                    "sender_wechat_ids": ["wxid_unidentified"],
                    "source_names": ["weflow_discovery"],
                    "trusted_channel_source": True,
                },
            )
            store = BridgeOutboxStore(data_dir)
            store.enqueue(conversation_id, "should not touch real backend", receiver="wxid_unidentified")

            class _RealBackend:
                name = "wechat_native_http"

                def __init__(self) -> None:
                    self.calls = 0

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    self.calls += 1
                    return SendOutcome.success("should_not_send")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    self.calls += 1
                    return SendOutcome.success("should_not_send")

                def close(self) -> None:
                    return None

            backend = _RealBackend()
            BridgeWorker(data_dir, backend).run_once()

            self.assertEqual(backend.calls, 0)
            item = store.state(limit=10)["items"][0]
            self.assertEqual(item["status"], BridgeAckStatus.BLOCKED)
            self.assertEqual(item["ack"]["reason"], "receiver_not_authorized:private_contact_unknown_or_unidentified")

    def test_worker_is_restart_safe_and_does_not_resend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "one")

            backend1 = DryRunSendBackend()
            BridgeWorker(data_dir, backend1).run_once()
            self.assertEqual(len(backend1.sent_texts), 1)

            # A fresh worker (simulating a restart) must not re-send the acked record.
            backend2 = DryRunSendBackend()
            processed = BridgeWorker(data_dir, backend2).run_once()
            self.assertEqual(processed, 0)
            self.assertEqual(backend2.sent_texts, [])

    def test_blocked_ack_is_terminal_and_not_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "should stay blocked")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.BLOCKED, reason="manual_block")

            backend = DryRunSendBackend()
            processed = BridgeWorker(data_dir, backend).run_once()

            self.assertEqual(processed, 0)
            self.assertEqual(backend.sent_texts, [])
            item = store.state(limit=10)["items"][0]
            self.assertEqual(item["status"], BridgeAckStatus.BLOCKED)
            self.assertEqual(item["ack"]["reason"], "manual_block")

    def test_stale_inflight_after_terminal_ack_is_not_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "already stopped")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.BLOCKED, reason="manual_block")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.INFLIGHT, reason="stale_inflight")

            backend = DryRunSendBackend()
            processed = BridgeWorker(data_dir, backend).run_once()

            self.assertEqual(processed, 0)
            self.assertEqual(backend.sent_texts, [])
            acks = [ack for ack in store._read_all(store.ack_path) if ack["bridge_id"] == rec["bridge_id"]]
            self.assertEqual(
                [ack["status"] for ack in acks],
                [BridgeAckStatus.BLOCKED, BridgeAckStatus.INFLIGHT],
            )

    def test_worker_does_not_send_when_inflight_ack_cannot_be_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "ack append fails")

            backend = DryRunSendBackend()
            worker = BridgeWorker(data_dir, backend)
            with mock.patch.object(worker.store, "append_ack", side_effect=OSError("locked")):
                processed = worker.run_once()

            self.assertEqual(processed, 1)
            self.assertEqual(backend.sent_texts, [])
            self.assertEqual(store._read_all(store.ack_path), [])
            self.assertIn("append_ack_failed", worker.stats.last_error)

    def test_worker_rechecks_effective_ack_after_inflight_before_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "race window")

            backend = DryRunSendBackend()
            worker = BridgeWorker(data_dir, backend)
            original_append = worker.store.append_ack

            def _racing_append(bridge_id: str, **kwargs):
                if kwargs.get("status") == BridgeAckStatus.INFLIGHT:
                    original_append(bridge_id, status=BridgeAckStatus.SENT, reason="racing_sent")
                return original_append(bridge_id, **kwargs)

            with mock.patch.object(worker.store, "append_ack", side_effect=_racing_append):
                processed = worker.run_once()

            self.assertEqual(processed, 1)
            self.assertEqual(backend.sent_texts, [])
            item = store.state(limit=10)["items"][0]
            self.assertEqual(item["status"], BridgeAckStatus.SENT)
            self.assertEqual(item["ack"]["reason"], "racing_sent")
            acks = [ack for ack in store._read_all(store.ack_path) if ack["bridge_id"] == rec["bridge_id"]]
            self.assertEqual(
                [ack["status"] for ack in acks],
                [BridgeAckStatus.SENT, BridgeAckStatus.INFLIGHT],
            )

    def test_worker_retries_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "retry me")

            backend = _FlakyBackend(fail_times=2)
            worker = BridgeWorker(data_dir, backend, max_send_attempts=3)
            worker.run_once()

            self.assertEqual(backend.attempts, 3)
            state = store.state(limit=10)
            self.assertEqual(state["items"][0]["status"], "sent")

    def test_worker_marks_failed_when_all_attempts_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "doomed")

            backend = _FlakyBackend(fail_times=99)
            worker = BridgeWorker(data_dir, backend, max_send_attempts=2)
            worker.run_once()

            state = store.state(limit=10)
            self.assertEqual(state["items"][0]["status"], "failed")
            self.assertEqual(worker.stats.failed, 1)

    def test_permanent_failure_is_not_retried_within_same_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "will hit missing endpoint")

            class _PermanentFailureBackend:
                name = "permanent"

                def __init__(self) -> None:
                    self.text_calls = 0

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    self.text_calls += 1
                    return SendOutcome.failure("wechat_native_http_send_file_error:ValueError:http_404:")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    return SendOutcome.failure("wechat_native_http_send_file_error:ValueError:http_404:")

                def close(self) -> None:
                    return None

            backend = _PermanentFailureBackend()
            BridgeWorker(data_dir, backend, max_send_attempts=3).run_once()

            self.assertEqual(backend.text_calls, 1)
            item = store.state(limit=10)["items"][0]
            self.assertEqual(item["status"], "failed")
            self.assertIn("http_404", item["ack"]["reason"])

    def test_worker_fails_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue_file("wxid_a", str(data_dir / "does_not_exist.pdf"))

            worker = BridgeWorker(data_dir, DryRunSendBackend())
            worker.run_once()

            state = store.state(limit=10)
            self.assertEqual(state["items"][0]["status"], "failed")
            self.assertIn("file_not_found", state["items"][0]["ack"]["reason"])

    def test_worker_delivers_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            target = data_dir / "tool_outputs" / "out.docx"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"docx bytes")
            store = BridgeOutboxStore(data_dir)
            store.enqueue_file("wxid_a", str(target), caption="here")

            backend = DryRunSendBackend()
            BridgeWorker(data_dir, backend).run_once()

            self.assertEqual(backend.sent_files, [("wxid_a", str(target), "here")])
            self.assertEqual(store.state(limit=10)["items"][0]["status"], "sent")

    def test_executor_file_send_queues_file_to_bridge_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            target = data_dir / "tool_outputs" / "reply.docx"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"docx")
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)
            config = load_config(data_dir)
            reply = ReplyCandidate(
                message_id="m-1",
                conversation_id="wxid_a",
                text="here is the doc",
                send_mode="confirm",
                model="fake",
                attachments=[{"path": str(target), "name": "reply.docx", "status": "indexed"}],
            )

            GuardedSendExecutor(config, driver).execute_confirmed(reply)

            backend = DryRunSendBackend()
            BridgeWorker(data_dir, backend).run_once()

            self.assertEqual(backend.sent_texts, [("wxid_a", "here is the doc")])
            self.assertEqual(backend.sent_files, [("wxid_a", str(target), "")])

    def test_interrupted_send_is_quarantined_not_resent(self) -> None:
        # A crash between the wire send and the terminal ack leaves an 'inflight'
        # ack as the latest. On the next run the record must be quarantined
        # (failed, possible duplicate) and NOT re-delivered.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "crashed mid-send")
            store.append_ack(rec["bridge_id"], status="inflight", reason="delivering")

            backend = DryRunSendBackend()
            BridgeWorker(data_dir, backend).run_once()

            self.assertEqual(backend.sent_texts, [])  # never re-sent
            item = store.state(limit=10)["items"][0]
            self.assertEqual(item["status"], "failed")
            self.assertEqual(item["ack"]["reason"], "possible_duplicate_send_after_crash")

    def test_healthy_send_writes_inflight_then_sent(self) -> None:
        # A normal delivery records inflight before the send and sent after, so
        # the latest ack is terminal 'sent' and it is not quarantined.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "clean send")

            backend = DryRunSendBackend()
            BridgeWorker(data_dir, backend).run_once()

            self.assertEqual(backend.sent_texts, [("wxid_a", "clean send")])
            acks = [a for a in store._read_all(store.ack_path) if a["bridge_id"] == rec["bridge_id"]]
            statuses = [a["status"] for a in acks]
            self.assertEqual(statuses, ["inflight", "sent"])
            self.assertEqual(store.state(limit=10)["items"][0]["status"], "sent")

    def test_heartbeat_fires_per_send_during_drain(self) -> None:
        # The worker must beat the lock before each send so a slow multi-record
        # drain keeps the single-instance lock fresh (no takeover / double-send).
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            for i in range(3):
                store.enqueue("wxid_a", f"msg {i}")

            beats = {"n": 0}
            worker = BridgeWorker(data_dir, DryRunSendBackend(), heartbeat=lambda: beats.__setitem__("n", beats["n"] + 1))
            worker.run_once()

            # One beat per delivered record (3), at minimum.
            self.assertGreaterEqual(beats["n"], 3)

    def test_retryable_failure_stays_pending_across_ticks(self) -> None:
        # A transient backend failure must NOT write a terminal ack; the record
        # stays pending so a later tick retries it (no silent drop).
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "deliver me eventually")

            class _DownThenUp:
                name = "downthenup"

                def __init__(self) -> None:
                    self.calls = 0

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    self.calls += 1
                    if self.calls == 1:
                        return SendOutcome.failure("wechat_native_http_unavailable")
                    return SendOutcome.success("ok")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    return SendOutcome.success("ok")

                def close(self) -> None:
                    return None

            backend = _DownThenUp()
            worker = BridgeWorker(data_dir, backend, max_send_attempts=1)

            worker.run_once()  # tick 1: backend down -> retry, stays pending
            state_after_1 = store.state(limit=10)
            self.assertEqual(state_after_1["items"][0]["status"], "retry")
            self.assertEqual(state_after_1["pending_count"], 1)

            worker.run_once()  # tick 2: backend up -> sent
            state_after_2 = store.state(limit=10)
            self.assertEqual(state_after_2["items"][0]["status"], "sent")
            self.assertEqual(state_after_2["pending_count"], 0)

    def test_unknown_delivery_state_is_quarantined_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "maybe sent")

            class _TimeoutBackend:
                name = "timeout"

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    return SendOutcome.failure("unknown_delivery_state:native_send_timeout")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    return SendOutcome.failure("unknown_delivery_state:native_send_timeout")

                def close(self) -> None:
                    return None

            BridgeWorker(data_dir, _TimeoutBackend(), max_send_attempts=1).run_once()

            item = store.state(limit=10)["items"][0]
            self.assertEqual(item["status"], "failed")
            self.assertIn("unknown_delivery_state", item["ack"]["reason"])

    def test_unknown_delivery_state_is_not_resent_within_same_tick(self) -> None:
        # A timeout means the message may already be on the wire. Even with a
        # multi-attempt budget, the worker must call the backend exactly once and
        # not re-send it during the remaining attempts of the same tick.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "maybe sent")

            class _CountingTimeoutBackend:
                name = "timeout"

                def __init__(self) -> None:
                    self.text_calls = 0

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    self.text_calls += 1
                    return SendOutcome.failure("unknown_delivery_state:native_send_timeout")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    return SendOutcome.failure("unknown_delivery_state:native_send_timeout")

                def close(self) -> None:
                    return None

            backend = _CountingTimeoutBackend()
            BridgeWorker(data_dir, backend, max_send_attempts=3).run_once()

            # Exactly one wire attempt despite a budget of 3.
            self.assertEqual(backend.text_calls, 1)
            item = store.state(limit=10)["items"][0]
            self.assertEqual(item["status"], "failed")
            self.assertIn("unknown_delivery_state", item["ack"]["reason"])

    def test_weflow_http_timeout_is_not_resent_within_same_tick(self) -> None:
        # A local WeFlow send timeout can mean the UI helper is still completing
        # the send. Treat it as unknown delivery state, not an ordinary retry.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "maybe sent by ui")

            class _CountingWeFlowTimeoutBackend:
                name = "weflow_timeout"

                def __init__(self) -> None:
                    self.text_calls = 0

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    self.text_calls += 1
                    return SendOutcome.failure("weflow_http_send_text_error:TimeoutError: timed out")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    return SendOutcome.failure("weflow_http_send_file_error:TimeoutError: timed out")

                def close(self) -> None:
                    return None

            backend = _CountingWeFlowTimeoutBackend()
            BridgeWorker(data_dir, backend, max_send_attempts=3).run_once()

            self.assertEqual(backend.text_calls, 1)
            item = store.state(limit=10)["items"][0]
            self.assertEqual(item["status"], "failed")
            self.assertIn("weflow_http_send_text_error", item["ack"]["reason"])

    def test_wechat_native_http_timeout_is_not_resent_within_same_tick(self) -> None:
        # A local native send timeout can still mean the native send landed. Treat
        # it as unknown delivery state and never retry blindly.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "maybe sent by native bridge")

            class _CountingHookTimeoutBackend:
                name = "wechat_native_timeout"

                def __init__(self) -> None:
                    self.text_calls = 0

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    self.text_calls += 1
                    return SendOutcome.failure("wechat_native_http_send_text_error:TimeoutError: timed out")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    return SendOutcome.failure("wechat_native_http_send_file_error:TimeoutError: timed out")

                def close(self) -> None:
                    return None

            backend = _CountingHookTimeoutBackend()
            BridgeWorker(data_dir, backend, max_send_attempts=3).run_once()

            self.assertEqual(backend.text_calls, 1)
            item = store.state(limit=10)["items"][0]
            self.assertEqual(item["status"], "failed")
            self.assertIn("wechat_native_http_send_text_error", item["ack"]["reason"])

    def test_retryable_failure_becomes_terminal_after_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "never lands")

            class _AlwaysDown:
                name = "down"

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    return SendOutcome.failure("wechat_native_http_connect_failed")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    return SendOutcome.failure("wechat_native_http_connect_failed")

                def close(self) -> None:
                    return None

            worker = BridgeWorker(data_dir, _AlwaysDown(), max_send_attempts=1)
            # Run more ticks than the cross-tick retry cap; must end terminal.
            for _ in range(20):
                if store.state(limit=10)["items"][0]["status"] == "failed":
                    break
                worker.run_once()

            final = store.state(limit=10)["items"][0]
            self.assertEqual(final["status"], "failed")
            self.assertIn("retries_exhausted", final["ack"]["reason"])

    def test_poison_record_is_quarantined_not_crashing_worker(self) -> None:
        # A backend that raises must not crash the whole worker; the record is
        # quarantined with a terminal ack so the queue advances.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_poison", "boom")
            store.enqueue("wxid_ok", "fine")

            class _RaiseForPoison:
                name = "raiser"

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    if receiver == "wxid_poison":
                        raise ValueError("embedded null or similar")
                    return SendOutcome.success("ok")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    return SendOutcome.success("ok")

                def close(self) -> None:
                    return None

            processed = BridgeWorker(data_dir, _RaiseForPoison()).run_once()

            self.assertEqual(processed, 2)
            items = {i["conversation_id"]: i for i in store.state(limit=10)["items"]}
            self.assertEqual(items["wxid_poison"]["status"], "failed")
            self.assertIn("deliver_exception", items["wxid_poison"]["ack"]["reason"])
            self.assertEqual(items["wxid_ok"]["status"], "sent")

    def test_failed_ack_sync_is_reconciled_on_next_tick(self) -> None:
        # If the ledger/confirm sync fails at ack time, a later tick must re-sync
        # so the ledger eventually reflects delivery.
        with tempfile.TemporaryDirectory() as tmp:
            import app.personal_wechat_bot.runtime.send_bridge_worker as worker_mod

            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            reply = ReplyCandidate(
                message_id="m-1",
                conversation_id="wxid_a",
                text="reconcile me",
                send_mode="confirm",
                model="fake",
            )
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_reply(reply)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)
            send_approved_confirm_item(data_dir, queue_id, driver=driver)

            worker = BridgeWorker(data_dir, DryRunSendBackend())

            # Force the first sync to fail (simulating a Windows file lock).
            original = worker_mod.sync_bridge_ack_to_send_state
            calls = {"n": 0}

            def _flaky_sync(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("state file locked")
                return original(*args, **kwargs)

            worker_mod.sync_bridge_ack_to_send_state = _flaky_sync
            try:
                worker.run_once()  # delivers, ack written, sync fails
                # Ledger not yet flipped because sync failed.
                self.assertNotEqual(ledger.read_entries("wxid_a")[0].send.get("status"), "sent")
                worker.run_once()  # reconcile pass re-syncs the terminal ack
            finally:
                worker_mod.sync_bridge_ack_to_send_state = original

            self.assertEqual(ledger.read_entries("wxid_a")[0].send["status"], "sent")

    def test_full_chain_queue_to_bridge_to_ledger(self) -> None:
        # End-to-end: approve -> bridge_outbox queue -> worker delivers -> ledger sent.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox", backend="dry_run")
            reply = ReplyCandidate(
                message_id="m-1",
                conversation_id="wxid_a",
                text="chain hello",
                send_mode="confirm",
                model="fake",
            )
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_reply(reply)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")

            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)
            result = send_approved_confirm_item(data_dir, queue_id, driver=driver)
            self.assertEqual(result["status"], "queued_to_bridge")

            stats = run_bridge_worker(data_dir, once=True, lock_enabled=False)
            self.assertEqual(stats.delivered, 1)

            entry = ledger.read_entries("wxid_a")[0]
            self.assertEqual(entry.send["status"], "sent")
            self.assertEqual(queue.get(queue_id)["status"], "sent")


class SendBridgeWorkerScriptDataDirGuardTest(unittest.TestCase):
    _SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "send_bridge_worker.py"

    def test_strict_data_dir_refuses_without_config(self) -> None:
        # A data dir with no config.json is almost certainly the wrong one (the
        # app always writes config.json). Under --strict-data-dir the worker must
        # refuse rather than silently drain an empty/foreign outbox.
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "no_config_here"
            empty.mkdir()
            completed = subprocess.run(
                [sys.executable, str(self._SCRIPT), "--data-dir", str(empty), "--once", "--strict-data-dir"],
                cwd=self._SCRIPT.parent.parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(completed.returncode, 3)
            self.assertIn("no config.json", completed.stderr)

    def test_valid_data_dir_runs_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            completed = subprocess.run(
                [sys.executable, str(self._SCRIPT), "--data-dir", str(data_dir), "--once", "--strict-data-dir"],
                cwd=self._SCRIPT.parent.parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(completed.returncode, 0)
            self.assertIn("using data dir", completed.stderr)
            self.assertIn("delivered=", completed.stdout)


if __name__ == "__main__":
    unittest.main()

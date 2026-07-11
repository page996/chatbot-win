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
from app.personal_wechat_bot.conversation.channel_registry_store import ChannelRegistryStore
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.conversation.segment import conversation_segment
from app.personal_wechat_bot.domain.models import ReplyCandidate
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue
from app.personal_wechat_bot.reply_gate.send_executor import GuardedSendExecutor
from app.personal_wechat_bot.runtime.send_bridge_worker import (
    BridgeWorker,
    bridge_worker_config_signature,
    bridge_worker_lock_alive,
    run_bridge_worker,
)
from app.personal_wechat_bot.wechat_driver.bridge_send import (
    BridgeAckStatus,
    BridgeOutboxSendDriver,
    BridgeOutboxStore,
)
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
    payload = {**payload, "conversation_id": conversation_id, "segment": segment}
    ChannelRegistryStore(data_dir).upsert(payload)
    channel_dir = data_dir / "conversation_channels" / segment
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / "channel.json").write_text(json.dumps(payload), encoding="utf-8")
    (data_dir / "conversation_channels" / "index.json").write_text(
        json.dumps({"channels": [{"conversation_id": conversation_id, "chat_title": chat_title}]}),
        encoding="utf-8",
    )


class SendBridgeWorkerTest(unittest.TestCase):
    def test_worker_quarantines_dead_owner_staged_record_without_backend_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            record = store.enqueue("wxid_a", "projection crashed", staged=True)
            backend = mock.Mock(wraps=DryRunSendBackend())
            backend.name = "dry_run"

            with mock.patch(
                "app.personal_wechat_bot.wechat_driver.bridge_send.process_pid_alive",
                return_value=False,
            ):
                processed = BridgeWorker(data_dir, backend).run_once()

            item = next(value for value in store.state(limit=10)["items"] if value["bridge_id"] == record["bridge_id"])
            self.assertEqual(processed, 0)
            self.assertEqual(item["status"], BridgeAckStatus.FAILED)
            backend.send_text.assert_not_called()
            backend.send_file.assert_not_called()

    def test_worker_retries_staged_quarantine_failure_on_later_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            record = BridgeOutboxStore(data_dir).enqueue("wxid_a", "retry quarantine", staged=True)
            worker = BridgeWorker(data_dir, DryRunSendBackend())

            with mock.patch.object(
                worker.store,
                "quarantine_abandoned_staged_records",
                side_effect=OSError("ack file locked"),
            ):
                self.assertEqual(worker.run_once(), 0)

            self.assertIn("staged_quarantine_failed", worker.stats.last_error)
            with mock.patch(
                "app.personal_wechat_bot.wechat_driver.bridge_send.process_pid_alive",
                return_value=False,
            ):
                self.assertEqual(worker.run_once(), 0)
            item = next(
                value
                for value in worker.store.state(limit=10)["items"]
                if value["bridge_id"] == record["bridge_id"]
            )
            self.assertEqual(item["status"], BridgeAckStatus.FAILED)

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

    def test_bridge_worker_lock_rejects_reused_pid_with_different_start_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            lock_path = data_dir / "send_bridge" / ".bridge_worker.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 1234,
                        "label": "send_bridge_worker",
                        "heartbeat_at": time.time(),
                        "process_start": "old-process-instance",
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch(
                    "app.personal_wechat_bot.runtime.send_bridge_worker.process_pid_alive",
                    return_value=True,
                ),
                mock.patch(
                    "app.personal_wechat_bot.runtime.send_bridge_worker.process_start_marker",
                    return_value="new-process-instance",
                ),
            ):
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

    def test_worker_never_observes_initial_send_before_staged_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)
            result = driver.send_message("wxid_a", "publish projections first")
            backend = DryRunSendBackend()
            worker = BridgeWorker(data_dir, backend)

            staged_processed = worker.run_once()
            staged_record = driver.store._read_all(driver.store.outbox_path)[0]
            driver.activate_send_result(result, expected_projections=[])
            activated_processed = worker.run_once()

            self.assertEqual(staged_processed, 0)
            self.assertFalse(staged_record["ready_for_delivery"])
            self.assertEqual(backend.sent_texts, [("wxid_a", "publish projections first")])
            self.assertEqual(activated_processed, 1)

    def test_worker_skips_staged_retry_until_projections_activate_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            original = store.enqueue("wxid_a", "retry after projection")
            store.append_ack(
                original["bridge_id"],
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
            )
            successor = store.requeue_resolved(original["bridge_id"], staged=True)
            backend = DryRunSendBackend()
            worker = BridgeWorker(data_dir, backend)

            staged_processed = worker.run_once()
            staged_state = store.state(limit=10)
            store.activate_retry_successor(successor["bridge_id"])
            activated_processed = worker.run_once()
            activated_state = store.state(limit=10)

            self.assertEqual(staged_processed, 0)
            self.assertEqual(backend.sent_texts, [("wxid_a", "retry after projection")])
            self.assertEqual(staged_state["pending_count"], 1)
            self.assertEqual(staged_state["ack_count"], 1)
            staged_by_id = {item["bridge_id"]: item for item in staged_state["items"]}
            self.assertFalse(staged_by_id[original["bridge_id"]]["retryable"])
            self.assertIn("active retry already pending", staged_by_id[original["bridge_id"]]["retry_blocker"])
            self.assertEqual(
                staged_by_id[original["bridge_id"]]["active_retry_bridge_id"],
                successor["bridge_id"],
            )
            self.assertFalse(staged_by_id[successor["bridge_id"]]["delivery_ready"])
            self.assertEqual(activated_processed, 1)
            self.assertEqual(activated_state["pending_count"], 0)
            self.assertEqual(activated_state["sent_count"], 1)

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
            self.assertFalse(item["retryable"])
            self.assertEqual(
                item["retry_blocker"],
                "accepted item may already be delivered; wait for verification and do not re-send",
            )

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

    def test_real_worker_rechecks_send_enabled_before_each_wire_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(
                data_dir,
                enabled=True,
                driver="bridge_outbox",
                backend="wechat_native_http",
            )
            store = BridgeOutboxStore(data_dir)
            store.enqueue("filehelper", "only one wire attempt", receiver="filehelper")

            class _DisableAfterFirstFailureBackend:
                name = "wechat_native_http"

                def __init__(self) -> None:
                    self.calls = 0

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    self.calls += 1
                    set_send_controls(data_dir, enabled=False)
                    return SendOutcome.failure("wechat_native_http_unavailable")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    raise AssertionError("unexpected file send")

                def close(self) -> None:
                    return None

            backend = _DisableAfterFirstFailureBackend()
            with mock.patch("app.personal_wechat_bot.runtime.send_bridge_worker.time.sleep"):
                BridgeWorker(data_dir, backend, max_send_attempts=3).run_once()

            self.assertEqual(backend.calls, 1)
            item = store.state(limit=10)["items"][0]
            self.assertEqual(item["status"], BridgeAckStatus.RETRY)
            self.assertEqual(item["ack"]["reason"], "send_enabled_false")
            self.assertEqual(item["ack"]["payload"]["attempt"], 2)

    def test_worker_rechecks_full_startup_signature_after_inflight_before_wire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(
                data_dir,
                enabled=True,
                driver="bridge_outbox",
                backend="dry_run",
            )
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "must not use stale config")

            class _CountingDryRunBackend:
                name = "dry_run"

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

            changed = {"done": False}

            def mutate_config_after_inflight() -> None:
                if changed["done"]:
                    return
                changed["done"] = True
                set_send_controls(data_dir, weflow_send_timeout_seconds=36.0)

            backend = _CountingDryRunBackend()
            signature = bridge_worker_config_signature(load_config(data_dir))
            worker = BridgeWorker(
                data_dir,
                backend,
                heartbeat=mutate_config_after_inflight,
                config_signature=signature,
            )

            processed = worker.run_once()
            item = store.state(limit=10)["items"][0]

            self.assertEqual(processed, 1)
            self.assertEqual(backend.calls, 0)
            self.assertEqual(item["status"], BridgeAckStatus.RETRY)
            self.assertIn("bridge_worker_runtime_config_changed", item["ack"]["reason"])
            self.assertIn("weflow_send_timeout_seconds", item["ack"]["reason"])

    def test_real_worker_rechecks_channel_authorization_before_each_wire_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(
                data_dir,
                enabled=True,
                driver="bridge_outbox",
                backend="wechat_native_http",
            )
            conversation_id = "authorized-private-dynamic"
            channel = {
                "conversation_id": conversation_id,
                "conversation_type": "private",
                "chat_title": "Alice",
                "conversation_key": "wxid_real_alice",
                "sender_wechat_ids": ["wxid_real_alice"],
                "source_names": ["weflow_discovery"],
                "trusted_channel_source": True,
                "is_friend": True,
                "contact_authorization": "explicit_friend",
            }
            _write_channel(data_dir, conversation_id, channel)
            store = BridgeOutboxStore(data_dir)
            store.enqueue(conversation_id, "only one authorized attempt", receiver="wxid_real_alice")

            class _RevokeAfterFirstFailureBackend:
                name = "wechat_native_http"

                def __init__(self) -> None:
                    self.calls = 0

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    self.calls += 1
                    ChannelRegistryStore(data_dir).upsert(
                        {
                            **channel,
                            "is_friend": False,
                            "contact_authorization": "unknown_or_unidentified",
                        }
                    )
                    return SendOutcome.failure("wechat_native_http_unavailable")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    raise AssertionError("unexpected file send")

                def close(self) -> None:
                    return None

            backend = _RevokeAfterFirstFailureBackend()
            with mock.patch("app.personal_wechat_bot.runtime.send_bridge_worker.time.sleep"):
                BridgeWorker(data_dir, backend, max_send_attempts=3).run_once()

            self.assertEqual(backend.calls, 1)
            item = store.state(limit=10)["items"][0]
            self.assertEqual(item["status"], BridgeAckStatus.BLOCKED)
            self.assertEqual(
                item["ack"]["reason"],
                "receiver_not_authorized:private_contact_unknown_or_unidentified",
            )

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

    def test_real_worker_blocks_receiver_that_does_not_match_authorized_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, backend="wechat_native_http")
            conversation_id = "authorized-private"
            _write_channel(
                data_dir,
                conversation_id,
                {
                    "conversation_id": conversation_id,
                    "conversation_type": "private",
                    "chat_title": "Alice",
                    "conversation_key": "wxid_real_alice",
                    "sender_wechat_ids": ["wxid_real_alice"],
                    "source_names": ["weflow_discovery"],
                    "trusted_channel_source": True,
                    "is_friend": True,
                    "contact_authorization": "explicit_friend",
                },
            )
            store = BridgeOutboxStore(data_dir)
            store.enqueue(conversation_id, "should not touch real backend", receiver="wxid_other_contact")

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
            self.assertEqual(item["ack"]["reason"], "receiver_not_authorized:receiver_channel_mismatch")

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

    def test_worker_rechecks_terminal_ack_after_heartbeat_before_wire_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "manual stop in race window")
            backend = DryRunSendBackend()
            injected = {"done": False}

            def inject_manual_block() -> None:
                if injected["done"]:
                    return
                injected["done"] = True
                store.append_ack(rec["bridge_id"], status=BridgeAckStatus.BLOCKED, reason="manual_block")

            processed = BridgeWorker(data_dir, backend, heartbeat=inject_manual_block).run_once()
            acks = [ack for ack in store._read_all(store.ack_path) if ack["bridge_id"] == rec["bridge_id"]]

            self.assertEqual(processed, 1)
            self.assertEqual(backend.sent_texts, [])
            self.assertEqual(
                [ack["status"] for ack in acks],
                [BridgeAckStatus.INFLIGHT, BridgeAckStatus.BLOCKED],
            )
            self.assertEqual(store.state(limit=10)["items"][0]["status"], BridgeAckStatus.BLOCKED)

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

            executor = GuardedSendExecutor(config, driver)
            result = executor.execute_confirmed(reply)
            executor.activate_staged(result, expected_projections=[])

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

    def test_post_connect_failures_are_quarantined_after_one_wire_attempt(self) -> None:
        failure_reasons = [
            "wechat_native_http_send_text_error:ConnectionResetError:connection reset",
            "wechat_native_http_send_text_error:BrokenPipeError:broken pipe",
            "wechat_native_http_send_text_error:ValueError:http_500:server error",
            "weflow_http_send_text_error:RemoteDisconnected:Remote end closed connection without response",
            "weflow_http_send_text_error:BadStatusLine:invalid HTTP status",
            "weflow_http_send_text_error:IncompleteRead:response ended early",
            "weflow_http_send_text_error:ConnectionError:response ended unexpectedly",
            "weflow_http_send_text_error:SSLEOFError:SSL EOF occurred",
        ]
        for failure_reason in failure_reasons:
            with self.subTest(reason=failure_reason), tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                create_default_config(data_dir)
                store = BridgeOutboxStore(data_dir)
                rec = store.enqueue("wxid_a", "ambiguous delivery")

                class _AmbiguousBackend:
                    name = "ambiguous"

                    def __init__(self) -> None:
                        self.calls = 0

                    def health_check(self) -> bool:
                        return True

                    def send_text(self, receiver: str, text: str) -> SendOutcome:
                        self.calls += 1
                        return SendOutcome.failure(failure_reason)

                    def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                        self.calls += 1
                        return SendOutcome.failure(failure_reason)

                    def close(self) -> None:
                        return None

                backend = _AmbiguousBackend()
                BridgeWorker(data_dir, backend, max_send_attempts=3).run_once()
                item = store.state(limit=10)["items"][0]

                self.assertEqual(backend.calls, 1)
                self.assertEqual(item["status"], BridgeAckStatus.FAILED)
                self.assertIn("unknown_delivery_state", item["ack"]["reason"])
                self.assertFalse(item["retryable"])
                with self.assertRaises(ValueError):
                    store.requeue_resolved(rec["bridge_id"])

    def test_connection_refused_remains_safely_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "backend not listening")

            class _RefusedBackend:
                name = "refused"

                def __init__(self) -> None:
                    self.calls = 0

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    self.calls += 1
                    return SendOutcome.failure(
                        "wechat_native_http_send_text_error:ConnectionError:[WinError 10061] "
                        "No connection could be made because the target actively refused it"
                    )

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    return SendOutcome.failure("ConnectionRefusedError:refused")

                def close(self) -> None:
                    return None

            backend = _RefusedBackend()
            BridgeWorker(data_dir, backend, max_send_attempts=1).run_once()
            item = store.state(limit=10)["items"][0]

            self.assertEqual(backend.calls, 1)
            self.assertEqual(item["status"], BridgeAckStatus.RETRY)
            self.assertEqual(store.state(limit=10)["pending_count"], 1)

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

    def test_terminal_ack_upgrade_retries_sync_for_new_fingerprint(self) -> None:
        import app.personal_wechat_bot.runtime.send_bridge_worker as worker_mod

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            record = store.enqueue("wxid_a", "accepted then verified")
            store.append_ack(record["bridge_id"], status=BridgeAckStatus.ACCEPTED, reason="accepted")
            worker = BridgeWorker(data_dir, DryRunSendBackend())
            accepted_ack = worker._effective_ack_state(record["bridge_id"])
            self.assertIsNotNone(accepted_ack)
            worker._mark_synced(
                record["bridge_id"],
                worker._sync_fingerprint(record["bridge_id"], accepted_ack.ack),
            )

            with mock.patch.object(
                worker_mod,
                "sync_bridge_ack_to_send_state",
                side_effect=OSError("sent projection locked"),
            ):
                self.assertTrue(worker._ack(record["bridge_id"], BridgeAckStatus.SENT, "verified"))

            sent_ack = worker._effective_ack_state(record["bridge_id"])
            self.assertIsNotNone(sent_ack)
            sent_fingerprint = worker._sync_fingerprint(record["bridge_id"], sent_ack.ack)
            self.assertNotEqual(worker._load_synced()[record["bridge_id"]], sent_fingerprint)

            with mock.patch.object(
                worker_mod,
                "sync_bridge_ack_to_send_state",
                return_value={"sync_complete": True, "queue_error": ""},
            ) as sync:
                worker._reconcile_unsynced_acks()

            self.assertEqual(sync.call_count, 1)
            self.assertEqual(sync.call_args.kwargs["status"], BridgeAckStatus.SENT)
            self.assertEqual(worker._load_synced()[record["bridge_id"]], sent_fingerprint)

    def test_projection_contract_change_invalidates_terminal_sync_proof(self) -> None:
        import app.personal_wechat_bot.runtime.send_bridge_worker as worker_mod

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            record = store.enqueue("wxid_a", "contract races recovery", staged=True)
            store.set_staged_projection_contract([record["bridge_id"]], expected_projections=[])
            store.append_ack(record["bridge_id"], status=BridgeAckStatus.FAILED, reason="owner exited")
            worker = BridgeWorker(data_dir, DryRunSendBackend())
            state = worker._effective_ack_state(record["bridge_id"])
            self.assertIsNotNone(state)
            old_fingerprint = worker._sync_fingerprint(record["bridge_id"], state.ack)
            worker._mark_synced(record["bridge_id"], old_fingerprint)

            store.set_staged_projection_contract([record["bridge_id"]], expected_projections=["queue"])
            self.assertNotEqual(
                worker._load_synced()[record["bridge_id"]],
                worker._sync_fingerprint(record["bridge_id"], state.ack),
            )
            with mock.patch.object(
                worker_mod,
                "sync_bridge_ack_to_send_state",
                return_value={"sync_complete": True, "queue_error": ""},
            ) as sync:
                worker._reconcile_unsynced_acks()

            self.assertEqual(sync.call_count, 1)
            self.assertEqual(
                worker._load_synced()[record["bridge_id"]],
                worker._sync_fingerprint(record["bridge_id"], state.ack),
            )

    def test_ack_only_terminal_upgrade_survives_sync_failure_and_compaction(self) -> None:
        import app.personal_wechat_bot.runtime.send_bridge_worker as worker_mod

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            record = store.enqueue("wxid_a", "verified after history compaction")
            store.append_ack(record["bridge_id"], status=BridgeAckStatus.FAILED, reason="initial failure")
            worker = BridgeWorker(data_dir, DryRunSendBackend())
            failed_state = worker._effective_ack_state(record["bridge_id"])
            worker._mark_synced(
                record["bridge_id"],
                worker._sync_fingerprint(record["bridge_id"], failed_state.ack),
            )
            store.compact(keep_resolved=0, synced_ack_fingerprints=worker._load_synced())
            store.append_ack(record["bridge_id"], status=BridgeAckStatus.SENT, reason="verified later")

            with mock.patch.object(
                worker_mod,
                "sync_bridge_ack_to_send_state",
                side_effect=OSError("projection locked"),
            ):
                worker._reconcile_unsynced_acks()
            preserved = store.compact(
                keep_resolved=0,
                synced_ack_fingerprints=worker._load_synced(),
            )

            self.assertEqual(preserved, {"removed_outbox": 0, "removed_acks": 0})
            self.assertEqual(worker._effective_ack_state(record["bridge_id"]).status, BridgeAckStatus.SENT)

            with mock.patch.object(
                worker_mod,
                "sync_bridge_ack_to_send_state",
                return_value={"sync_complete": True, "queue_error": ""},
            ):
                worker._reconcile_unsynced_acks()
            removed = store.compact(
                keep_resolved=0,
                synced_ack_fingerprints=worker._load_synced(),
            )
            self.assertEqual(removed, {"removed_outbox": 0, "removed_acks": 1})

    def test_legacy_synced_id_list_forces_resync_and_migrates_to_v3(self) -> None:
        import app.personal_wechat_bot.runtime.send_bridge_worker as worker_mod

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            record = store.enqueue("wxid_a", "legacy marker")
            store.append_ack(record["bridge_id"], status=BridgeAckStatus.FAILED, reason="failed")
            worker = BridgeWorker(data_dir, DryRunSendBackend())
            worker._synced_marker_path().write_text(
                json.dumps({"synced": [record["bridge_id"]]}),
                encoding="utf-8",
            )

            self.assertEqual(worker._load_synced(), {})
            with mock.patch.object(
                worker_mod,
                "sync_bridge_ack_to_send_state",
                return_value={"sync_complete": True, "queue_error": ""},
            ) as sync:
                worker._reconcile_unsynced_acks()

            payload = json.loads(worker._synced_marker_path().read_text(encoding="utf-8"))
            self.assertEqual(sync.call_count, 1)
            self.assertEqual(payload["version"], 3)
            self.assertEqual(
                payload["synced"][record["bridge_id"]],
                worker._sync_fingerprint(
                    record["bridge_id"],
                    worker._effective_ack_state(record["bridge_id"]).ack,
                ),
            )

    def test_prune_synced_marker_drops_missing_and_stale_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            stale = store.enqueue("wxid_a", "upgraded")
            store.append_ack(stale["bridge_id"], status=BridgeAckStatus.FAILED, reason="failed")
            retained = store.enqueue("wxid_a", "current")
            store.append_ack(retained["bridge_id"], status=BridgeAckStatus.FAILED, reason="failed")
            worker = BridgeWorker(data_dir, DryRunSendBackend())
            worker._mark_synced(
                stale["bridge_id"],
                worker._sync_fingerprint(
                    stale["bridge_id"],
                    worker._effective_ack_state(stale["bridge_id"]).ack,
                ),
            )
            retained_fingerprint = worker._sync_fingerprint(
                retained["bridge_id"],
                worker._effective_ack_state(retained["bridge_id"]).ack,
            )
            worker._mark_synced(retained["bridge_id"], retained_fingerprint)
            marker = worker._load_synced()
            marker["bridge:missing"] = "bridge-ack-v1:missing"
            worker._synced_marker_path().write_text(
                json.dumps({"version": 3, "synced": marker}),
                encoding="utf-8",
            )
            store.append_ack(stale["bridge_id"], status=BridgeAckStatus.SENT, reason="verified")

            worker._prune_synced_marker()

            self.assertEqual(worker._load_synced(), {retained["bridge_id"]: retained_fingerprint})

    def test_corrupt_worker_markers_are_quarantined_without_losing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            worker = BridgeWorker(data_dir, DryRunSendBackend())
            cases = [
                (worker._synced_marker_path(), worker._load_synced, {}),
                (worker._accepted_reverify_marker_path(), worker._load_accepted_reverify_marker, {}),
            ]
            for path, loader, expected in cases:
                with self.subTest(marker=path.name):
                    evidence = b'\xff\xfe{"truncated":'
                    path.write_bytes(evidence)

                    loaded = loader()
                    quarantined = list(path.parent.glob(f"{path.name}.corrupt.*"))

                    self.assertEqual(loaded, expected)
                    self.assertFalse(path.exists())
                    self.assertEqual(len(quarantined), 1)
                    self.assertEqual(quarantined[0].read_bytes(), evidence)

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

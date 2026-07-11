from __future__ import annotations

import concurrent.futures
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.config.loader import create_default_config
from app.personal_wechat_bot.conversation.channel_registry_store import ChannelRegistryStore
from app.personal_wechat_bot.conversation.segment import conversation_segment
from app.personal_wechat_bot.wechat_driver.bridge_send import (
    BridgeAckStatus,
    BridgeOutboxSendDriver,
    BridgeOutboxStore,
    bridge_sync_fingerprint,
    bridge_ack,
    bridge_ack_if_queued,
    bridge_requeue_resolved,
    bridge_state,
    effective_bridge_ack_states,
)


def _write_channel(data_dir: Path, conversation_id: str, payload: dict) -> None:
    """Write SQLite authority plus the readable channel projections."""
    chat_title = str(payload.get("chat_title", "") or "")
    segment = conversation_segment(conversation_id, chat_title)
    payload = {**payload, "conversation_id": conversation_id, "segment": segment}
    ChannelRegistryStore(data_dir).upsert(payload)
    channel_dir = data_dir / "conversation_channels" / segment
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / "channel.json").write_text(json.dumps(payload), encoding="utf-8")
    index_path = data_dir / "conversation_channels" / "index.json"
    (index_path).write_text(
        json.dumps({"channels": [{"conversation_id": conversation_id, "chat_title": chat_title}]}),
        encoding="utf-8",
    )


def _synced_ack_fingerprints(store: BridgeOutboxStore) -> dict[str, str]:
    outbox_by_id = {
        str(record.get("bridge_id", "")): record
        for record in store._read_all(store.outbox_path)
        if str(record.get("bridge_id", ""))
    }
    return {
        bridge_id: bridge_sync_fingerprint(state.ack, outbox_by_id.get(bridge_id))
        for bridge_id, state in effective_bridge_ack_states(store._read_all(store.ack_path)).items()
        if state.terminal
    }


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
            ack = bridge_ack(data_dir, result.message_id, status="sent", reason="native_sent")
            confirmed = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "queued_to_bridge")
            self.assertIn("queued_to_non_foreground_bridge", result.reason)
            self.assertEqual(queued["pending_count"], 1)
            self.assertEqual(queued["items"][0]["text"], "hello bridge")
            self.assertEqual(ack["status"], "ok")
            self.assertEqual(confirmed["pending_count"], 0)
            self.assertEqual(confirmed["sent_count"], 1)
            self.assertEqual(confirmed["failed_count"], 0)
            self.assertEqual(confirmed["items"][0]["status"], "sent")

    def test_wechat_native_backend_unavailable_blocks_before_outbox_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            with mock.patch(
                "app.personal_wechat_bot.wechat_driver.bridge_send.wechat_native_http_status",
                return_value={"available": False, "reason": "ConnectionRefusedError:refused"},
            ):
                driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir, send_backend="wechat_native_http")

                result = driver.send_message("private-1", "hello bridge")
                state = bridge_state(data_dir, limit=10)
                probe = driver.probe()

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "wechat_native_backend_unavailable:ConnectionRefusedError:refused")
            self.assertEqual(state["count"], 0)
            self.assertIn("wechat_native_http_unavailable", probe.blockers)

    def test_wechat_native_worker_lock_with_verify_timeouts_matches_driver_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            conversation_id = "native-known-private"
            _write_channel(
                data_dir,
                conversation_id,
                {
                    "conversation_id": conversation_id,
                    "conversation_type": "private",
                    "chat_title": "Alice",
                    "conversation_key": "wxid_a",
                    "sender_wechat_ids": ["wxid_a"],
                    "source_names": ["backend_events_jsonl"],
                    "trusted_channel_source": True,
                    "is_friend": True,
                    "contact_authorization": "explicit_friend",
                },
            )
            driver = BridgeOutboxSendDriver(
                send_enabled=True,
                data_dir=data_dir,
                send_backend="wechat_native_http",
                wechat_native_verify_timeout_seconds=2.5,
                wechat_native_file_verify_timeout_seconds=12.0,
            )
            signature = driver._worker_expected_config_signature()
            lock_path = data_dir / "send_bridge" / ".bridge_worker.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 0,
                        "label": "send_bridge_worker",
                        "heartbeat_at": time.time(),
                        "backend_name": "wechat_native_http",
                        "config_signature": signature,
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch(
                "app.personal_wechat_bot.wechat_driver.bridge_send.wechat_native_http_status",
                return_value={"available": True, "reason": ""},
            ):
                result = driver.send_message(conversation_id, "hello bridge")
                probe = driver.probe()
            state = bridge_state(data_dir, limit=10)

            self.assertEqual(signature["wechat_native_verify_timeout_seconds"], 2.5)
            self.assertEqual(signature["wechat_native_file_verify_timeout_seconds"], 12.0)
            self.assertEqual(probe.backend["worker_config"]["config_status"], "matched")
            self.assertNotIn("bridge_worker_stale_config", " ".join(probe.blockers))
            self.assertEqual(result.status, "queued_to_bridge")
            self.assertEqual(state["pending_count"], 1)
            self.assertEqual(state["items"][0]["receiver"], "wxid_a")

    def test_weflow_send_capability_false_blocks_before_outbox_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            with mock.patch(
                "app.personal_wechat_bot.wechat_driver.bridge_send.weflow_http_status",
                return_value={
                    "available": True,
                    "token_present": True,
                    "send_capabilities": {
                        "text": {"supports": False},
                        "file": {"supports": False},
                        "backend": "native-not-implemented",
                    },
                },
            ):
                driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir, send_backend="weflow_http")

                text = driver.send_message("private-1", "hello bridge")
                file_result = driver.send_file("private-1", str(data_dir / "report.pdf"))
                probe = driver.probe()
                state = bridge_state(data_dir, limit=10)

            self.assertEqual(text.status, "failed")
            self.assertIn("weflow_text_send_not_supported", text.reason)
            self.assertEqual(file_result.status, "failed")
            self.assertIn("weflow_file_send_not_supported", file_result.reason)
            self.assertEqual(probe.health, "blocked")
            self.assertTrue(any("weflow_text_send_not_supported" in item for item in probe.blockers))
            self.assertEqual(state["count"], 0)

    def test_stale_worker_lock_blocks_before_outbox_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir, send_backend="weflow_http")
            lock_path = data_dir / "send_bridge" / ".bridge_worker.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 0,
                        "label": "send_bridge_worker",
                        "heartbeat_at": time.time(),
                        "backend_name": "dry_run",
                        "config_signature": {"send_backend": "dry_run"},
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch(
                "app.personal_wechat_bot.wechat_driver.bridge_send.weflow_http_status",
                return_value={
                    "available": True,
                    "token_present": True,
                    "send_capabilities": {"text": {"supports": True}, "backend": "native"},
                },
            ):
                result = driver.send_message("private-1", "hello bridge")
                probe = driver.probe()
            state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "failed")
            self.assertIn("bridge_worker_stale_config", result.reason)
            self.assertIn("bridge_worker_stale_config", " ".join(probe.blockers))
            self.assertEqual(probe.backend["worker_config"]["config_status"], "stale")
            self.assertEqual(state["count"], 0)

    def test_unknown_legacy_worker_lock_blocks_before_outbox_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)
            lock_path = data_dir / "send_bridge" / ".bridge_worker.lock"
            lock_path.write_text(
                json.dumps({"pid": 0, "label": "send_bridge_worker", "heartbeat_at": time.time()}),
                encoding="utf-8",
            )

            result = driver.send_message("private-1", "hello bridge")
            state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "bridge_worker_config_unknown")
            self.assertEqual(state["count"], 0)

    def test_terminal_ack_is_not_overridden_by_stale_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "already sent")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.SENT, reason="native_sent")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.RETRY, reason="stale_retry")

            state = bridge_state(data_dir, limit=10)

            self.assertEqual(state["pending_count"], 0)
            self.assertEqual(state["items"][0]["status"], BridgeAckStatus.SENT)
            self.assertEqual(state["items"][0]["ack"]["reason"], "native_sent")

    def test_sent_ack_is_not_downgraded_by_later_failed_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "wire result wins")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.SENT, reason="native_sent")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.FAILED, reason="stale_failed")

            state = bridge_state(data_dir, limit=10)

            self.assertEqual(state["pending_count"], 0)
            self.assertEqual(state["items"][0]["status"], BridgeAckStatus.SENT)
            self.assertEqual(state["items"][0]["ack"]["reason"], "native_sent")

    def test_blocked_ack_is_not_overridden_by_later_inflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "operator stopped")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.BLOCKED, reason="manual_block")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.INFLIGHT, reason="stale_inflight")

            state = bridge_state(data_dir, limit=10)

            self.assertEqual(state["pending_count"], 0)
            self.assertEqual(state["items"][0]["status"], BridgeAckStatus.BLOCKED)
            self.assertEqual(state["items"][0]["ack"]["reason"], "manual_block")

    def test_failed_bridge_item_can_be_requeued_to_fresh_outbox_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "please send")
            store.append_ack(
                rec["bridge_id"],
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
            )

            before = store.state(limit=10)
            retry = bridge_requeue_resolved(data_dir, rec["bridge_id"], reason="test_retry")
            after = store.state(limit=10)
            items = {item["bridge_id"]: item for item in after["items"]}
            new_bridge_id = retry["new_bridge_id"]

            self.assertTrue(before["items"][0]["retryable"])
            self.assertEqual(before["open_problem_count"], 0)
            self.assertEqual(before["historical_failed_count"], 1)
            self.assertNotEqual(new_bridge_id, rec["bridge_id"])
            self.assertEqual(items[new_bridge_id]["status"], "queued")
            self.assertEqual(items[new_bridge_id]["retry_of"], rec["bridge_id"])
            self.assertEqual(items[new_bridge_id]["retry_reason"], "test_retry")
            self.assertEqual(after["pending_count"], 1)
            self.assertEqual(after["historical_failed_count"], 1)

    def test_concurrent_requeue_reuses_one_active_successor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "retry once")
            store.append_ack(
                rec["bridge_id"],
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
            )

            def requeue(index: int) -> dict:
                return bridge_requeue_resolved(data_dir, rec["bridge_id"], reason=f"retry-{index}")

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(requeue, range(16)))

            successor_ids = {str(item["new_bridge_id"]) for item in results}
            state = store.state(limit=30)
            original = next(item for item in state["items"] if item["bridge_id"] == rec["bridge_id"])

            self.assertEqual(len(successor_ids), 1)
            self.assertEqual(sum(1 for item in results if item["created"]), 1)
            self.assertEqual(sum(1 for item in results if item["reused_existing"]), 15)
            self.assertEqual(state["count"], 2)
            self.assertEqual(state["pending_count"], 1)
            self.assertFalse(original["retryable"])
            self.assertEqual(original["active_retry_bridge_id"], next(iter(successor_ids)))

    def test_requeue_of_ancestor_reuses_active_transitive_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            original = store.enqueue("wxid_a", "retry chain")
            failure_reason = "wechat_native_http_send_text_error:ConnectionRefusedError:refused"
            store.append_ack(original["bridge_id"], status=BridgeAckStatus.FAILED, reason=failure_reason)
            first = bridge_requeue_resolved(data_dir, original["bridge_id"], reason="first")
            store.append_ack(first["new_bridge_id"], status=BridgeAckStatus.FAILED, reason=failure_reason)
            second = bridge_requeue_resolved(data_dir, original["bridge_id"], reason="second-from-stale-ancestor")

            replay = bridge_requeue_resolved(data_dir, original["bridge_id"], reason="ancestor-replay")
            state = store.state(limit=20)

            self.assertEqual(second["retry_parent_id"], first["new_bridge_id"])
            self.assertEqual(second["record"]["retry_of"], first["new_bridge_id"])
            self.assertTrue(replay["reused_existing"])
            self.assertFalse(replay["created"])
            self.assertEqual(replay["new_bridge_id"], second["new_bridge_id"])
            self.assertEqual(state["count"], 3)
            self.assertEqual(state["pending_count"], 1)

    def test_terminal_retry_descendant_permanently_blocks_ancestor_requeue(self) -> None:
        cases = [
            (BridgeAckStatus.SENT, "native_sent"),
            (BridgeAckStatus.ACCEPTED, "native_accepted_unverified"),
            (BridgeAckStatus.FAILED, "unknown_delivery_state:ConnectionResetError:reset"),
        ]
        for descendant_status, descendant_reason in cases:
            with self.subTest(status=descendant_status), tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "data"
                store = BridgeOutboxStore(data_dir)
                original = store.enqueue("wxid_a", "retry lineage")
                store.append_ack(
                    original["bridge_id"],
                    status=BridgeAckStatus.FAILED,
                    reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
                )
                successor = bridge_requeue_resolved(data_dir, original["bridge_id"], reason="first")
                store.append_ack(
                    successor["new_bridge_id"],
                    status=descendant_status,
                    reason=descendant_reason,
                )

                with self.assertRaises(ValueError):
                    bridge_requeue_resolved(data_dir, original["bridge_id"], reason="must-not-fork")

                self.assertEqual(store.state(limit=20)["count"], 2)

    def test_manual_terminal_ack_is_atomic_and_only_applies_while_queued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            queued = store.enqueue("wxid_a", "stop before send")

            applied = bridge_ack_if_queued(
                data_dir,
                queued["bridge_id"],
                status=BridgeAckStatus.BLOCKED,
                reason="manual_block",
            )
            replay = bridge_ack_if_queued(
                data_dir,
                queued["bridge_id"],
                status=BridgeAckStatus.FAILED,
                reason="late_manual_failure",
            )
            missing = bridge_ack_if_queued(
                data_dir,
                "bridge:missing",
                status=BridgeAckStatus.BLOCKED,
                reason="missing",
            )

            self.assertTrue(applied["applied"])
            self.assertEqual(applied["status"], "ok")
            self.assertFalse(replay["applied"])
            self.assertEqual(replay["effective_status"], BridgeAckStatus.BLOCKED)
            self.assertFalse(missing["applied"])
            self.assertEqual(missing["effective_status"], "missing")

    def test_manual_terminal_ack_rejects_staged_item_until_contract_is_durable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            staged = store.enqueue("wxid_a", "projection pending", staged=True)

            refused = bridge_ack_if_queued(
                data_dir,
                staged["bridge_id"],
                status=BridgeAckStatus.BLOCKED,
                reason="manual_block",
            )
            store.set_staged_projection_contract(
                [staged["bridge_id"]],
                expected_projections=[],
            )
            applied = bridge_ack_if_queued(
                data_dir,
                staged["bridge_id"],
                status=BridgeAckStatus.FAILED,
                reason="projection_publish_failed",
            )

            self.assertEqual(refused["status"], "conflict")
            self.assertEqual(refused["reason"], "bridge_item_staged_without_projection_contract")
            self.assertEqual(store._read_all(store.ack_path)[0]["status"], BridgeAckStatus.FAILED)
            self.assertTrue(applied["applied"])

    def test_abandoned_staged_record_is_quarantined_without_becoming_deliverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            record = store.enqueue("wxid_a", "never activate", staged=True)

            with mock.patch(
                "app.personal_wechat_bot.wechat_driver.bridge_send.process_pid_alive",
                return_value=False,
            ):
                quarantined = store.quarantine_abandoned_staged_records()

            state = store.state(limit=10)
            item = next(value for value in state["items"] if value["bridge_id"] == record["bridge_id"])
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(item["status"], BridgeAckStatus.FAILED)
            self.assertFalse(item["delivery_ready"])
            self.assertEqual(item["expected_projections"], [])
            self.assertFalse(item["ack"]["payload"]["delivery_attempted"])
            with self.assertRaisesRegex(ValueError, "already terminal"):
                store.activate_staged_record(
                    record["bridge_id"],
                    expected_projections=["queue", "ledger"],
                )
            conflicted = next(
                value
                for value in store._read_all(store.outbox_path)
                if value["bridge_id"] == record["bridge_id"]
            )
            self.assertEqual(conflicted["expected_projections"], ["queue", "ledger"])

    def test_live_fresh_staged_record_is_not_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BridgeOutboxStore(Path(tmp) / "data")
            record = store.enqueue("wxid_a", "still projecting", staged=True)

            quarantined = store.quarantine_abandoned_staged_records()

            self.assertEqual(quarantined, [])
            item = next(value for value in store.state(limit=10)["items"] if value["bridge_id"] == record["bridge_id"])
            self.assertEqual(item["status"], "queued")
            self.assertFalse(item["delivery_ready"])

    def test_live_old_staged_owner_is_not_fenced_by_wall_clock_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BridgeOutboxStore(Path(tmp) / "data")
            record = store.enqueue("wxid_a", "slow projection", staged=True)
            outbox = store._read_all(store.outbox_path)
            outbox[0]["staging_owner"]["created_at"] = "2000-01-01T00:00:00+00:00"
            store._rewrite(store.outbox_path, outbox)

            quarantined = store.quarantine_abandoned_staged_records()

            self.assertEqual(quarantined, [])
            item = next(value for value in store.state(limit=10)["items"] if value["bridge_id"] == record["bridge_id"])
            self.assertEqual(item["status"], "queued")
            self.assertFalse(item["delivery_ready"])

    def test_old_legacy_staged_record_without_owner_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BridgeOutboxStore(Path(tmp) / "data")
            record = store.enqueue("wxid_a", "legacy staged", staged=True)
            outbox = store._read_all(store.outbox_path)
            outbox[0].pop("staging_owner", None)
            outbox[0]["created_at"] = "2000-01-01T00:00:00+00:00"
            store._rewrite(store.outbox_path, outbox)

            quarantined = store.quarantine_abandoned_staged_records()

            self.assertEqual([item["bridge_id"] for item in quarantined], [record["bridge_id"]])
            self.assertEqual(store.state(limit=10)["items"][0]["status"], BridgeAckStatus.FAILED)

    def test_abandoned_staged_record_preserves_durable_projection_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BridgeOutboxStore(Path(tmp) / "data")
            record = store.enqueue("wxid_a", "contract written before crash", staged=True)
            store.set_staged_projection_contract(
                [record["bridge_id"]],
                expected_projections=["ledger", "task"],
            )

            with mock.patch(
                "app.personal_wechat_bot.wechat_driver.bridge_send.process_pid_alive",
                return_value=False,
            ):
                quarantined = store.quarantine_abandoned_staged_records()

            item = next(value for value in store.state(limit=10)["items"] if value["bridge_id"] == record["bridge_id"])
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(item["expected_projections"], ["ledger", "task"])
            self.assertEqual(item["status"], BridgeAckStatus.FAILED)

    def test_permanent_native_media_failures_are_not_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            unsupported = store.enqueue_file("wxid_a", str(data_dir / "image.png"))
            missing_route = store.enqueue_file("wxid_a", str(data_dir / "report.txt"))
            store.append_ack(
                unsupported["bridge_id"],
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_image_failed:unsupported_on_411053_text_only",
            )
            store.append_ack(
                missing_route["bridge_id"],
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_file_error:ValueError:http_404:",
            )

            state = store.state(limit=10)
            by_id = {item["bridge_id"]: item for item in state["items"]}

            self.assertFalse(by_id[unsupported["bridge_id"]]["retryable"])
            self.assertFalse(by_id[missing_route["bridge_id"]]["retryable"])
            with self.assertRaises(ValueError):
                store.requeue_resolved(unsupported["bridge_id"], reason="should_not_retry")
            with self.assertRaises(ValueError):
                store.requeue_resolved(missing_route["bridge_id"], reason="should_not_retry")

    def test_true_sent_bridge_item_is_not_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "already sent")
            store.append_ack(
                rec["bridge_id"],
                status=BridgeAckStatus.SENT,
                reason="wechat_native_http_send_text",
                payload={"backend": "wechat_native_http", "delivery_verified": True},
            )

            state = store.state(limit=10)

            self.assertFalse(state["items"][0]["retryable"])
            self.assertIn("already marked sent", state["items"][0]["retry_blocker"])
            with self.assertRaises(ValueError):
                store.requeue_resolved(rec["bridge_id"], reason="should_not_retry")

    def test_unverified_native_sent_ack_is_projected_as_accepted_and_not_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "maybe sent")
            store.append_ack(
                rec["bridge_id"],
                status=BridgeAckStatus.SENT,
                reason="wechat_native_http_send_text",
                payload={
                    "backend": "wechat_native_http",
                    "operation": "wechat_native_http_send_text",
                    "response": {"ret": 0, "retmsg": "success"},
                },
            )

            state = store.state(limit=10)

            self.assertEqual(state["sent_count"], 0)
            self.assertEqual(state["accepted_count"], 1)
            self.assertEqual(state["items"][0]["status"], BridgeAckStatus.ACCEPTED)
            self.assertEqual(state["items"][0]["ack"]["original_status"], BridgeAckStatus.SENT)
            self.assertFalse(state["items"][0]["retryable"])
            self.assertIn("may already be delivered", state["items"][0]["retry_blocker"])
            with self.assertRaises(ValueError):
                store.requeue_resolved(rec["bridge_id"], reason="unsafe_duplicate_retry")

    def test_bridge_state_separates_legacy_hook_unverified_from_active_native_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            legacy = store.enqueue("wxid_old", "old hook residual")
            active = store.enqueue("wxid_live", "native accepted")
            verified = store.enqueue("wxid_ok", "native verified")
            store.append_ack(
                legacy["bridge_id"],
                status=BridgeAckStatus.ACCEPTED,
                reason="wechat_hook_http_send_text_accepted_unverified",
                payload={"backend": "wechat_hook_http", "delivery_verified": False},
            )
            store.append_ack(
                active["bridge_id"],
                status=BridgeAckStatus.ACCEPTED,
                reason="wechat_native_http_send_text_accepted_unverified",
                payload={
                    "backend": "wechat_native_http",
                    "delivery_verified": False,
                    "accepted_unverified": True,
                },
            )
            store.append_ack(
                verified["bridge_id"],
                status=BridgeAckStatus.SENT,
                reason="wechat_native_http_send_text_verified",
                payload={"backend": "wechat_native_http", "delivery_verified": True},
            )

            state = store.state(limit=10)

            self.assertEqual(state["accepted_count"], 2)
            self.assertEqual(state["legacy_hook_unverified_count"], 1)
            self.assertEqual(state["active_unverified_count"], 1)
            self.assertEqual(state["active_problem_count"], 1)
            self.assertEqual(state["unverified_by_backend"]["wechat_hook_http"], 1)
            self.assertEqual(state["unverified_by_backend"]["wechat_native_http"], 1)
            self.assertEqual(state["status_counts_by_backend"]["wechat_native_http"][BridgeAckStatus.SENT], 1)

    def test_dry_run_sent_bridge_item_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "dry run only")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.SENT, reason="dry_run_not_delivered:text")

            state = store.state(limit=10)
            retry = store.requeue_resolved(rec["bridge_id"], reason="switch_to_real_backend")

            self.assertTrue(state["items"][0]["retryable"])
            self.assertNotEqual(retry["bridge_id"], rec["bridge_id"])

    def test_bridge_send_queues_by_conversation_receiver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)

            result = driver.send_message("private-1", "hello bridge")
            state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "queued_to_bridge")
            self.assertIn("queued_to_non_foreground_bridge", result.reason)
            self.assertEqual(state["pending_count"], 1)
            self.assertEqual(state["items"][0]["text"], "hello bridge")
            self.assertNotIn("manual_binding", state["items"][0])

    def test_bridge_send_uses_channel_receiver_for_hashed_conversation_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = "abc123hashedconversation"
            _write_channel(
                data_dir,
                conversation_id,
                {
                    "conversation_id": conversation_id,
                    "conversation_type": "private",
                    "chat_title": "Alice",
                    "sender_wechat_ids": ["wxid_real_alice"],
                    "source_names": ["weflow_discovery"],
                    "trusted_channel_source": True,
                    "is_friend": True,
                    "contact_authorization": "explicit_friend",
                },
            )
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)

            result = driver.send_message(conversation_id, "hello bridge")
            state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "queued_to_bridge")
            self.assertEqual(state["items"][0]["conversation_id"], conversation_id)
            self.assertEqual(state["items"][0]["receiver"], "wxid_real_alice")

    def test_real_bridge_send_blocks_raw_private_receiver_without_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            with mock.patch(
                "app.personal_wechat_bot.wechat_driver.bridge_send.wechat_native_http_status",
                return_value={"available": True, "reason": "", "health": {"IsLogin": 1}},
            ):
                driver = BridgeOutboxSendDriver(
                    send_enabled=True,
                    data_dir=data_dir,
                    send_backend="wechat_native_http",
                )

                result = driver.send_message("wxid_unidentified", "do not send")
                state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "receiver_not_authorized:missing_channel")
            self.assertEqual(state["count"], 0)

    def test_real_bridge_send_blocks_raw_group_receiver_without_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            with mock.patch(
                "app.personal_wechat_bot.wechat_driver.bridge_send.wechat_native_http_status",
                return_value={"available": True, "reason": "", "health": {"IsLogin": 1}},
            ):
                driver = BridgeOutboxSendDriver(
                    send_enabled=True,
                    data_dir=data_dir,
                    send_backend="wechat_native_http",
                )

                result = driver.send_message("12345678@chatroom", "do not send")
                state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "receiver_not_authorized:missing_channel")
            self.assertEqual(state["count"], 0)

    def test_real_bridge_send_blocks_unidentified_legacy_private_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
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
            with mock.patch(
                "app.personal_wechat_bot.wechat_driver.bridge_send.wechat_native_http_status",
                return_value={"available": True, "reason": "", "health": {"IsLogin": 1}},
            ):
                driver = BridgeOutboxSendDriver(
                    send_enabled=True,
                    data_dir=data_dir,
                    send_backend="wechat_native_http",
                )

                result = driver.send_message(conversation_id, "do not send")
                state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "receiver_not_authorized:private_contact_unknown_or_unidentified")
            self.assertEqual(state["count"], 0)

    def test_filehelper_raw_receiver_is_allowed_for_bridge_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)

            result = driver.send_message("filehelper", "hello bridge")
            state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "queued_to_bridge")
            self.assertEqual(state["items"][0]["conversation_id"], "filehelper")
            self.assertEqual(state["items"][0]["receiver"], "filehelper")

    def test_group_reply_routes_to_roomid_not_member_wxid(self) -> None:
        # Regression: a group channel's sender_wechat_ids holds speaking members'
        # wxids. The reply must go to the group's roomid (from conversation_key),
        # never privately to a member.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = "grouphash000000000000000"
            _write_channel(
                data_dir,
                conversation_id,
                {
                    "conversation_id": conversation_id,
                    "conversation_type": "group",
                    "chat_title": "家庭群",
                    "conversation_key": "12345678@chatroom",
                    "sender_wechat_ids": ["wxid_alice_member", "wxid_bob_member"],
                    "source_names": ["backend_events_jsonl"],
                    "trusted_channel_source": True,
                },
            )
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)

            result = driver.send_message(conversation_id, "群里好")
            state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "queued_to_bridge")
            self.assertEqual(state["items"][0]["receiver"], "12345678@chatroom")

    def test_group_without_roomid_does_not_leak_to_member_wxid(self) -> None:
        # If no roomid is recoverable, the receiver must be empty (send fails
        # cleanly) rather than delivering privately to a member wxid.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = "grouphash111111111111111"
            _write_channel(
                data_dir,
                conversation_id,
                {
                    "conversation_id": conversation_id,
                    "conversation_type": "group",
                    "chat_title": "无roomid群",
                    "sender_wechat_ids": ["wxid_alice_member"],
                    "source_names": ["backend_events_jsonl"],
                    "trusted_channel_source": True,
                },
            )
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)

            driver.send_message(conversation_id, "群里好")
            state = bridge_state(data_dir, limit=10)

            self.assertEqual(state["items"][0]["receiver"], "")

    def test_send_worker_resolves_receiver_from_real_store_channel(self) -> None:
        # End-to-end cross-process regression: the channel is written by the
        # real ConversationChannelStore under a human-readable segment dir, and
        # a *fresh* bridge send (standing in for the out-of-process worker with
        # no in-memory cache) must still recover the receiver via index.json.
        from app.personal_wechat_bot.config.schema import ProviderConfig
        from app.personal_wechat_bot.conversation.channel_store import ConversationChannelStore
        from app.personal_wechat_bot.domain.models import NormalizedMessage
        from app.personal_wechat_bot.llm.key_pool import ApiKeyPool

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = ConversationChannelStore(
                data_dir,
                ApiKeyPool(ProviderConfig(api_key_env="", api_key_env_pool=["KEY_A"])),
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            conversation_id = "e2ehashedconversationid00"
            store.ensure_channel(
                NormalizedMessage(
                    message_id="m1",
                    conversation_id=conversation_id,
                    conversation_type="private",  # type: ignore[arg-type]
                    chat_title="Bob",
                    sender_name="Bob",
                    text="hi",
                    is_self=False,
                    received_at="2026-07-05T00:00:00+00:00",
                    sender_wechat_id="wxid_real_bob",
                    metadata={"source": "weflow_discovery", "trusted_channel_source": True, "is_friend": True},
                )
            )
            # Confirm the dir is NOT the raw hash id (readable naming in effect).
            self.assertFalse((data_dir / "conversation_channels" / conversation_id).exists())
            channel = store.get_channel(conversation_id)
            self.assertIsNotNone(channel)
            projection = data_dir / "conversation_channels" / channel.segment / "channel.json"
            projection.unlink()
            (data_dir / "conversation_channels" / "index.json").unlink()

            # Fresh driver instance = no shared cache or file projection.
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)
            result = driver.send_message(conversation_id, "hello from worker")
            state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "queued_to_bridge")
            self.assertEqual(state["items"][0]["receiver"], "wxid_real_bob")

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

    def test_bridge_send_file_normalizes_relative_path_to_absolute(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)
            target = root / "relative-report.txt"
            target.write_text("report", encoding="utf-8")
            relative_target = os.path.relpath(target, Path.cwd())

            result = driver.send_file("private-1", relative_target)
            state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "queued_to_bridge")
            self.assertEqual(state["items"][0]["path"], str(target.resolve()))
            self.assertEqual(state["items"][0]["name"], "relative-report.txt")

    def test_wechat_native_default_file_route_queues_before_worker_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            conversation_id = "native-file-private"
            _write_channel(
                data_dir,
                conversation_id,
                {
                    "conversation_id": conversation_id,
                    "conversation_type": "private",
                    "chat_title": "Alice",
                    "conversation_key": "wxid_file_alice",
                    "sender_wechat_ids": ["wxid_file_alice"],
                    "source_names": ["backend_events_jsonl"],
                    "trusted_channel_source": True,
                    "is_friend": True,
                    "contact_authorization": "explicit_friend",
                },
            )
            target = data_dir / "report.pdf"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"%PDF-1.4 test")
            with mock.patch(
                "app.personal_wechat_bot.wechat_driver.bridge_send.wechat_native_http_status",
                return_value={"available": True, "reason": "", "health": {"IsLogin": 1}},
            ):
                driver = BridgeOutboxSendDriver(
                    send_enabled=True,
                    data_dir=data_dir,
                    send_backend="wechat_native_http",
                )

                result = driver.send_file(conversation_id, str(target), caption="see attached")
                state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "queued_to_bridge")
            self.assertIn("queued_file_to_non_foreground_bridge", result.reason)
            self.assertEqual(state["count"], 1)
            self.assertEqual(state["items"][0]["kind"], "file")
            self.assertEqual(state["items"][0]["path"], str(target))
            self.assertEqual(state["items"][0]["receiver"], "wxid_file_alice")

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


class BridgeOutboxCompactionTest(unittest.TestCase):
    def test_append_isolates_partial_tail_and_compaction_preserves_corruption_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            partial = b'{"bridge_id":"crash-truncated"'
            store.outbox_path.write_bytes(partial)

            appended = store.enqueue("wxid_a", "after crash")
            before_compaction = store.outbox_path.read_bytes()
            state = store.state(limit=10)
            compacted = store.compact(keep_resolved=0)

            self.assertTrue(before_compaction.startswith(partial + b"\n"))
            self.assertEqual(state["count"], 1)
            self.assertEqual(state["items"][0]["bridge_id"], appended["bridge_id"])
            self.assertEqual(state["invalid_outbox_line_count"], 1)
            self.assertEqual(compacted["invalid_outbox_lines"], 1)
            self.assertEqual(compacted["removed_outbox"], 0)
            self.assertEqual(store.outbox_path.read_bytes(), before_compaction)

    def test_compact_drops_old_resolved_keeps_pending_and_recent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            # 5 resolved (sent) records + 1 still-pending record.
            resolved_ids = []
            for i in range(5):
                rec = store.enqueue("wxid_a", f"msg {i}")
                store.append_ack(rec["bridge_id"], status="sent", reason="ok")
                resolved_ids.append(rec["bridge_id"])
            pending = store.enqueue("wxid_b", "still pending")

            # keep only the 2 most recent resolved records.
            result = store.compact(
                keep_resolved=2,
                synced_ack_fingerprints=_synced_ack_fingerprints(store),
            )

            self.assertEqual(result["removed_outbox"], 3)
            state = store.state(limit=50)
            remaining = {item["bridge_id"] for item in state["items"]}
            # pending always retained
            self.assertIn(pending["bridge_id"], remaining)
            # 2 newest resolved retained, 3 oldest dropped
            self.assertIn(resolved_ids[-1], remaining)
            self.assertIn(resolved_ids[-2], remaining)
            self.assertNotIn(resolved_ids[0], remaining)
            self.assertEqual(state["pending_count"], 1)

    def test_compact_preserves_terminal_records_without_current_sync_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BridgeOutboxStore(Path(tmp) / "data")
            for index in range(3):
                record = store.enqueue("wxid_a", f"unsynced {index}")
                store.append_ack(record["bridge_id"], status=BridgeAckStatus.FAILED, reason="not projected")
            before_outbox = store.outbox_path.read_bytes()
            before_acks = store.ack_path.read_bytes()

            result = store.compact(keep_resolved=0)

            self.assertEqual(result, {"removed_outbox": 0, "removed_acks": 0})
            self.assertEqual(store.outbox_path.read_bytes(), before_outbox)
            self.assertEqual(store.ack_path.read_bytes(), before_acks)

    def test_compact_preserves_record_when_sync_fingerprint_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BridgeOutboxStore(Path(tmp) / "data")
            record = store.enqueue("wxid_a", "terminal upgraded")
            store.append_ack(record["bridge_id"], status=BridgeAckStatus.FAILED, reason="first terminal")
            stale_fingerprint = _synced_ack_fingerprints(store)[record["bridge_id"]]
            store.append_ack(record["bridge_id"], status=BridgeAckStatus.SENT, reason="verified sent")

            preserved = store.compact(
                keep_resolved=0,
                synced_ack_fingerprints={record["bridge_id"]: stale_fingerprint},
            )
            removed = store.compact(
                keep_resolved=0,
                synced_ack_fingerprints=_synced_ack_fingerprints(store),
            )

            self.assertEqual(preserved, {"removed_outbox": 0, "removed_acks": 0})
            self.assertEqual(removed, {"removed_outbox": 1, "removed_acks": 2})

    def test_compact_preserves_ack_only_terminal_upgrade_until_current_sync_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = BridgeOutboxStore(Path(tmp) / "data")
            record = store.enqueue("wxid_a", "failed then verified")
            store.append_ack(record["bridge_id"], status=BridgeAckStatus.FAILED, reason="initial failure")
            old_proof = _synced_ack_fingerprints(store)[record["bridge_id"]]
            store.compact(
                keep_resolved=0,
                synced_ack_fingerprints={record["bridge_id"]: old_proof},
            )
            store.append_ack(record["bridge_id"], status=BridgeAckStatus.SENT, reason="verified later")

            preserved = store.compact(
                keep_resolved=0,
                synced_ack_fingerprints={record["bridge_id"]: old_proof},
            )
            ack_state = effective_bridge_ack_states(store._read_all(store.ack_path))[record["bridge_id"]]
            current_proof = bridge_sync_fingerprint(ack_state.ack, None)
            removed = store.compact(
                keep_resolved=0,
                synced_ack_fingerprints={record["bridge_id"]: current_proof},
            )

            self.assertEqual(preserved, {"removed_outbox": 0, "removed_acks": 0})
            self.assertEqual(ack_state.status, BridgeAckStatus.SENT)
            self.assertEqual(removed, {"removed_outbox": 0, "removed_acks": 1})

    def test_ack_upgrade_appended_during_compaction_is_not_lost(self) -> None:
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            store = BridgeOutboxStore(Path(tmp) / "data")
            upgraded = store.enqueue("wxid_a", "late verification")
            store.append_ack(upgraded["bridge_id"], status=BridgeAckStatus.FAILED, reason="initial failure")
            old_proof = _synced_ack_fingerprints(store)[upgraded["bridge_id"]]
            store.compact(
                keep_resolved=0,
                synced_ack_fingerprints={upgraded["bridge_id"]: old_proof},
            )
            noise = store.enqueue("wxid_b", "compaction trigger")
            store.append_ack(noise["bridge_id"], status=BridgeAckStatus.SENT, reason="done")
            sync_proofs = _synced_ack_fingerprints(store)
            sync_proofs[upgraded["bridge_id"]] = old_proof
            compaction_inside_rewrite = threading.Event()
            append_started = threading.Event()
            original_rewrite = store._rewrite

            def paused_rewrite(path: Path, records: list[dict]) -> None:
                if path == store.outbox_path and not compaction_inside_rewrite.is_set():
                    compaction_inside_rewrite.set()
                    self.assertTrue(append_started.wait(2.0))
                    time.sleep(0.05)
                original_rewrite(path, records)

            def append_upgrade() -> None:
                self.assertTrue(compaction_inside_rewrite.wait(2.0))
                append_started.set()
                store.append_ack(
                    upgraded["bridge_id"],
                    status=BridgeAckStatus.SENT,
                    reason="verified while compacting",
                )

            with mock.patch.object(store, "_rewrite", side_effect=paused_rewrite):
                thread = threading.Thread(target=append_upgrade)
                thread.start()
                store.compact(keep_resolved=0, synced_ack_fingerprints=sync_proofs)
                thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            ack_state = effective_bridge_ack_states(store._read_all(store.ack_path))[upgraded["bridge_id"]]
            self.assertEqual(ack_state.status, BridgeAckStatus.SENT)

    def test_compact_treats_terminal_ack_with_stale_retry_as_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "done")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.SENT, reason="ok")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.RETRY, reason="stale_retry")

            result = store.compact(
                keep_resolved=0,
                synced_ack_fingerprints=_synced_ack_fingerprints(store),
            )

            self.assertEqual(result["removed_outbox"], 1)
            self.assertEqual(store.state(limit=10)["items"], [])

    def test_compact_keeps_complete_ancestry_for_staged_retry_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            noise = store.enqueue("wxid_a", "old noise")
            store.append_ack(noise["bridge_id"], status=BridgeAckStatus.SENT, reason="done")
            original = store.enqueue("wxid_a", "retry chain")
            safe_failure = "wechat_native_http_send_text_error:ConnectionRefusedError:refused"
            store.append_ack(original["bridge_id"], status=BridgeAckStatus.FAILED, reason=safe_failure)
            first = store.requeue_resolved(original["bridge_id"])
            store.append_ack(first["bridge_id"], status=BridgeAckStatus.FAILED, reason=safe_failure)
            staged = store.requeue_resolved(original["bridge_id"], staged=True)

            compacted = store.compact(
                keep_resolved=0,
                synced_ack_fingerprints=_synced_ack_fingerprints(store),
            )
            retained_ids = {
                str(item.get("bridge_id", ""))
                for item in store._read_all(store.outbox_path)
            }
            replay = store.requeue_resolved(original["bridge_id"], staged=True)

            self.assertGreater(compacted["removed_outbox"], 0)
            self.assertNotIn(noise["bridge_id"], retained_ids)
            self.assertEqual(
                retained_ids,
                {original["bridge_id"], first["bridge_id"], staged["bridge_id"]},
            )
            self.assertTrue(replay["_reused_existing"])
            self.assertEqual(replay["bridge_id"], staged["bridge_id"])

    def test_concurrent_append_during_compaction_is_not_lost(self) -> None:
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            # Seed enough resolved records that compaction will actually rewrite.
            for i in range(10):
                rec = store.enqueue("wxid_a", f"seed {i}")
                store.append_ack(rec["bridge_id"], status="sent", reason="ok")

            appended_ids: list[str] = []
            errors: list[Exception] = []
            barrier = threading.Barrier(2)

            def appender() -> None:
                try:
                    barrier.wait()
                    for i in range(20):
                        rec = BridgeOutboxStore(data_dir).enqueue("wxid_b", f"live {i}")
                        appended_ids.append(rec["bridge_id"])
                except Exception as exc:  # pragma: no cover - surfaced via errors
                    errors.append(exc)

            def compactor() -> None:
                try:
                    barrier.wait()
                    for _ in range(20):
                        current_store = BridgeOutboxStore(data_dir)
                        current_store.compact(
                            keep_resolved=1,
                            synced_ack_fingerprints=_synced_ack_fingerprints(current_store),
                        )
                except Exception as exc:  # pragma: no cover - surfaced via errors
                    errors.append(exc)

            threads = [threading.Thread(target=appender), threading.Thread(target=compactor)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            # Every live-appended record must still be present: the store lock
            # serializes append vs compaction's read-modify-rewrite so no append
            # is dropped inside the compaction window.
            all_ids = {
                str(rec.get("bridge_id", ""))
                for rec in BridgeOutboxStore(data_dir)._read_all(store.outbox_path)
            }
            for bridge_id in appended_ids:
                self.assertIn(bridge_id, all_ids)

    def test_compact_is_noop_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "one")
            store.append_ack(rec["bridge_id"], status="sent", reason="ok")

            result = store.compact(
                keep_resolved=500,
                synced_ack_fingerprints=_synced_ack_fingerprints(store),
            )

            self.assertEqual(result, {"removed_outbox": 0, "removed_acks": 0})

    def test_compacted_records_are_not_redelivered(self) -> None:
        # A dropped record is terminally resolved, so a fresh worker must not
        # re-send it (restart-safety survives compaction).
        from app.personal_wechat_bot.runtime.send_bridge_worker import BridgeWorker
        from app.personal_wechat_bot.wechat_driver.send_backends import DryRunSendBackend

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            for i in range(4):
                rec = store.enqueue("wxid_a", f"done {i}")
                store.append_ack(rec["bridge_id"], status="sent", reason="ok")
            store.compact(
                keep_resolved=1,
                synced_ack_fingerprints=_synced_ack_fingerprints(store),
            )

            backend = DryRunSendBackend()
            processed = BridgeWorker(data_dir, backend).run_once()

            self.assertEqual(processed, 0)
            self.assertEqual(backend.sent_texts, [])


if __name__ == "__main__":
    unittest.main()

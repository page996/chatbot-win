from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.config.loader import create_default_config
from app.personal_wechat_bot.conversation.segment import conversation_segment
from app.personal_wechat_bot.wechat_driver.bridge_send import (
    BridgeAckStatus,
    BridgeOutboxSendDriver,
    BridgeOutboxStore,
    bridge_ack,
    bridge_state,
)


def _write_channel(data_dir: Path, conversation_id: str, payload: dict) -> None:
    """Write a channel.json + index.json exactly as ConversationChannelStore does.

    Channel dirs are named by the human-readable segment (chat_title_hashPrefix)
    and the out-of-process send worker recovers that segment from index.json,
    so a faithful fixture must create both.
    """
    chat_title = str(payload.get("chat_title", "") or "")
    segment = conversation_segment(conversation_id, chat_title)
    channel_dir = data_dir / "conversation_channels" / segment
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / "channel.json").write_text(json.dumps(payload), encoding="utf-8")
    index_path = data_dir / "conversation_channels" / "index.json"
    (index_path).write_text(
        json.dumps({"channels": [{"conversation_id": conversation_id, "chat_title": chat_title}]}),
        encoding="utf-8",
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

    def test_terminal_ack_is_not_overridden_by_stale_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "already sent")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.SENT, reason="wcf_sent")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.RETRY, reason="stale_retry")

            state = bridge_state(data_dir, limit=10)

            self.assertEqual(state["pending_count"], 0)
            self.assertEqual(state["items"][0]["status"], BridgeAckStatus.SENT)
            self.assertEqual(state["items"][0]["ack"]["reason"], "wcf_sent")

    def test_sent_ack_is_not_downgraded_by_later_failed_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "wire result wins")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.SENT, reason="wcf_sent")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.FAILED, reason="stale_failed")

            state = bridge_state(data_dir, limit=10)

            self.assertEqual(state["pending_count"], 0)
            self.assertEqual(state["items"][0]["status"], BridgeAckStatus.SENT)
            self.assertEqual(state["items"][0]["ack"]["reason"], "wcf_sent")

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
                },
            )
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)

            result = driver.send_message(conversation_id, "hello bridge")
            state = bridge_state(data_dir, limit=10)

            self.assertEqual(result.status, "queued_to_bridge")
            self.assertEqual(state["items"][0]["conversation_id"], conversation_id)
            self.assertEqual(state["items"][0]["receiver"], "wxid_real_alice")

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
                    metadata={"source": "weflow_discovery", "trusted_channel_source": True},
                )
            )
            # Confirm the dir is NOT the raw hash id (readable naming in effect).
            self.assertFalse((data_dir / "conversation_channels" / conversation_id).exists())

            # Fresh driver instance = no shared cache with the store.
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
            result = store.compact(keep_resolved=2)

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

    def test_compact_treats_terminal_ack_with_stale_retry_as_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "done")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.SENT, reason="ok")
            store.append_ack(rec["bridge_id"], status=BridgeAckStatus.RETRY, reason="stale_retry")

            result = store.compact(keep_resolved=0)

            self.assertEqual(result["removed_outbox"], 1)
            self.assertEqual(store.state(limit=10)["items"], [])

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
                        BridgeOutboxStore(data_dir).compact(keep_resolved=1)
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

            result = store.compact(keep_resolved=500)

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
            store.compact(keep_resolved=1)

            backend = DryRunSendBackend()
            processed = BridgeWorker(data_dir, backend).run_once()

            self.assertEqual(processed, 0)
            self.assertEqual(backend.sent_texts, [])


if __name__ == "__main__":
    unittest.main()

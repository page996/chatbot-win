from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.config.loader import create_default_config, load_config
from app.personal_wechat_bot.control import sidebar_api
from app.personal_wechat_bot.control.sidebar_api import (
    ack_sidebar_bridge_item,
    append_sidebar_backend_event,
    build_sidebar_bridge_state,
    build_sidebar_runtime_cards,
    build_sidebar_state,
    cleanup_sidebar_channels,
    delete_sidebar_channel,
    sidebar_queue_action,
    sidebar_runtime_card_action,
    update_sidebar_controls,
)
from app.personal_wechat_bot.conversation.channel_store import ConversationChannelStore
from app.personal_wechat_bot.domain.models import RawWeChatMessage
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool
from app.personal_wechat_bot.domain.models import ReplyCandidate
from app.personal_wechat_bot.normalizer.normalizer import MessageNormalizer
from app.personal_wechat_bot.router.deduper import Deduper
from app.personal_wechat_bot.router.router import Router
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue


class SidebarApiTest(unittest.TestCase):
    def test_sidebar_state_contains_controls_queues_readiness_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            ConfirmQueue(data_dir / "confirm_queue.jsonl").enqueue(_reply())

            state = build_sidebar_state(data_dir)

            self.assertEqual(state["status"], "ok")
            self.assertIn("config", state)
            self.assertEqual(state["queues"]["pending"]["count"], 1)
            self.assertIn("readiness", state)
            self.assertIn("driver_probe", state)
            self.assertIn("audit", state)
            self.assertIn("send_bridge", state)
            self.assertIn("runtime_cards", state)
            self.assertIn("queued_to_bridge", state["queues"])
            self.assertEqual(state["send_bridge"]["manual_bound_count"], 0)
            self.assertEqual(state["capture"]["background_send_status"], "bridge_outbox_manual_capture_only_available")
            self.assertIn("skill.file_workspace_agent", [item["card_id"] for item in state["runtime_cards"]["active"]["skills"]])

    def test_sidebar_channel_state_hides_probe_fragments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            key_pool = ApiKeyPool(config.providers.get("chat", config.llm), data_dir)
            store = ConversationChannelStore(
                data_dir,
                key_pool,
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            normalizer = MessageNormalizer()
            visible = normalizer.normalize(RawWeChatMessage("1", "PAGE", "PAGE", "hello", driver_meta={"source": "backend_events_jsonl"}))
            noisy = normalizer.normalize(RawWeChatMessage("2", "+25", "+25", "8/10/16", driver_meta={"source": "backend_events_jsonl"}))
            assert visible is not None and noisy is not None
            store.ensure_channel(visible)
            store.ensure_channel(noisy)

            state = build_sidebar_state(data_dir)

            self.assertEqual(state["channels"]["count"], 1)
            self.assertEqual(state["channels"]["hidden_count"], 1)
            self.assertEqual(state["channels"]["items"][0]["chat_title"], "PAGE")

    def test_router_does_not_register_windows_snapshot_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            key_pool = ApiKeyPool(config.providers.get("chat", config.llm), data_dir)
            store = ConversationChannelStore(
                data_dir,
                key_pool,
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            router = Router(config, Deduper(data_dir / "dedupe.sqlite"), channel_store=store)
            message = MessageNormalizer().normalize(
                RawWeChatMessage("snapshot-1", "+25", "+25", "8/10/16", driver_meta={"source": "windows_snapshot"})
            )
            assert message is not None

            decision = router.decide(message)

            self.assertEqual(decision.action, "process")
            self.assertEqual(store.list_channels(), [])

    def test_sidebar_hides_untrusted_legacy_channels_but_keeps_trusted_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            key_pool = ApiKeyPool(config.providers.get("chat", config.llm), data_dir)
            store = ConversationChannelStore(
                data_dir,
                key_pool,
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            trusted = MessageNormalizer().normalize(
                RawWeChatMessage("1", "PAGE", "PAGE", "hello", driver_meta={"source": "backend_events_jsonl"})
            )
            stale = MessageNormalizer().normalize(
                RawWeChatMessage("2", "PTURE", "PTURE", "fragment", driver_meta={"allow_empty_message": True})
            )
            assert trusted is not None and stale is not None
            store.ensure_channel(trusted)
            # Simulate a pre-provenance channel left by old snapshot/OCR ingestion.
            stale_path = data_dir / "conversation_channels" / stale.conversation_id / "channel.json"
            stale_path.parent.mkdir(parents=True, exist_ok=True)
            stale_path.write_text(
                """
{
  "conversation_id": "2ce59ad9ab7cc4bdfe59a871",
  "conversation_type": "private",
  "chat_title": "PTURE",
  "status": "active",
  "key_slots": 1,
  "api_key_refs": [],
  "session_scope": "per_conversation_current_session",
  "backend_dir": "",
  "context_dir": "",
  "file_workspace_dir": "",
  "sender_names": ["PTURE"],
  "sender_wechat_ids": [],
  "created_at": "2026-06-30T00:00:00+00:00",
  "updated_at": "2026-06-30T00:00:00+00:00"
}
""".strip(),
                encoding="utf-8",
            )

            state = build_sidebar_state(data_dir)

            self.assertEqual([item["chat_title"] for item in state["channels"]["items"]], ["PAGE"])
            self.assertEqual(state["channels"]["hidden_reasons"]["untrusted_legacy_channel"], 1)

    def test_sidebar_cleanup_hidden_channels_deletes_only_hidden_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            key_pool = ApiKeyPool(config.providers.get("chat", config.llm), data_dir)
            store = ConversationChannelStore(
                data_dir,
                key_pool,
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            trusted = MessageNormalizer().normalize(
                RawWeChatMessage("1", "PAGE", "PAGE", "hello", driver_meta={"source": "backend_events_jsonl"})
            )
            hidden = MessageNormalizer().normalize(
                RawWeChatMessage("2", "+25", "+25", "8/10/16", driver_meta={"source": "backend_events_jsonl"})
            )
            assert trusted is not None and hidden is not None
            store.ensure_channel(trusted)
            store.ensure_channel(hidden)
            ledger_file = data_dir / "conversation_ledgers" / hidden.conversation_id / "conversation.md"
            ledger_file.parent.mkdir(parents=True, exist_ok=True)
            ledger_file.write_text("keep", encoding="utf-8")

            result = cleanup_sidebar_channels(data_dir)
            state = build_sidebar_state(data_dir)

            self.assertEqual(result["deleted_conversation_ids"], [hidden.conversation_id])
            self.assertEqual(result["cleanups"][0]["cleanup_policy"], "wechat_preserve")
            self.assertEqual(state["channels"]["count"], 1)
            self.assertEqual(state["channels"]["hidden_count"], 0)
            self.assertIsNotNone(store.get_channel(trusted.conversation_id))
            self.assertIsNone(store.get_channel(hidden.conversation_id))
            self.assertTrue(ledger_file.exists())

    def test_sidebar_delete_untrusted_channel_fully_purges_associated_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = "legacy-noise"
            channel_file = data_dir / "conversation_channels" / conversation_id / "channel.json"
            ledger_dir = data_dir / "conversation_ledgers" / conversation_id
            workspace_dir = data_dir / "file_workspace" / conversation_id
            session_dir = data_dir / "conversation_sessions" / conversation_id
            channel_file.parent.mkdir(parents=True, exist_ok=True)
            ledger_dir.mkdir(parents=True, exist_ok=True)
            workspace_dir.mkdir(parents=True, exist_ok=True)
            session_dir.mkdir(parents=True, exist_ok=True)
            channel_file.write_text(
                """
{
  "conversation_id": "legacy-noise",
  "conversation_type": "private",
  "chat_title": "NOISE",
  "status": "active",
  "key_slots": 1,
  "api_key_refs": [],
  "session_scope": "per_conversation_current_session",
  "sender_names": ["NOISE"],
  "sender_wechat_ids": [],
  "source_names": [],
  "trusted_channel_source": false
}
""".strip(),
                encoding="utf-8",
            )
            (ledger_dir / "conversation.md").write_text("ledger", encoding="utf-8")
            (workspace_dir / "file.txt").write_text("file", encoding="utf-8")
            (session_dir / "state.json").write_text("session", encoding="utf-8")

            result = delete_sidebar_channel(data_dir, conversation_id)

            self.assertEqual(result["deleted_count"], 1)
            self.assertEqual(result["cleanup"]["cleanup_policy"], "non_wechat_purge")
            self.assertFalse(channel_file.parent.exists())
            self.assertFalse(ledger_dir.exists())
            self.assertFalse(workspace_dir.exists())
            self.assertFalse(session_dir.exists())

    def test_sidebar_delete_channel_removes_specific_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            store = ConversationChannelStore(
                data_dir,
                ApiKeyPool(config.providers.get("chat", config.llm), data_dir),
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            message = MessageNormalizer().normalize(
                RawWeChatMessage("1", "PAGE", "PAGE", "hello", driver_meta={"source": "backend_events_jsonl"})
            )
            assert message is not None
            store.ensure_channel(message)

            result = delete_sidebar_channel(data_dir, message.conversation_id)

            self.assertEqual(result["deleted_count"], 1)
            self.assertIsNone(store.get_channel(message.conversation_id))

    def test_backend_event_ingest_rejects_event_file_outside_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            outside = Path(tmp) / "outside.jsonl"
            create_default_config(data_dir)

            with self.assertRaises(ValueError):
                append_sidebar_backend_event(
                    data_dir,
                    {
                        "event_file": str(outside),
                        "chat_title": "PAGE",
                        "sender_name": "PAGE",
                        "text": "hello",
                    },
                )

    def test_sidebar_controls_update_mode_and_send_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            result = update_sidebar_controls(
                data_dir,
                {"mode": "confirm", "send_enabled": True, "send_driver": "windows_guarded"},
            )
            config = load_config(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(config.mode, "confirm")
            self.assertTrue(config.send_enabled)
            self.assertEqual(config.send_driver, "windows_guarded")

    def test_sidebar_queue_action_approves_and_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply())

            approved = sidebar_queue_action(data_dir, "approve", queue_id, {"reviewer": "test"})

            self.assertEqual(approved["item"]["status"], "approved")

    def test_sidebar_bridge_state_and_ack_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            ack = ack_sidebar_bridge_item(
                data_dir,
                {"bridge_id": "bridge:test", "status": "sent", "reason": "manual"},
            )
            state = build_sidebar_bridge_state(data_dir)

            self.assertEqual(ack["status"], "ok")
            self.assertEqual(state["status"], "ok")
            self.assertEqual(state["ack_count"], 1)

    def test_sidebar_runtime_cards_actions_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            saved = sidebar_runtime_card_action(
                data_dir,
                "save-task",
                {"name": "测试任务卡", "content": "持续生效的任务约束"},
            )
            state = build_sidebar_runtime_cards(data_dir)

            self.assertEqual(saved["status"], "ok")
            self.assertIn("持续生效的任务约束", "\n".join(item["content"] for item in state["active"]["tasks"]))


class WeflowBackgroundLoopTest(unittest.TestCase):
    def test_background_loop_reuses_one_context_across_ticks(self) -> None:
        """The puller must be built once and reused so the backend driver keeps
        its dedup state instead of re-reading the whole backend log each tick."""

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            build_calls = 0
            tick_calls = 0

            def fake_build(root, payload):
                nonlocal build_calls
                build_calls += 1
                return {"context_id": build_calls}

            def fake_tick(context):
                nonlocal tick_calls
                tick_calls += 1
                return {"status": "ok", "source": {"status": "ok"}, "context_id": context["context_id"]}

            stop_event = threading.Event()

            def stop_after_two_ticks(_seconds):
                if tick_calls >= 2:
                    stop_event.set()

            with mock.patch.object(sidebar_api, "_build_weflow_pull_context", side_effect=fake_build), mock.patch.object(
                sidebar_api, "_run_weflow_pull_tick", side_effect=fake_tick
            ), mock.patch.object(stop_event, "wait", side_effect=stop_after_two_ticks):
                sidebar_api._weflow_background_loop(data_dir, {"interval_seconds": 1}, stop_event)

            self.assertGreaterEqual(tick_calls, 2)
            self.assertEqual(build_calls, 1)

    def test_background_loop_rebuilds_context_after_total_pull_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            build_calls = 0
            tick_calls = 0

            def fake_build(root, payload):
                nonlocal build_calls
                build_calls += 1
                return {"context_id": build_calls}

            def fake_tick(context):
                nonlocal tick_calls
                tick_calls += 1
                # First tick reports a total pull failure (WeFlow went away),
                # so the loop must rebuild before the second tick.
                status = "error" if tick_calls == 1 else "ok"
                return {"status": "partial_error", "source": {"status": status}}

            stop_event = threading.Event()

            def stop_after_two_ticks(_seconds):
                if tick_calls >= 2:
                    stop_event.set()

            with mock.patch.object(sidebar_api, "_build_weflow_pull_context", side_effect=fake_build), mock.patch.object(
                sidebar_api, "_run_weflow_pull_tick", side_effect=fake_tick
            ), mock.patch.object(stop_event, "wait", side_effect=stop_after_two_ticks):
                sidebar_api._weflow_background_loop(data_dir, {"interval_seconds": 1}, stop_event)

            self.assertEqual(build_calls, 2)

    def test_background_loop_fails_fast_when_consumer_lock_held(self) -> None:
        from app.personal_wechat_bot.runtime.process_lock import ProcessLock

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)
            # Simulate another consumer already holding the lock.
            other = ProcessLock(data_dir / "hook_events_state.json.consumer.lock", label="other-consumer")
            other.acquire()
            build_calls = 0

            def fake_build(root, payload):
                nonlocal build_calls
                build_calls += 1
                return {"context_id": build_calls}

            stop_event = threading.Event()
            try:
                with mock.patch.object(sidebar_api, "_build_weflow_pull_context", side_effect=fake_build):
                    sidebar_api._weflow_background_loop(data_dir, {"interval_seconds": 1}, stop_event)
            finally:
                other.release()

            # The loop must refuse to run (no context built) and record the error.
            self.assertEqual(build_calls, 0)
            state = sidebar_api.build_sidebar_weflow_state(data_dir)
            self.assertIn("already running", state["worker"]["last_error"])


def _reply() -> ReplyCandidate:
    return ReplyCandidate(
        message_id="message-1",
        conversation_id="private-1",
        text="hello",
        send_mode="confirm",
        model="fake",
    )


if __name__ == "__main__":
    unittest.main()

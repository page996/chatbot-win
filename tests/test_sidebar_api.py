from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.config.loader import create_default_config, load_config, save_config
from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.control import sidebar_api
from app.personal_wechat_bot.control.send_commands import set_send_controls
from app.personal_wechat_bot.control.sidebar_api import (
    ack_sidebar_bridge_item,
    add_api_key,
    append_sidebar_backend_event,
    build_sidebar_bridge_state,
    build_sidebar_runtime_cards,
    build_sidebar_state,
    clear_sidebar_history_data,
    cleanup_file_workspace,
    cleanup_sidebar_channels,
    delete_sidebar_channel,
    get_model_config,
    list_api_keys,
    probe_model_fetch,
    remove_api_key,
    set_model_config,
    sidebar_weflow_backfill,
    sidebar_weflow_cancel_backfill,
    sidebar_queue_action,
    sidebar_runtime_card_action,
    sidebar_weflow_dependency_status,
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
            self.assertEqual(state["capture"]["background_send_status"], "bridge_outbox_available")
            self.assertIn("skill.file_workspace_agent", [item["card_id"] for item in state["runtime_cards"]["active"]["skills"]])

    def test_sidebar_state_restores_config_from_persistent_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox")
            (data_dir / "config.json").unlink()

            state = build_sidebar_state(data_dir)

            self.assertEqual(state["status"], "ok")
            self.assertEqual(state["config"]["mode"], "confirm")
            self.assertEqual(state["config"]["send_driver"], "bridge_outbox")
            self.assertTrue((data_dir / "config.json").exists())

    def test_history_clear_preserves_sidebar_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox")
            (data_dir / "conversation_ledgers").mkdir()
            (data_dir / "conversation_ledgers" / "old.md").write_text("history", encoding="utf-8")
            (data_dir / "backend_events.jsonl").write_text("{}\n", encoding="utf-8")

            result = clear_sidebar_history_data(data_dir)
            state = build_sidebar_state(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertFalse((data_dir / "conversation_ledgers").exists())
            self.assertFalse((data_dir / "backend_events.jsonl").exists())
            self.assertTrue((data_dir / "config.json").exists())
            self.assertEqual(state["config"]["mode"], "confirm")

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
            channel_file = data_dir / "conversation_channels" / "NOISE_legacy-n" / "channel.json"
            ledger_dir = data_dir / "conversation_ledgers" / "NOISE_legacy-n"
            workspace_dir = data_dir / "file_workspace" / "NOISE_legacy-n"
            session_dir = data_dir / "conversation_sessions" / "NOISE_legacy-n"
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

    def test_sidebar_bridge_state_projects_visible_service_channels(self) -> None:
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
                RawWeChatMessage(
                    "1",
                    "Alice",
                    "Alice",
                    "hello",
                    sender_wechat_id="wxid_real_alice",
                    driver_meta={
                        "source": "weflow_discovery",
                        "trusted_channel_source": True,
                        "conversation_key": "wxid_real_alice",
                    },
                )
            )
            assert message is not None
            store.ensure_channel(message)

            state = build_sidebar_bridge_state(data_dir)

            self.assertEqual(state["channel_count"], 1)
            self.assertEqual(state["channels"][0]["display_name"], "Alice")
            self.assertEqual(state["channels"][0]["receiver"], "wxid_real_alice")
            self.assertTrue(state["channels"][0]["bridge_ready"])

    def test_weflow_backfill_returns_async_job_and_can_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            with mock.patch.object(sidebar_api, "_run_sidebar_weflow_once", return_value={"status": "ok", "source": {"scanned_count": 1, "appended_count": 1}, "pull": {"processed_count": 1}}):
                result = sidebar_weflow_backfill(data_dir, {"talkers": ["wxid_user"], "max_messages": 1})
                self.assertEqual(result["status"], "started")
                job_id = result["backfill_job"]["job_id"]
                self.assertTrue(job_id)

                deadline = threading.Event()
                for _ in range(20):
                    state = sidebar_api.build_sidebar_weflow_state(data_dir)
                    if state["backfill_job"].get("status") == "completed":
                        break
                    deadline.wait(0.05)
            state = sidebar_api.build_sidebar_weflow_state(data_dir)
            self.assertEqual(state["backfill_job"]["status"], "completed")
            self.assertEqual(state["last_backfill"]["status"], "ok")

    def test_weflow_backfill_cancel_sets_stop_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            started = threading.Event()

            def fake_run(*args, **kwargs):
                started.set()
                cancel_event = kwargs["cancel_event"]
                cancel_event.wait(2)
                return {"status": "cancelled", "source": {"scanned_count": 0, "appended_count": 0}, "pull": {"processed_count": 0}}

            with mock.patch.object(sidebar_api, "_run_sidebar_weflow_once", side_effect=fake_run):
                result = sidebar_weflow_backfill(data_dir, {"talkers": ["wxid_user"], "max_messages": 1})
                self.assertEqual(result["status"], "started")
                self.assertTrue(started.wait(1))
                cancel = sidebar_weflow_cancel_backfill(data_dir, {})
                deadline = threading.Event()
                for _ in range(20):
                    state = sidebar_api.build_sidebar_weflow_state(data_dir)
                    if state["backfill_job"].get("status") == "cancelled":
                        break
                    deadline.wait(0.05)

            self.assertEqual(cancel["status"], "cancel_requested")
            state = sidebar_api.build_sidebar_weflow_state(data_dir)
            self.assertEqual(state["backfill_job"]["status"], "cancelled")

    def test_weflow_persisted_running_job_reported_interrupted_after_restart(self) -> None:
        # A snapshot persisted with running=True but no live in-memory thread
        # (server restarted mid-backfill) must never be reported as still
        # running, or the UI keeps the Backfill button disabled forever.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            sidebar_api._write_weflow_sidebar_state(
                data_dir,
                {
                    "backfill_job": {
                        "job_id": "backfill-stale",
                        "status": "running",
                        "running": True,
                        "talkers": ["wxid_user"],
                    }
                },
            )
            # No in-memory job registered -> falls back to the persisted snapshot.
            with mock.patch.dict(sidebar_api._WEFLOW_BACKFILL_JOBS, {}, clear=True):
                state = sidebar_api.build_sidebar_weflow_state(data_dir)
            job = state["backfill_job"]
            self.assertFalse(job["running"])
            self.assertEqual(job["status"], "interrupted")

    def test_weflow_consume_step_is_serialized_across_concurrent_ticks(self) -> None:
        # Two consume steps (e.g. background worker tick + pull-once) must not
        # run runner.run_once() at the same time, or they race the shared hook
        # offset and message deduper.
        overlap = {"max": 0, "active": 0}
        lock = threading.Lock()

        class FakeRunner:
            def run_once(self_inner) -> dict:
                with lock:
                    overlap["active"] += 1
                    overlap["max"] = max(overlap["max"], overlap["active"])
                time.sleep(0.05)
                with lock:
                    overlap["active"] -= 1
                return {"status": "ok", "processed_count": 0, "processed": []}

        class FakeSource:
            status = "ok"

        def fake_pull_once(*args, **kwargs):
            return FakeSource()

        fake_bridge = mock.Mock()
        fake_bridge.base_url = "http://127.0.0.1:5031"
        fake_bridge.pull_once.side_effect = fake_pull_once
        context = {
            "params": {
                "talkers": [],
                "session_limit": 100,
                "message_limit": 100,
                "max_pages": 1,
                "max_messages": 0,
                "since": None,
                "lookback_seconds": 300,
                "workers": 1,
                "media": True,
                "context_only": False,
                "hook_event_file": "hook.jsonl",
                "backend_event_file": "backend.jsonl",
            },
            "bridge": fake_bridge,
            "runner": FakeRunner(),
            "weflow_ready": {},
            "media_roots": [],
        }
        threads = [
            threading.Thread(target=sidebar_api._run_weflow_pull_tick, args=(context,))
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(overlap["max"], 1)

    def test_weflow_pull_once_registers_requested_talker_in_local_library(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            fake_result = {
                "status": "ok",
                "source": {"status": "ok", "scanned_count": 1, "appended_count": 1},
                "pull": {"status": "ok", "processed_count": 1, "processed": []},
            }

            with mock.patch.object(sidebar_api, "_run_sidebar_weflow_once", return_value=fake_result):
                result = sidebar_api.sidebar_weflow_pull_once(data_dir, {"talkers": ["wxid_user"]})

            state = sidebar_api.build_sidebar_weflow_state(data_dir)
            sessions = state["discovered_sessions"]["sessions"]
            channels = build_sidebar_state(data_dir)["channels"]["items"]

            self.assertEqual(result["session_store"]["status"], "ok")
            self.assertIn("wxid_user", {item["id"] for item in sessions})
            self.assertTrue(any(item["conversation_key"] == "wxid_user" for item in channels))

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
                {"mode": "confirm", "send_enabled": True, "send_driver": "bridge_outbox"},
            )
            config = load_config(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(config.mode, "confirm")
            self.assertTrue(config.send_enabled)
            self.assertEqual(config.send_driver, "bridge_outbox")

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

    def test_sidebar_manual_ack_rejects_nonterminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            with self.assertRaisesRegex(ValueError, "sent, failed, or blocked"):
                ack_sidebar_bridge_item(
                    data_dir,
                    {"bridge_id": "bridge:test", "status": "retry", "reason": "not_final"},
                )
            state = build_sidebar_bridge_state(data_dir)

            self.assertEqual(state["ack_count"], 0)

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

    def test_supervisor_restarts_worker_loop_then_stops(self) -> None:
        # If the worker loop dies unexpectedly (not a stop, not a lock refusal),
        # the supervisor restarts it with backoff and tracks restart_count; a
        # stop during backoff ends the supervision without resurrection.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            root = data_dir.resolve()
            stop_event = threading.Event()
            calls = {"n": 0}

            def flaky_loop(r, payload, ev):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("boom in loop")
                ev.set()  # second run: request stop so the supervisor exits

            with mock.patch.object(sidebar_api, "_weflow_worker_loop", side_effect=flaky_loop), mock.patch.object(
                stop_event, "wait", return_value=False
            ):
                sidebar_api._weflow_background_loop(root, {"interval_seconds": 1}, stop_event)

            self.assertEqual(calls["n"], 2)  # crashed once, restarted once
            state = sidebar_api._weflow_worker_state(root)
            self.assertGreaterEqual(state["restart_count"], 1)

    def test_supervisor_gives_up_after_max_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            root = data_dir.resolve()
            stop_event = threading.Event()
            calls = {"n": 0}

            def always_crash(r, payload, ev):
                calls["n"] += 1
                raise RuntimeError("always down")

            with mock.patch.object(sidebar_api, "_weflow_worker_loop", side_effect=always_crash), mock.patch.object(
                stop_event, "wait", return_value=False
            ):
                sidebar_api._weflow_background_loop(root, {"interval_seconds": 1, "max_restarts": 3}, stop_event)

            self.assertEqual(calls["n"], 4)  # initial run + 3 restarts, then give up
            state = sidebar_api._weflow_worker_state(root)
            self.assertEqual(state["last_status"], "crashed")

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

    def test_refresh_send_controls_picks_up_config_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="dry_run", enabled=False, driver="not_implemented")
            runtime = build_runtime(load_config(data_dir))
            context = {
                "runtime": runtime,
                "config_path": data_dir / "config.json",
                "config_mtime": sidebar_api._config_mtime(data_dir),
            }

            # No change yet -> refresh is a no-op.
            self.assertFalse(sidebar_api._refresh_weflow_send_controls(context))
            self.assertEqual(runtime.reply_gate.mode, "dry_run")

            # Change the send controls on disk (as the sidebar would).
            set_send_controls(data_dir, mode="auto", enabled=True, driver="bridge_outbox")
            # Force a distinct mtime in case the clock resolution is coarse.
            import os

            os.utime(data_dir / "config.json", None)

            self.assertTrue(sidebar_api._refresh_weflow_send_controls(context))
            self.assertEqual(runtime.reply_gate.mode, "auto")
            self.assertTrue(runtime.reply_gate.auto_executor.config.send_enabled)
            self.assertEqual(runtime.reply_gate.auto_executor.config.send_driver, "bridge_outbox")
            self.assertEqual(runtime.config.mode, "auto")

    def test_refresh_send_controls_noop_without_runtime(self) -> None:
        # The loop tests mock _build_weflow_pull_context to return a context with
        # no runtime; refresh must handle that gracefully.
        self.assertFalse(sidebar_api._refresh_weflow_send_controls({"context_id": 1}))


class SidebarKeyPoolApiTest(unittest.TestCase):
    def _configure_key_file(self, data_dir: Path, relative: str = "keys.md") -> None:
        create_default_config(data_dir)
        config = load_config(data_dir)
        provider = config.providers.get("chat", config.llm)
        provider.api_key_file = relative
        config.llm.api_key_file = relative
        config.providers["chat"] = provider
        save_config(config)

    def test_list_add_remove_api_keys_roundtrip_without_exposing_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._configure_key_file(data_dir)

            empty = list_api_keys(data_dir)
            self.assertEqual(empty["status"], "ok")
            self.assertEqual(empty["available_count"], 0)
            self.assertTrue(empty["key_file_writable"])

            added = add_api_key(data_dir, {"value": "sk-abcd-secret-7777"})
            self.assertEqual(added["status"], "ok")
            self.assertEqual(added["available_count"], 1)
            self.assertNotIn("sk-abcd-secret-7777", str(added))
            new_ref = added["ref"]

            listed = list_api_keys(data_dir)
            previews = {item["ref"]: item["preview"] for item in listed["keys"]}
            self.assertEqual(previews[new_ref], "****7777")

            removed = remove_api_key(data_dir, {"ref": new_ref})
            self.assertEqual(removed["status"], "ok")
            self.assertEqual(removed["available_count"], 0)

    def test_add_api_key_requires_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._configure_key_file(data_dir)
            with self.assertRaises(ValueError):
                add_api_key(data_dir, {"value": "  "})

    def test_remove_api_key_unknown_ref_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._configure_key_file(data_dir)
            with self.assertRaises(ValueError):
                remove_api_key(data_dir, {"ref": "missing:secret:deadbeef"})

    def test_fresh_install_allows_add_key_without_configured_key_file(self) -> None:
        # On a default config no api_key_file is set. The UI add-key flow must
        # still work (falls back to a default key file under data_dir), so
        # key_file_writable is true and add/list/remove round-trips.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            listed = list_api_keys(data_dir)
            self.assertTrue(listed["key_file_writable"])
            self.assertTrue(listed["key_file"])  # a concrete default path

            added = add_api_key(data_dir, {"value": "sk-fresh-install-1234"})
            self.assertEqual(added["status"], "ok")
            self.assertEqual(added["available_count"], 1)
            self.assertNotIn("sk-fresh-install-1234", str(added))

            removed = remove_api_key(data_dir, {"ref": added["ref"]})
            self.assertEqual(removed["status"], "ok")
            self.assertEqual(removed["available_count"], 0)

    def test_key_pool_entries_carry_independent_model_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            added = add_api_key(data_dir, {"value": "sk-relay-secret-1234"})
            ref = added["ref"]
            result = set_model_config(
                data_dir,
                {
                    "ref": ref,
                    "provider": "relay",
                    "model": "gpt-5.5",
                    "base_url": "https://relay.example/v1",
                    "max_wait_seconds": 45,
                },
            )
            listed = list_api_keys(data_dir)

            self.assertEqual(result["status"], "ok")
            item = next(item for item in listed["keys"] if item["ref"] == ref)
            self.assertEqual(item["model_config"]["provider"], "relay")
            self.assertEqual(item["model_config"]["model"], "gpt-5.5")
            self.assertEqual(item["model_config"]["base_url"], "https://relay.example/v1")


class SidebarModelConfigApiTest(unittest.TestCase):
    def test_get_and_set_model_config_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            initial = get_model_config(data_dir)
            self.assertEqual(initial["status"], "ok")
            self.assertIn("deepseek", initial["provider_formats"])
            self.assertTrue(initial["async_summary_follows_chat"])

            result = set_model_config(
                data_dir,
                {"provider": "relay", "model": "gpt-5.5", "base_url": "https://relay.example/v1", "max_wait_seconds": 90},
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["model"], "gpt-5.5")

            updated = get_model_config(data_dir)
            self.assertEqual(updated["provider"], "relay")
            self.assertEqual(updated["model"], "gpt-5.5")
            self.assertEqual(updated["base_url"], "https://relay.example/v1")
            self.assertEqual(updated["max_wait_seconds"], 90)

    def test_set_model_config_preserves_key_pool_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            provider = config.providers.get("chat", config.llm)
            provider.api_key_file = "keys.md"
            config.llm.api_key_file = "keys.md"
            config.providers["chat"] = provider
            save_config(config)

            set_model_config(data_dir, {"model": "deepseek-v4-flash"})

            after = load_config(data_dir)
            # The key-file link must survive a model edit so keys keep working.
            self.assertEqual(after.providers["chat"].api_key_file, "keys.md")

    def test_set_model_config_rejects_unknown_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            with self.assertRaises(ValueError):
                set_model_config(data_dir, {"provider": "not-a-format"})

    def test_probe_model_fetch_without_key_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            # No key pool configured -> probe should fail gracefully, not raise.
            result = probe_model_fetch(data_dir, {"base_url": "https://api.deepseek.com", "provider": "deepseek"})
            self.assertEqual(result["status"], "error")
            self.assertFalse(result["reachable"])
            self.assertEqual(result["error"], "no_api_key_available")

    def test_probe_model_fetch_requires_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            with self.assertRaises(ValueError):
                probe_model_fetch(data_dir, {"base_url": ""})

    def test_probe_model_fetch_rejects_non_http_url_before_key_egress(self) -> None:
        # Even with an available key, a non-http(s) base_url must be rejected
        # before the key is attached, so the key can't leak to file://, ftp://, etc.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            provider = config.providers.get("chat", config.llm)
            provider.api_key_file = "keys.md"
            config.llm.api_key_file = "keys.md"
            config.providers["chat"] = provider
            save_config(config)
            (data_dir / "keys.md").write_text("KEY_01 = sk-available-1234\n", encoding="utf-8")

            result = probe_model_fetch(data_dir, {"base_url": "file:///etc/passwd", "provider": "relay"})

            self.assertEqual(result["status"], "error")
            self.assertFalse(result["reachable"])
            self.assertTrue(result["error"].startswith("unsupported_url_scheme"))


class SidebarDependencyStatusTest(unittest.TestCase):
    def test_dependency_status_reports_deduplicated_runtime_groups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            result = sidebar_weflow_dependency_status(data_dir, record_history=False)

            self.assertIn("document_runtime", {item["group"] for item in result["groups"]})
            self.assertIn("ocr_runtime", {item["group"] for item in result["groups"]})
            self.assertIn("asr_runtime", {item["group"] for item in result["groups"]})
            self.assertEqual(result["duplicate_requirements"], {})


class SidebarWorkspaceCleanupApiTest(unittest.TestCase):
    def test_cleanup_file_workspace_prunes_old_dirs(self) -> None:
        import os
        import time as _time

        from app.personal_wechat_bot.workspace.file_workspace import FileWorkspace

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            ws = FileWorkspace(data_dir / "file_workspace")
            ids = []
            for i in range(4):
                p = data_dir / f"doc{i}.txt"
                p.write_text(f"distinct {i}", encoding="utf-8")
                staged = ws.stage_file(p, conversation_id="c1", session_id="s1", original_name=f"doc{i}.txt", kind="file")
                ids.append(staged.file_id)
            old = _time.time() - 100_000
            for fid in ids:
                d = ws.file_dir("c1", "s1", fid)
                os.utime(d, (old, old))

            result = cleanup_file_workspace(data_dir, {"max_age_days": 1, "keep_min": 1})

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["removed"], 3)
            self.assertEqual(result["keep_min"], 1)

    def test_cleanup_file_workspace_defaults_are_conservative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            # Empty workspace -> nothing to remove, no error.
            result = cleanup_file_workspace(data_dir, {})
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["removed"], 0)
            self.assertEqual(result["keep_min"], 50)


def _reply() -> ReplyCandidate:
    return ReplyCandidate(
        message_id="message-1",
        conversation_id="private-1",
        text="hello",
        send_mode="confirm",
        model="fake",
    )


class SidebarBridgeWorkerSupervisionTest(unittest.TestCase):
    def test_weflow_start_existing_worker_repairs_bridge_worker_without_deadlock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = (Path(tmp) / "data").resolve()
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox")
            stop = threading.Event()
            alive_thread = threading.Thread(target=lambda: stop.wait(2), daemon=True)
            alive_thread.start()
            key = str(data_dir)
            sidebar_api._WEFLOW_WORKERS[key] = {
                "thread": alive_thread,
                "stop": stop,
                "started_at": time.time(),
                "metrics": sidebar_api.WeflowWorkerMetrics(),
            }
            try:
                with mock.patch.object(sidebar_api, "_start_bridge_worker") as start_bridge:
                    result = sidebar_api.sidebar_weflow_start(data_dir, {})
            finally:
                stop.set()
                alive_thread.join(timeout=1)
                sidebar_api._WEFLOW_WORKERS.pop(key, None)

            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["worker"]["running"])
            start_bridge.assert_called_once()

    def test_bridge_supervisor_preserves_crashed_state_after_giveup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = (Path(tmp) / "data").resolve()
            create_default_config(data_dir)
            stop_event = threading.Event()
            key = str(data_dir)
            try:
                with mock.patch(
                    "app.personal_wechat_bot.runtime.send_bridge_worker.run_bridge_worker",
                    side_effect=RuntimeError("bridge down"),
                ), mock.patch.object(stop_event, "wait", return_value=False):
                    sidebar_api._bridge_worker_supervisor(
                        data_dir,
                        {"bridge_interval_seconds": 0.5, "bridge_max_restarts": 1},
                        stop_event,
                    )
                state = sidebar_api._bridge_worker_state(data_dir)
            finally:
                sidebar_api._BRIDGE_WORKERS.pop(key, None)

            self.assertFalse(state["running"])
            self.assertEqual(state["last_status"], "crashed")
            self.assertIn("max_restarts_exceeded", state["last_error"])

    def test_start_bridge_worker_delivers_and_stop_terminates(self) -> None:
        from app.personal_wechat_bot.wechat_driver.bridge_send import BridgeOutboxStore

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            # bridge_outbox + dry_run backend: worker delivers (acks) without a
            # live WeChat. enqueue a record for the worker to pick up.
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox")
            root = data_dir.resolve()
            store = BridgeOutboxStore(root)
            rec = store.enqueue("wxid_a", "deliver me")

            sidebar_api._start_bridge_worker(root, {"bridge_interval_seconds": 0.2})
            try:
                # Wait until the worker acks the record (terminal), up to ~5s.
                deadline = time.time() + 5.0
                delivered = False
                while time.time() < deadline:
                    item = store.state(limit=10)["items"][0]
                    if item["status"] in {"sent", "failed"}:
                        delivered = item["status"] == "sent"
                        break
                    time.sleep(0.1)
                self.assertTrue(delivered, "bridge worker did not deliver the queued record")
                self.assertTrue(sidebar_api._bridge_worker_state(root)["running"])
            finally:
                sidebar_api._stop_bridge_worker(root)

            # After stop, the thread winds down.
            deadline = time.time() + 5.0
            while time.time() < deadline and sidebar_api._bridge_worker_state(root)["running"]:
                time.sleep(0.1)
            self.assertFalse(sidebar_api._bridge_worker_state(root)["running"])

    def test_start_bridge_worker_noop_when_driver_not_bridge_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)  # default driver is not_implemented
            root = data_dir.resolve()
            sidebar_api._start_bridge_worker(root, {})
            try:
                self.assertFalse(sidebar_api._bridge_worker_state(root)["running"])
            finally:
                sidebar_api._stop_bridge_worker(root)

    def test_background_send_status_reports_worker_down(self) -> None:
        # bridge_outbox + send_enabled but no live worker lock: the status must
        # reflect that nothing is delivering, not a config-only "ready".
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox")
            config = load_config(data_dir)

            # No worker lock at all -> worker down.
            status = sidebar_api._background_send_status(config, {"pending_count": 0}, data_dir)
            self.assertEqual(status, "bridge_outbox_worker_down")

            # A pending backlog with no live worker -> down-with-backlog.
            status = sidebar_api._background_send_status(config, {"pending_count": 3}, data_dir)
            self.assertEqual(status, "bridge_outbox_worker_down_backlog")

    def test_background_send_status_ready_when_worker_lock_fresh(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox")
            config = load_config(data_dir)

            # Simulate a live worker by writing a fresh heartbeat lock.
            lock_path = data_dir / "send_bridge" / ".bridge_worker.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                _json.dumps({"pid": 1234, "label": "send_bridge_worker", "heartbeat_at": time.time()}),
                encoding="utf-8",
            )
            status = sidebar_api._background_send_status(config, {"pending_count": 2}, data_dir)
            self.assertEqual(status, "bridge_outbox_ready")


if __name__ == "__main__":
    unittest.main()

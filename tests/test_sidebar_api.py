from __future__ import annotations

import json
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
    build_sidebar_task_manager,
    clear_sidebar_history_data,
    cleanup_file_workspace,
    cleanup_sidebar_channels,
    delete_sidebar_channel,
    get_model_config,
    list_api_keys,
    probe_model_fetch,
    remove_api_key,
    set_model_config,
    sidebar_resource_audit,
    sidebar_weflow_backfill,
    sidebar_weflow_cancel_backfill,
    sidebar_queue_action,
    sidebar_runtime_probe,
    sidebar_runtime_card_action,
    sidebar_weflow_dependency_status,
    update_sidebar_controls,
)
from app.personal_wechat_bot.conversation.channel_store import ConversationChannelStore
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.domain.models import NormalizedMessage, RawWeChatMessage
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool
from app.personal_wechat_bot.domain.models import ReplyCandidate
from app.personal_wechat_bot.normalizer.normalizer import MessageNormalizer
from app.personal_wechat_bot.normalizer.normalizer import conversation_id_for
from app.personal_wechat_bot.router.deduper import Deduper
from app.personal_wechat_bot.router.router import Router
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue


class SidebarApiTest(unittest.TestCase):
    def test_weflow_session_normalization_prefers_display_name_over_wxid(self) -> None:
        session = sidebar_api._normalize_weflow_session(
            {
                "id": "wxid_alice",
                "name": "wxid_alice",
                "displayName": "Alice",
                "remark": "Alice Remark",
                "type": "private",
            }
        )

        self.assertEqual(session["name"], "Alice Remark")

    def test_weflow_session_normalization_ignores_unknown_display_name(self) -> None:
        session = sidebar_api._normalize_weflow_session(
            {
                "id": "wxid_alice",
                "name": "unknown",
                "type": "private",
            }
        )

        self.assertEqual(session["name"], "wxid_alice")

    def test_weflow_params_ignore_stale_context_only_for_incremental_pull(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"

            params = sidebar_api._weflow_params(data_dir, {"context_only": True})
            forced = sidebar_api._weflow_params(data_dir, {"context_only": True, "force_context_only": True})
            backfill = sidebar_api._weflow_params(data_dir, {"since": 0})

            self.assertFalse(params["context_only"])
            self.assertTrue(forced["context_only"])
            self.assertTrue(backfill["context_only"])

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
            self.assertFalse((data_dir / "conversation_channels").exists())
            self.assertFalse((data_dir / "backend_events.jsonl").exists())
            self.assertTrue((data_dir / "config.json").exists())
            self.assertTrue((data_dir / "confirm_queue.jsonl").exists())
            self.assertTrue((data_dir / "send_bridge" / "outbox.jsonl").exists())
            self.assertTrue((data_dir / "send_bridge" / "acks.jsonl").exists())
            self.assertEqual(state["config"]["mode"], "confirm")

    def test_history_clear_removes_scheduler_authority_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            sidebar_api.TaskStatusStore(data_dir).create({"task_id": "old-task", "title": "Old task"})

            result = clear_sidebar_history_data(data_dir)
            state = build_sidebar_task_manager(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(state["counts"]["total"], 0)
            self.assertEqual(state["tasks"], [])

    def test_history_clear_removes_channel_state_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            sidebar_api.ChannelStateStore(data_dir).upsert(
                {"conversation_id": "conv-old", "chat_title": "Old", "status": "active"}
            )

            result = clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertFalse((data_dir / "channel_state.sqlite").exists())
            self.assertFalse((data_dir / "channel_state.sqlite-shm").exists())
            self.assertFalse((data_dir / "channel_state.sqlite-wal").exists())

    def test_resource_audit_is_cached_and_injected_into_task_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            audit = {
                "status": "ok",
                "schema": "local_resource_audit_v1",
                "snapshot": {
                    "cpu_name": "Test CPU",
                    "physical_cores": 16,
                    "logical_processors": 32,
                    "cpu_percent": 12.5,
                    "total_memory_mb": 32768,
                    "available_memory_mb": 20000,
                    "gpu_name": "Test GPU",
                    "gpu_memory_total_mb": 12288,
                    "gpu_memory_used_mb": 1024,
                },
                "recommendation": {
                    "media_cpu": 6,
                    "ocr_cpu_parallel": 5,
                    "asr_cpu_parallel": 2,
                    "file_io_parallel": 4,
                    "gpu_media": 1,
                    "llm_interactive_ratio": 0.7,
                    "llm_background_ratio": 0.3,
                    "thermal_risk": "low",
                    "reason": "test recommendation",
                },
            }

            with mock.patch.object(sidebar_api, "audit_local_resources", return_value=dict(audit)):
                result = sidebar_resource_audit(data_dir, {"manual": True})

            state = build_sidebar_state(data_dir)
            manager = state["task_manager"]
            pools = manager["scheduler"]["resource_pools"]

            self.assertEqual(result["status"], "ok")
            self.assertTrue((data_dir / "runtime" / "resource_audit.json").exists())
            self.assertEqual(state["resource_audit"]["snapshot"]["cpu_name"], "Test CPU")
            self.assertEqual(state["resource_scheduler"]["schema"], "resource_scheduler_v1")
            self.assertEqual(state["resource_scheduler"]["status"], "ok")
            self.assertEqual(state["resource_scheduler"]["interactive"]["media_cpu"], 6)
            self.assertEqual(state["resource_scheduler"]["interactive"]["max_parallel_conversations"], 4)
            self.assertEqual(state["resource_scheduler"]["background"]["max_parallel_conversations"], 2)
            self.assertEqual(pools["media_cpu"]["max_parallel"], 6)
            self.assertEqual(pools["file_io"]["max_parallel"], 4)
            self.assertIn("llm_interactive", pools)
            self.assertIn("llm_background", pools)
            self.assertEqual(pools["llm_interactive"]["max_parallel"], 4)
            self.assertEqual(pools["llm_background"]["max_parallel"], 2)
            self.assertIn("total", manager["scheduler"]["llm_gate"])
            self.assertEqual(manager["scheduler"]["llm_gate"]["total"]["max_parallel"], pools["llm"]["max_parallel"])
            self.assertEqual(manager["scheduler"]["resource_scheduler"]["interactive"]["file_io"], 4)
            self.assertEqual(manager["scheduler"]["resource_audit"]["recommendation"]["reason"], "test recommendation")

    def test_history_clear_tolerates_locked_weflow_process_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            log_file = data_dir / "weflow_process.err.log"
            log_file.write_text("busy log", encoding="utf-8")
            (data_dir / "backend_events.jsonl").write_text("{}\n", encoding="utf-8")
            original_unlink = Path.unlink

            def locked_log_unlink(path: Path, *args: object, **kwargs: object) -> None:
                if path.name == "weflow_process.err.log":
                    raise PermissionError(32, "another process is using this file", str(path))
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", locked_log_unlink):
                result = clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["retained_locked_count"], 1)
            self.assertEqual(result["retained_locked"][0]["fallback"], "truncated")
            self.assertTrue(log_file.exists())
            self.assertEqual(log_file.read_text(encoding="utf-8"), "")
            self.assertFalse((data_dir / "backend_events.jsonl").exists())

    def test_history_clear_reports_partial_error_for_locked_core_history_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            event_file = data_dir / "backend_events.jsonl"
            event_file.write_text("{}\n", encoding="utf-8")
            original_unlink = Path.unlink

            def locked_event_unlink(path: Path, *args: object, **kwargs: object) -> None:
                if path.name == "backend_events.jsonl":
                    raise PermissionError(32, "another process is using this file", str(path))
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(Path, "unlink", locked_event_unlink):
                result = clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "partial_error")
            self.assertEqual(result["error_count"], 1)
            self.assertIn("backend_events.jsonl", result["errors"][0]["relative_path"])
            self.assertTrue(event_file.exists())

    def test_history_clear_with_shutdown_schedules_helper_without_immediate_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            event_file = data_dir / "backend_events.jsonl"
            event_file.write_text("{}\n", encoding="utf-8")
            launch_state = {
                "pid": 1234,
                "mode": "server",
                "host": "127.0.0.1",
                "port": 8765,
                "interval_ms": 2500,
                "weflow": "on",
                "weflow_port": 5031,
                "weflow_host": "127.0.0.1",
                "install_weflow_deps": "never",
                "weflow_wait_seconds": 8,
                "weflow_window": "normal",
                "weflow_pid": 5678,
            }
            launch_file = data_dir / "runtime" / "sidebar_launch.json"
            launch_file.parent.mkdir(parents=True, exist_ok=True)
            launch_file.write_text(json.dumps(launch_state), encoding="utf-8")

            with mock.patch.object(sidebar_api.subprocess, "Popen", return_value=mock.Mock(pid=4321)) as popen:
                result = clear_sidebar_history_data(data_dir, {"shutdown_processes": True})

            command = popen.call_args.args[0]
            self.assertEqual(result["status"], "shutdown_scheduled")
            self.assertEqual(result["helper_pid"], 4321)
            self.assertTrue(event_file.exists())
            self.assertIn("sidebar_history_reset_shutdown.py", " ".join(command))
            self.assertNotIn("start_sidebar_frontend.py", " ".join(command))
            self.assertIn("--parent-pid", command)
            self.assertIn("1234", command)
            self.assertIn("--weflow-pid", command)
            self.assertIn("5678", command)
            self.assertTrue(result["manual_reopen_required"])

    def test_history_clear_with_shutdown_deduplicates_active_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            event_file = data_dir / "backend_events.jsonl"
            event_file.write_text("{}\n", encoding="utf-8")
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "history_reset_shutdown.lock").write_text(
                json.dumps({"helper_pid": 4321, "updated_at_epoch": time.time()}),
                encoding="utf-8",
            )
            (runtime_dir / "history_reset_shutdown.json").write_text(
                json.dumps({"status": "running", "phase": "stopping_weflow", "helper_pid": 4321, "parent_pid": 1234}),
                encoding="utf-8",
            )

            with mock.patch.object(sidebar_api, "_pid_exists", return_value=True), mock.patch.object(sidebar_api.subprocess, "Popen") as popen:
                result = clear_sidebar_history_data(data_dir, {"shutdown_processes": True})

            popen.assert_not_called()
            self.assertEqual(result["status"], "shutdown_scheduled")
            self.assertTrue(result["deduplicated"])
            self.assertEqual(result["helper_pid"], 4321)
            self.assertEqual(result["phase"], "stopping_weflow")
            self.assertTrue(result["manual_reopen_required"])
            self.assertTrue(event_file.exists())

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

    def test_sidebar_state_projects_channel_state_files_and_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            channel_store = ConversationChannelStore(
                data_dir,
                ApiKeyPool(config.providers.get("chat", config.llm), data_dir),
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            message = NormalizedMessage(
                message_id="msg-file",
                conversation_id="conv-file",
                conversation_type="private",
                chat_title="Alice",
                sender_name="Alice",
                sender_wechat_id="wxid_alice",
                text="请看这个文件",
                is_self=False,
                received_at="2026-07-08T10:00:00+08:00",
                metadata={
                    "source": "backend_events_jsonl",
                    "trusted_channel_source": True,
                    "attachments": [
                        {
                            "status": "indexed",
                            "file_id": "file-123",
                            "name": "report.pdf",
                            "kind": "file",
                            "parse": {
                                "status": "parsed",
                                "kind": "pdf",
                                "ai_analysis_status": "analyzed",
                                "ai_summary": "AI 已总结这份报告。",
                                "ai_key_points": ["第一点", "第二点"],
                                "chunk_count": 2,
                            },
                            "artifacts": {
                                "ai_analysis_status": "analyzed",
                                "ai_summary": "AI 已总结这份报告。",
                                "ai_key_points": ["第一点", "第二点"],
                                "chunk_count": 2,
                            },
                        }
                    ],
                },
            )
            channel_store.ensure_channel(message)
            sidebar_api.ChannelStateStore(data_dir).patch_control(
                "conv-file",
                {
                    "mode": "paused",
                    "priority": 88,
                    "wait_reason": "等待用户补充材料",
                    "operator_note": "暂停自动接话",
                },
                updated_by="tester",
            )
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(message)
            ledger.append_reply(
                ReplyCandidate(
                    message_id="msg-file",
                    conversation_id="conv-file",
                    text="我看到了报告。",
                    send_mode="confirm",
                    model="fake",
                ),
                chat_title="Alice",
            )
            sidebar_api.TaskStatusStore(data_dir).create(
                {
                    "task_id": "task-file",
                    "title": "处理文件请求",
                    "conversation_id": "conv-file",
                    "scope": "conversation:conv-file",
                    "topic_id": "topic-file",
                    "topic_title": "文件请求",
                    "status": "running",
                    "resource_class": "llm_interactive",
                    "estimated_cost": 3,
                }
            )

            state = build_sidebar_state(data_dir)
            channel = state["channels"]["items"][0]
            projected = channel["state"]
            file_state = projected["file_states"][0]

            self.assertIn("channel_states", state)
            self.assertTrue((data_dir / "channel_state.sqlite").exists())
            self.assertEqual(state["channels"]["state_schema"], "channel_state_v1")
            self.assertEqual(projected["current_topic"]["title"], "文件请求")
            self.assertEqual(file_state["file_id"], "file-123")
            self.assertEqual(file_state["summary"], "AI 已总结这份报告。")
            self.assertEqual(file_state["key_points"], ["第一点", "第二点"])
            self.assertEqual(projected["reply_state"]["status"], "candidate")
            self.assertEqual(projected["control"]["mode"], "paused")
            self.assertEqual(projected["control"]["priority"], 88)
            self.assertEqual(projected["control"]["wait_reason"], "等待用户补充材料")
            self.assertEqual(projected["effective_status"], "paused")
            self.assertEqual(state["channel_states"][0]["conversation_id"], "conv-file")

    def test_channel_state_action_updates_control_and_survives_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            channel_store = ConversationChannelStore(
                data_dir,
                ApiKeyPool(config.providers.get("chat", config.llm), data_dir),
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            message = NormalizedMessage(
                message_id="msg-control",
                conversation_id="conv-control",
                conversation_type="private",
                chat_title="Control",
                sender_name="Control",
                sender_wechat_id="wxid_control",
                text="hello",
                is_self=False,
                received_at="2026-07-08T10:00:00+08:00",
                metadata={"source": "backend_events_jsonl", "trusted_channel_source": True},
            )
            channel_store.ensure_channel(message)

            paused = sidebar_api.sidebar_channel_state_action(
                data_dir,
                {
                    "action": "pause",
                    "conversation_id": "conv-control",
                    "wait_reason": "人工检查",
                    "operator_note": "先暂停",
                    "priority": 91,
                    "updated_by": "tester",
                },
            )
            refreshed = build_sidebar_state(data_dir)
            resumed = sidebar_api.sidebar_channel_state_action(
                data_dir,
                {"action": "resume", "conversation_id": "conv-control", "updated_by": "tester"},
            )
            pinned = sidebar_api.sidebar_channel_state_action(
                data_dir,
                {"action": "pin", "conversation_id": "conv-control", "updated_by": "tester"},
            )

            self.assertEqual(paused["channel_state"]["control"]["mode"], "paused")
            self.assertEqual(paused["channel_state"]["control"]["priority"], 91)
            self.assertIn("task_manager", paused)
            self.assertEqual(refreshed["channels"]["items"][0]["state"]["effective_status"], "paused")
            self.assertEqual(refreshed["channels"]["items"][0]["state"]["control"]["wait_reason"], "人工检查")
            self.assertEqual(resumed["channel_state"]["control"]["mode"], "active")
            self.assertEqual(resumed["channel_state"]["control"]["wait_reason"], "")
            self.assertTrue(pinned["channel_state"]["control"]["pinned"])

    def test_channel_state_refresh_without_channels_does_not_delete_control_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = sidebar_api.ChannelStateStore(data_dir)
            store.patch_control(
                "conv-orphan",
                {
                    "mode": "paused",
                    "priority": 77,
                    "wait_reason": "等待通道恢复",
                },
                updated_by="tester",
            )

            state = build_sidebar_state(data_dir)
            reloaded = store.get("conv-orphan")

            self.assertEqual(state["channels"]["items"], [])
            self.assertEqual(state["channel_states"][0]["conversation_id"], "conv-orphan")
            self.assertEqual(state["channel_states"][0]["control"]["mode"], "paused")
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded["control"]["mode"], "paused")  # type: ignore[index]

    def test_weflow_backfill_returns_async_job_and_can_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            with mock.patch.object(sidebar_api, "_run_sidebar_weflow_once", return_value={"status": "ok", "source": {"scanned_count": 1, "appended_count": 1}, "pull": {"processed_count": 1}}):
                result = sidebar_weflow_backfill(data_dir, {"talkers": ["wxid_user"], "max_messages": 1})
                self.assertEqual(result["status"], "started")
                job_id = result["backfill_job"]["job_id"]
                self.assertTrue(job_id)
                self.assertTrue(result["backfill_job"]["running"])

                deadline = threading.Event()
                for _ in range(20):
                    state = sidebar_api.build_sidebar_weflow_state(data_dir)
                    job = state["backfill_job"]
                    if job.get("status") == "completed" and not job.get("running"):
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
                    job = state["backfill_job"]
                    if job.get("status") == "cancelled" and not job.get("running"):
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

    def test_weflow_pull_once_can_run_as_background_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            fake_result = {"status": "ok", "source": {"scanned_count": 1, "appended_count": 1}, "pull": {"processed_count": 1}}

            with mock.patch.object(sidebar_api, "_run_sidebar_weflow_once", return_value=fake_result):
                result = sidebar_api.sidebar_weflow_pull_once(data_dir, {"talkers": ["wxid_user"], "background": True})
                self.assertEqual(result["status"], "started")
                self.assertTrue(result["pull_job"]["job_id"])
                self.assertTrue(result["pull_job"]["running"])

                deadline = threading.Event()
                for _ in range(20):
                    state = sidebar_api.build_sidebar_weflow_state(data_dir)
                    job = state["pull_job"]
                    if job.get("status") == "completed" and not job.get("running"):
                        break
                    deadline.wait(0.05)

            state = sidebar_api.build_sidebar_weflow_state(data_dir)
            self.assertEqual(state["pull_job"]["status"], "completed")
            self.assertEqual(state["last_pull"]["status"], "ok")

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

    def test_sidebar_agent_tick_reads_session_and_runs_reply_closed_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("private", "wxid_alice")
            ConversationLedgerStore(data_dir).append_message(
                NormalizedMessage(
                    message_id="seed-message",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Alice",
                    sender_name="Alice",
                    sender_wechat_id="wxid_alice",
                    text="上一句上下文",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test"},
                )
            )
            append_sidebar_backend_event(
                data_dir,
                {
                    "raw_id": "agent-tick-message-1",
                    "chat_title": "Alice",
                    "sender_name": "Alice",
                    "sender_wechat_id": "wxid_alice",
                    "text": "今天有点累",
                    "observed_at": "2026-07-08T09:01:00+08:00",
                    "source_payload": {"conversation_key": "wxid_alice", "talker_id": "wxid_alice"},
                },
            )

            result = sidebar_api.sidebar_agent_tick(data_dir, {})

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["agent"]["processed_count"], 1)
            self.assertEqual(result["session_snapshot"]["before"]["conversation_count"], 1)
            self.assertEqual(result["session_snapshot"]["before"]["conversations"][0]["last_user_message"], "上一句上下文")
            after = result["session_snapshot"]["after"]
            self.assertEqual(after["conversation_count"], 1)
            self.assertEqual(after["pending_user_count"], 0)
            self.assertTrue(after["topic_candidates"])
            conversation = after["conversations"][0]
            self.assertEqual(conversation["conversation_id"], conversation_id)
            self.assertTrue(conversation["last_assistant_reply"])
            self.assertTrue(Path(conversation["ledger_markdown"]).exists())
            entries = ConversationLedgerStore(data_dir).read_entries(conversation_id)
            self.assertEqual(entries[-1].role, "assistant")
            self.assertEqual(entries[-1].send["status"], "skipped")
            task_statuses = {item["kind"]: item["status"] for item in result["task_manager"]["tasks"]}
            self.assertEqual(task_statuses["Agent"], "completed")

    def test_sidebar_controls_update_mode_and_send_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            result = update_sidebar_controls(
                data_dir,
                {
                    "mode": "confirm",
                    "send_enabled": True,
                    "send_driver": "bridge_outbox",
                    "ocr_mode": "gpu",
                    "asr_mode": "cpu",
                    "file_max_bytes": 32 * 1024 * 1024,
                },
            )
            config = load_config(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(config.mode, "confirm")
            self.assertTrue(config.send_enabled)
            self.assertEqual(config.send_driver, "bridge_outbox")
            self.assertEqual(config.ocr_mode, "gpu")
            self.assertEqual(config.asr_mode, "cpu")
            self.assertEqual(config.file_max_bytes, 32 * 1024 * 1024)
            self.assertEqual(build_sidebar_state(data_dir)["config"]["ocr_mode"], "gpu")
            self.assertEqual(build_sidebar_state(data_dir)["config"]["file_max_bytes"], 32 * 1024 * 1024)

    def test_sidebar_controls_parse_string_false_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            result = update_sidebar_controls(
                data_dir,
                {
                    "mode": "auto",
                    "send_enabled": "false",
                    "send_driver": "bridge_outbox",
                    "send_confirm_required": "false",
                },
            )
            config = load_config(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertFalse(config.send_enabled)
            self.assertFalse(config.send_confirm_required)
            self.assertEqual(build_sidebar_state(data_dir)["config"]["send_enabled"], False)

    def test_config_loader_parses_string_false_booleans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config_path = data_dir / "config.json"
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["send_enabled"] = "false"
            raw["send_confirm_required"] = "false"
            raw["save_full_chat"] = "false"
            raw["save_raw_and_summary"] = "false"
            config_path.write_text(json.dumps(raw), encoding="utf-8")

            config = load_config(data_dir)

            self.assertFalse(config.send_enabled)
            self.assertFalse(config.send_confirm_required)
            self.assertFalse(config.save_full_chat)
            self.assertFalse(config.save_raw_and_summary)

    def test_runtime_probe_uses_configured_ingest_engines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            ocr_engine = mock.Mock()
            ocr_engine.health.return_value = {"available": True, "gpu_available": True, "gpu_used": True, "mode": "gpu"}
            asr_engine = mock.Mock()
            asr_engine.health.return_value = {"available": True, "gpu_available": True, "gpu_used": True, "mode": "gpu"}
            with mock.patch.object(sidebar_api, "build_default_ocr_engine", return_value=ocr_engine) as ocr_factory, mock.patch.object(
                sidebar_api, "LocalAsrSubprocessEngine", return_value=asr_engine
            ) as asr_factory:
                result = sidebar_runtime_probe(data_dir, {"ocr_mode": "gpu", "asr_mode": "gpu"})

            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["same_path_as_ingest"])
            self.assertTrue(result["gpu"]["ocr_enabled"])
            self.assertTrue(result["gpu"]["asr_enabled"])
            ocr_factory.assert_called_once_with(mode="gpu")
            asr_factory.assert_called_once_with(mode="gpu")

    def test_runtime_probe_run_sample_checks_asr_worker_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            ocr_result = mock.Mock()
            ocr_result.text = ""
            ocr_result.item_count = 0
            ocr_result.metadata = {"gpu_used": True, "backends": ["paddleocr_gpu"]}
            ocr_engine = mock.Mock()
            ocr_engine.health.return_value = {"available": True, "gpu_available": True, "gpu_used": True, "mode": "gpu"}
            ocr_engine.read_structured.return_value = ocr_result
            transcript = mock.Mock()
            transcript.status = "empty"
            transcript.text = ""
            transcript.backend = "faster_whisper_gpu"
            transcript.model = "base"
            transcript.language = ""
            transcript.error = ""
            asr_engine = mock.Mock()
            asr_engine.health.return_value = {"available": True, "gpu_available": True, "gpu_used": True, "mode": "gpu"}
            asr_engine.transcribe.return_value = transcript
            with mock.patch.object(sidebar_api, "build_default_ocr_engine", return_value=ocr_engine), mock.patch.object(
                sidebar_api, "LocalAsrSubprocessEngine", return_value=asr_engine
            ):
                result = sidebar_runtime_probe(data_dir, {"ocr_mode": "gpu", "asr_mode": "gpu", "run_sample": True})

            self.assertTrue(result["gpu"]["ocr_worker_checked"])
            self.assertTrue(result["gpu"]["asr_worker_checked"])
            self.assertTrue(result["gpu"]["asr_enabled"])
            self.assertEqual(result["gpu"]["asr_worker_backend"], "faster_whisper_gpu")
            self.assertEqual(result["asr"]["sample"]["metadata"]["backend"], "faster_whisper_gpu")
            asr_engine.transcribe.assert_called_once()

    def test_sidebar_queue_action_approves_and_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply())

            approved = sidebar_queue_action(data_dir, "approve", queue_id, {"reviewer": "test"})

            self.assertEqual(approved["item"]["status"], "approved")

    def test_sidebar_queue_action_removes_queue_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply())

            removed = sidebar_queue_action(data_dir, "remove", queue_id, {"reviewer": "test"})

            self.assertEqual(removed["status"], "ok")
            self.assertTrue(removed["removed"])
            self.assertIsNone(queue.get(queue_id))

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

            def fake_tick(context, **_kwargs):
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

            def fake_tick(context, **_kwargs):
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
                    "max_concurrency": 9,
                },
            )
            listed = list_api_keys(data_dir)

            self.assertEqual(result["status"], "ok")
            item = next(item for item in listed["keys"] if item["ref"] == ref)
            self.assertEqual(item["model_config"]["provider"], "relay")
            self.assertEqual(item["model_config"]["model"], "gpt-5.5")
            self.assertEqual(item["model_config"]["base_url"], "https://relay.example/v1")
            self.assertEqual(item["model_config"]["max_concurrency"], 9)


class SidebarModelConfigApiTest(unittest.TestCase):
    def test_get_and_set_model_config_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            initial = get_model_config(data_dir)
            self.assertEqual(initial["status"], "ok")
            self.assertIn("deepseek", initial["provider_formats"])
            self.assertTrue(initial["async_summary_follows_chat"])
            self.assertGreaterEqual(initial["max_concurrency"], 1)

            result = set_model_config(
                data_dir,
                {
                    "provider": "relay",
                    "model": "gpt-5.5",
                    "base_url": "https://relay.example/v1",
                    "max_wait_seconds": 90,
                    "max_concurrency": 8,
                },
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["model"], "gpt-5.5")

            updated = get_model_config(data_dir)
            self.assertEqual(updated["provider"], "relay")
            self.assertEqual(updated["model"], "gpt-5.5")
            self.assertEqual(updated["base_url"], "https://relay.example/v1")
            self.assertEqual(updated["max_wait_seconds"], 90)
            self.assertEqual(updated["max_concurrency"], 8)

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

    def test_set_model_config_syncs_keys_inheriting_provider_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            added = add_api_key(data_dir, {"value": "sk-sync-concurrency-1234"})

            set_model_config(data_dir, {"max_concurrency": 11})

            listed = list_api_keys(data_dir)
            item = next(item for item in listed["keys"] if item["ref"] == added["ref"])
            self.assertEqual(item["model_config"]["max_concurrency"], 11)

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
    def test_weflow_stop_terminalizes_worker_task_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = (Path(tmp) / "data").resolve()
            create_default_config(data_dir)
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
            sidebar_api.TaskStatusStore(data_dir).create(
                {
                    "task_id": "weflow-worker-test",
                    "title": "WeFlow worker",
                    "kind": "WeFlow",
                    "status": "running",
                    "scope": "weflow:worker",
                    "external_id": "worker",
                }
            )
            try:
                result = sidebar_api.sidebar_weflow_stop(data_dir, {})
            finally:
                stop.set()
                alive_thread.join(timeout=1)
                sidebar_api._WEFLOW_WORKERS.pop(key, None)

            self.assertEqual(result["status"], "ok")
            self.assertIn("task_manager", result)
            tasks = {item["task_id"]: item for item in result["task_manager"]["tasks"]}
            self.assertEqual(tasks["weflow-worker-test"]["status"], "completed")
            self.assertEqual(tasks["weflow-worker-test"]["progress"], 100)

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

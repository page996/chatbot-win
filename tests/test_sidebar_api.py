from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from app.personal_wechat_bot.config.loader import create_default_config, load_config, persistent_config_dir, save_config
from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.control import sidebar_api
from app.personal_wechat_bot.control.send_commands import (
    send_approved_confirm_item,
    set_send_controls,
    sync_bridge_ack_to_send_state,
)
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
    retry_sidebar_bridge_item,
    set_model_config,
    sidebar_channel_test_file,
    sidebar_channel_test_reply,
    sidebar_diagnostics_export,
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
from app.personal_wechat_bot.conversation.session_store import DEFAULT_SESSION_ID, ConversationSessionStore
from app.personal_wechat_bot.domain.models import NormalizedMessage, RawWeChatMessage, SendResult
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool
from app.personal_wechat_bot.domain.models import ReplyCandidate
from app.personal_wechat_bot.normalizer.normalizer import MessageNormalizer
from app.personal_wechat_bot.normalizer.normalizer import conversation_id_for
from app.personal_wechat_bot.persona.runtime_cards import RuntimeCardStore
from app.personal_wechat_bot.router.deduper import Deduper
from app.personal_wechat_bot.router.router import Router
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue
from app.personal_wechat_bot.reply_gate.send_audit import SendAuditLog


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

    def test_weflow_session_registration_blocks_unidentified_private_contact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            result = sidebar_api._register_weflow_sessions(
                data_dir,
                [
                    {"id": "wxid_unknown", "name": "wxid_unknown", "type": "private"},
                    {"id": "wxid_alice", "name": "Alice", "type": "private", "is_friend": True},
                ],
            )

            self.assertEqual(result["registered_count"], 1)
            self.assertEqual(result["registered_channels"][0]["id"], "wxid_alice")
            self.assertEqual(result["skipped_count"], 1)
            self.assertEqual(result["skipped_channels"][0]["id"], "wxid_unknown")
            self.assertIn("private_contact_unknown", result["skipped_channels"][0]["reason"])
            unknown_id = conversation_id_for("private", "wxid_unknown")
            self.assertIsNone(sidebar_api._channel_store(data_dir).get_channel(unknown_id))
            store = sidebar_api._upsert_weflow_sessions(
                data_dir,
                [
                    {"id": "wxid_unknown", "name": "wxid_unknown", "type": "private"},
                    {"id": "wxid_alice", "name": "Alice", "type": "private", "is_friend": True},
                ],
                source="test",
                registration=result,
            )
            cached = json.loads(Path(store["store"]).read_text(encoding="utf-8"))["sessions"]
            self.assertEqual(cached["wxid_unknown"]["channel_registration_status"], "blocked")
            self.assertEqual(cached["wxid_unknown"]["conversation_id"], "")
            self.assertEqual(cached["wxid_alice"]["channel_registration_status"], "registered")

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
            self.assertIn("agent", state)
            self.assertEqual(state["agent"]["status"], "idle")
            self.assertIn("queued_to_bridge", state["queues"])
            self.assertEqual(state["queues"]["by_channel"]["count"], 1)
            self.assertEqual(state["queues"]["pending"]["channels"][0]["conversation_id"], "private-1")
            self.assertEqual(state["capture"]["background_send_status"], "bridge_outbox_configured_disabled")
            self.assertIn("native_migration", state)
            self.assertIn("skill.file_workspace_agent", [item["card_id"] for item in state["runtime_cards"]["active"]["skills"]])

    def test_native_migration_probe_persists_supported_version_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            message_root = Path(tmp) / "Documents" / "xwechat_files"
            (message_root / "wxid_alice" / "Msg").mkdir(parents=True)
            (message_root / "wxid_alice" / "Msg" / "MSG0.db").write_bytes(b"sqlite")
            process = {
                "pid": 123,
                "name": "Weixin",
                "path": r"C:\Program Files\Tencent\Weixin\Weixin.exe",
                "root": r"C:\Program Files\Tencent\Weixin",
                "product_version": "4.1.10.53",
                "file_version": "4.1.10.53",
            }

            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.wechat_native_http_status",
                return_value={
                    "status": "available",
                    "available": True,
                    "health": {"WeChatVersion": "4.1.10.53", "dataDir": str(message_root)},
                    "send_capabilities": {},
                    "reason": "",
                },
            ), mock.patch(
                "app.personal_wechat_bot.control.sidebar_api._wechat_native_processes",
                return_value=[process],
            ), mock.patch.dict(
                os.environ,
                {"WECHAT_NATIVE_MESSAGE_ROOT": str(message_root)},
                clear=False,
            ):
                result = sidebar_api.sidebar_native_migration_probe(
                    data_dir,
                    {"persist": True, "include_cleanup_sizes": False, "max_depth": 3, "max_entries": 100},
                )
                state = sidebar_api.build_sidebar_native_migration_state(data_dir)

            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["version_gate"]["gate"], "supported")
            self.assertTrue(result["message_path_candidates"][0]["exists"])
            self.assertIn("cleanup_manifest", result)
            cleanup_paths = {item["relative_path"] for item in result["cleanup_manifest"]["items"]}
            self.assertIn("vendor/reference/WeChat-Hook-aixed/.git", cleanup_paths)
            self.assertIn("vendor/reference/WeChat-Hook-aixed/x64_Version", cleanup_paths)
            self.assertIn("vendor/reference/WeFlow-gitcode/.git", cleanup_paths)
            manifest = result["deploy_manifest"]
            self.assertEqual(manifest["schema"], "native_deploy_manifest_v1")
            required_paths = {item["relative_path"]: item for item in manifest["required_paths"]}
            self.assertTrue(required_paths["vendor/artifacts/wechat-native-411053/version.dll"]["required"])
            self.assertIn("scripts/deploy_wechat_native_hook.ps1", required_paths)
            self.assertIn("requirements-document.txt", {item["relative_path"] for item in manifest["optional_dependency_paths"]})
            self.assertIn("install_python_dependencies", [item["step"] for item in manifest["operator_steps"]])
            self.assertTrue((data_dir / "native_diagnostics" / "native-migration-latest.json").exists())
            self.assertEqual(state["latest"]["status"], "ready")
            self.assertIn("deploy_manifest", state["latest"])

    def test_native_migration_probe_uses_extra_version_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            message_root = Path(tmp) / "xwechat_files"
            (message_root / "Msg").mkdir(parents=True)
            (message_root / "Msg" / "MSG0.db").write_bytes(b"sqlite")

            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.wechat_native_http_status",
                return_value={
                    "status": "available",
                    "available": True,
                    "health": {"IsLogin": 1},
                    "send_capabilities": {},
                    "reason": "",
                },
            ), mock.patch(
                "app.personal_wechat_bot.control.sidebar_api._wechat_native_extra_http_probes",
                return_value=[
                    {
                        "endpoint": "/debug/status",
                        "status": "ok",
                        "payload": {"version": "4.1.10.53", "dataDir": str(message_root)},
                    }
                ],
            ), mock.patch(
                "app.personal_wechat_bot.control.sidebar_api._wechat_native_processes",
                return_value=[],
            ):
                result = sidebar_api.sidebar_native_migration_probe(
                    data_dir,
                    {"persist": False, "include_cleanup_sizes": False, "max_depth": 2, "max_entries": 100},
                )

            self.assertEqual(result["version_gate"]["gate"], "supported")
            self.assertTrue(result["message_scan"]["deep_scan"])
            self.assertEqual(result["message_path_candidates"][0]["scan_status"], "scanned")

    def test_sidebar_queues_are_grouped_by_conversation_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            _ensure_test_channel(data_dir, conversation_id="private-1", sender_wechat_id="wxid_alice", chat_title="Alice")
            _ensure_test_channel(data_dir, conversation_id="private-2", sender_wechat_id="wxid_bob", chat_title="Bob")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            alice_id = queue.enqueue(_reply(message_id="alice-message", conversation_id="private-1", text="hello alice"))
            bob_id = queue.enqueue(_reply(message_id="bob-message", conversation_id="private-2", text="hello bob"))
            queue.approve(bob_id, reviewer="test")

            state = build_sidebar_state(data_dir)

            self.assertEqual(state["queues"]["pending"]["count"], 1)
            self.assertEqual(state["queues"]["approved"]["count"], 1)
            self.assertEqual(state["queues"]["pending"]["items"][0]["queue_id"], alice_id)
            self.assertEqual(state["queues"]["pending"]["channels"][0]["display_name"], "Alice")
            self.assertEqual(state["queues"]["pending"]["channels"][0]["status_counts"]["pending"], 1)
            self.assertEqual(state["queues"]["approved"]["channels"][0]["display_name"], "Bob")
            grouped = {item["conversation_id"]: item for item in state["queues"]["by_channel"]["channels"]}
            self.assertEqual(grouped["private-1"]["statuses"]["pending"]["items"][0]["queue_id"], alice_id)
            self.assertEqual(grouped["private-2"]["statuses"]["approved"]["items"][0]["queue_id"], bob_id)

    def test_sidebar_audit_is_grouped_by_conversation_channel_even_after_queue_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            _ensure_test_channel(data_dir, conversation_id="private-1", sender_wechat_id="wxid_alice", chat_title="Alice")
            _ensure_test_channel(data_dir, conversation_id="private-2", sender_wechat_id="wxid_bob", chat_title="Bob")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            alice_id = queue.enqueue(_reply(message_id="alice-message", conversation_id="private-1", text="hello alice"))
            bob_id = queue.enqueue(_reply(message_id="bob-message", conversation_id="private-2", text="hello bob"))

            sidebar_queue_action(data_dir, "remove", alice_id, {"reviewer": "test"})
            sidebar_queue_action(data_dir, "approve", bob_id, {"reviewer": "test"})
            state = build_sidebar_state(data_dir)

            grouped = {item["conversation_id"]: item for item in state["audit"]["channels"]}
            self.assertEqual(grouped["private-1"]["display_name"], "Alice")
            self.assertEqual(grouped["private-1"]["phase_counts"]["pending"], 1)
            self.assertEqual(grouped["private-1"]["phases"]["pending"]["items"][0]["queue_id"], alice_id)
            self.assertEqual(grouped["private-1"]["phases"]["pending"]["items"][0]["conversation_id"], "private-1")
            self.assertEqual(grouped["private-1"]["phases"]["pending"]["items"][0]["channel_display_name"], "Alice")
            self.assertEqual(grouped["private-1"]["phases"]["pending"]["items"][0]["phase"], "pending")
            self.assertEqual(grouped["private-2"]["display_name"], "Bob")
            self.assertEqual(grouped["private-2"]["phase_counts"]["approved"], 1)
            self.assertEqual(grouped["private-2"]["phases"]["approved"]["items"][0]["queue_id"], bob_id)
            self.assertEqual(grouped["private-2"]["phases"]["approved"]["items"][0]["conversation_id"], "private-2")
            self.assertEqual(grouped["private-2"]["phases"]["approved"]["items"][0]["channel_display_name"], "Bob")
            self.assertEqual(grouped["private-2"]["phases"]["approved"]["items"][0]["phase"], "approved")

    def test_sidebar_state_restores_config_from_persistent_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="wechat_native_http")
            (data_dir / "config.json").unlink()

            state = build_sidebar_state(data_dir)

            self.assertEqual(state["status"], "ok")
            self.assertEqual(state["config"]["mode"], "confirm")
            self.assertEqual(state["config"]["send_driver"], "bridge_outbox")
            self.assertTrue((data_dir / "config.json").exists())

    def test_update_sidebar_controls_persists_wechat_native_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            result = update_sidebar_controls(
                data_dir,
                {
                    "mode": "confirm",
                    "send_enabled": True,
                    "send_driver": "bridge_outbox",
                    "send_backend": "wechat_native_http",
                    "wechat_native_base_url": "http://127.0.0.1:30001",
                    "wechat_native_send_text_path": "/custom-text",
                    "wechat_native_status_path": "/custom-status",
                    "wechat_native_verify_timeout_seconds": 2.5,
                    "wechat_native_file_verify_timeout_seconds": 33.0,
                },
            )
            config = load_config(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(config.send_backend, "wechat_native_http")
            self.assertEqual(config.wechat_native_base_url, "http://127.0.0.1:30001")
            self.assertEqual(config.wechat_native_send_text_path, "/custom-text")
            self.assertEqual(config.wechat_native_status_path, "/custom-status")
            self.assertEqual(config.wechat_native_verify_timeout_seconds, 2.5)
            self.assertEqual(config.wechat_native_file_verify_timeout_seconds, 33.0)

    def test_history_clear_preserves_sidebar_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="dry_run")
            from app.personal_wechat_bot.wechat_driver.bridge_send import BridgeAckStatus, BridgeOutboxStore

            store = BridgeOutboxStore(data_dir)
            record = store.enqueue("wxid_a", "preserve bridge evidence", receiver="wxid_a")
            store.append_ack(record["bridge_id"], status=BridgeAckStatus.SENT, reason="native_verified")
            lock_path = data_dir / "send_bridge" / ".bridge_worker.lock"
            lock_path.write_text(
                json.dumps({"pid": os.getpid(), "label": "send_bridge_worker", "heartbeat_at": time.time()}),
                encoding="utf-8",
            )
            synced_path = data_dir / "send_bridge" / "synced_acks.json"
            synced_path.write_text(json.dumps({"seen": [record["bridge_id"]]}), encoding="utf-8")
            reverify_path = data_dir / "send_bridge" / "accepted_reverify.json"
            reverify_path.write_text(json.dumps({record["bridge_id"]: {"attempts": 1}}), encoding="utf-8")
            ConfirmQueue(data_dir / "confirm_queue.jsonl").enqueue(_reply(message_id="pending-before-clear"))
            SendAuditLog(data_dir / "send_audit.jsonl").append(
                "confirm_approve",
                queue_id="pending-before-clear",
                status="approved",
            )
            self.assertTrue((data_dir / "confirm_queue.sqlite").exists())
            self.assertTrue((data_dir / "send_audit.sqlite").exists())
            outbox_before = (data_dir / "send_bridge" / "outbox.jsonl").read_text(encoding="utf-8")
            acks_before = (data_dir / "send_bridge" / "acks.jsonl").read_text(encoding="utf-8")
            lock_before = lock_path.read_text(encoding="utf-8")
            synced_before = synced_path.read_text(encoding="utf-8")
            reverify_before = reverify_path.read_text(encoding="utf-8")
            history_conversation_id = "history-private"
            history_message = NormalizedMessage(
                message_id="history-before-clear",
                conversation_id=history_conversation_id,
                conversation_type="private",
                chat_title="History Friend",
                sender_name="History Friend",
                sender_wechat_id="wxid_history_friend",
                text="history that must not return",
                is_self=False,
                received_at="2026-07-10T00:00:00+08:00",
                metadata={
                    "source": "backend_events_jsonl",
                    "trusted_channel_source": True,
                    "conversation_key": "wxid_history_friend",
                    "is_friend": True,
                },
            )
            history_config = load_config(data_dir)
            history_channels = ConversationChannelStore(
                data_dir,
                ApiKeyPool(history_config.providers["chat"], data_dir),
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            history_channels.ensure_channel(history_message)
            ConversationLedgerStore(data_dir).append_message(history_message)
            history_sessions = ConversationSessionStore(data_dir)
            history_sessions.current_session_id_for_message(history_message)
            history_sessions.reset_session(history_conversation_id, reason="history_clear_test")
            authority_names = (
                "conversation_channels.sqlite",
                "conversation_ledger.sqlite",
                "conversation_sessions.sqlite",
            )
            for name in authority_names:
                self.assertTrue((data_dir / name).exists())
                (data_dir / f"{name}-wal").write_bytes(b"stale-wal")
                (data_dir / f"{name}-shm").write_bytes(b"stale-shm")
            (data_dir / "conversation_ledgers" / "old.md").write_text("history", encoding="utf-8")
            (data_dir / "backend_events.jsonl").write_text("{}\n", encoding="utf-8")
            (data_dir / "hook_events.jsonl").write_text("{}\n", encoding="utf-8")
            (data_dir / "hook_events.jsonl.raw_ids.json").write_text(json.dumps({"old-hook": True}), encoding="utf-8")
            (data_dir / "hook_events_state.json.consumer.lock").write_text(
                json.dumps({"pid": 999999, "label": "old-consumer"}),
                encoding="utf-8",
            )
            RuntimeCardStore(data_dir).apply_action(
                "save-task",
                {"name": "persistent launch card", "content": "survive history clear"},
            )
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            agent_state_path = runtime_dir / "agent_state.json"
            launch_path = runtime_dir / "sidebar_launch.json"
            resource_audit_path = runtime_dir / "resource_audit.json"
            agent_state_path.write_text(json.dumps({"cursor": 123, "status": "old"}), encoding="utf-8")
            launch_path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            resource_audit_path.write_text(json.dumps({"status": "ok"}), encoding="utf-8")

            result = clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertFalse((data_dir / "conversation_ledgers").exists())
            self.assertFalse((data_dir / "conversation_channels").exists())
            removed_paths = {item["relative_path"] for item in result["removed"]}
            expected_authority_paths = {
                name
                for authority in authority_names
                for name in (authority, f"{authority}-wal", f"{authority}-shm")
            }
            self.assertTrue(expected_authority_paths.issubset(removed_paths))
            for name in authority_names:
                self.assertFalse((data_dir / name).exists())
            self.assertFalse((data_dir / "backend_events.jsonl").exists())
            self.assertFalse((data_dir / "hook_events.jsonl.raw_ids.json").exists())
            self.assertFalse((data_dir / "hook_events_state.json.consumer.lock").exists())
            self.assertTrue((data_dir / "config.json").exists())
            self.assertTrue((data_dir / "confirm_queue.jsonl").exists())
            self.assertTrue((data_dir / "confirm_queue.sqlite").exists())
            self.assertTrue((data_dir / "send_audit.sqlite").exists())
            self.assertEqual(ConfirmQueue(data_dir / "confirm_queue.jsonl").list_pending(), [])
            self.assertEqual(SendAuditLog(data_dir / "send_audit.jsonl").list_recent(limit=10), [])
            self.assertTrue((data_dir / "send_bridge" / "outbox.jsonl").exists())
            self.assertTrue((data_dir / "send_bridge" / "acks.jsonl").exists())
            self.assertEqual((data_dir / "send_bridge" / "outbox.jsonl").read_text(encoding="utf-8"), outbox_before)
            self.assertEqual((data_dir / "send_bridge" / "acks.jsonl").read_text(encoding="utf-8"), acks_before)
            self.assertEqual(lock_path.read_text(encoding="utf-8"), lock_before)
            self.assertEqual(synced_path.read_text(encoding="utf-8"), synced_before)
            self.assertEqual(reverify_path.read_text(encoding="utf-8"), reverify_before)
            self.assertFalse(agent_state_path.exists())
            self.assertTrue(launch_path.exists())
            self.assertTrue(resource_audit_path.exists())
            self.assertIn("runtime/agent_state.json", {item["relative_path"] for item in result["removed"]})
            self.assertIn(str(Path("send_bridge") / "outbox.jsonl"), result["preserved_runtime"])
            self.assertIn(str(Path("send_bridge") / "acks.jsonl"), result["preserved_runtime"])
            self.assertIn(str(Path("send_bridge") / "synced_acks.json"), result["preserved_runtime"])
            self.assertIn(str(Path("send_bridge") / "accepted_reverify.json"), result["preserved_runtime"])
            self.assertIn(str(Path("send_bridge") / ".bridge_worker.lock"), result["preserved_runtime"])

            reopened_config = load_config(data_dir)
            reopened_channels = ConversationChannelStore(
                data_dir,
                ApiKeyPool(reopened_config.providers["chat"], data_dir),
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            self.assertIsNone(reopened_channels.get_channel(history_conversation_id))
            self.assertEqual(ConversationLedgerStore(data_dir).read_entries(history_conversation_id), [])
            reopened_sessions = ConversationSessionStore(data_dir)
            reopened_session_state = reopened_sessions.state_for_conversation(history_conversation_id)
            self.assertEqual(reopened_session_state["current_session_id"], DEFAULT_SESSION_ID)
            self.assertEqual(reopened_session_state["reset_count"], 0)
            self.assertEqual(reopened_sessions.database.list_events(history_conversation_id), [])
            for name in authority_names:
                self.assertTrue((data_dir / name).exists())

            state = build_sidebar_state(data_dir)
            self.assertEqual(state["config"]["mode"], "confirm")
            self.assertIn("survive history clear", "\n".join(RuntimeCardStore(data_dir).prompt_lines()))

    def test_history_clear_preserves_config_runtime_cards_and_bridge_evidence_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            config.accepted_contacts.add("wxid_friend")
            config.accepted_groups.add("family@chatroom")
            config.search_blocklist = ["ads.example", "login.example"]
            config.wechat_native_base_url = "http://127.0.0.1:30001"
            config.wechat_native_send_text_path = "/custom-text"
            config.wechat_native_status_path = "/custom-status"
            config.providers["chat"].api_key_file = "api_keys.local.md"
            save_config(config)
            (data_dir / "api_keys.local.md").write_text("sk-history-clear-secret", encoding="utf-8")
            sidecar = persistent_config_dir(data_dir)
            self.assertTrue((sidecar / "config.json").exists())

            cards = RuntimeCardStore(data_dir)
            persona = cards.apply_action(
                "save-persona",
                {"name": "channel persona", "content": "channel-specific warmth"},
            )["card"]
            cards.apply_action(
                "set-channel-persona",
                {"conversation_id": "private-1", "card_id": persona["card_id"]},
            )
            cards.apply_action(
                "set-channel-skills",
                {"conversation_id": "private-1", "card_ids": ["skill.foreground_dialogue"]},
            )

            from app.personal_wechat_bot.wechat_driver.bridge_send import BridgeAckStatus, BridgeOutboxStore

            bridge = BridgeOutboxStore(data_dir)
            record = bridge.enqueue("private-1", "preserved bridge text", receiver="wxid_friend")
            bridge.append_ack(record["bridge_id"], status=BridgeAckStatus.ACCEPTED, reason="native_accepted_unverified")
            (data_dir / "send_bridge" / "synced_acks.json").write_text(
                json.dumps({"seen": [record["bridge_id"]]}),
                encoding="utf-8",
            )

            ledger = ConversationLedgerStore(data_dir)
            message = NormalizedMessage(
                message_id="history-user-1",
                conversation_id="private-1",
                conversation_type="private",
                chat_title="Alice",
                sender_name="Alice",
                sender_wechat_id="wxid_friend",
                text="old session line",
                is_self=False,
                received_at="2026-07-10T00:00:00+08:00",
                metadata={"session_id": "session_old"},
            )
            ledger.append_message(message)
            memory_dir = ledger.conversation_markdown_path("private-1").parent / "sessions" / "session_old" / "memory"
            memory_dir.mkdir(parents=True, exist_ok=True)
            (memory_dir / "summary.md").write_text("old derived memory", encoding="utf-8")
            ConversationSessionStore(data_dir).reset_session(
                "private-1",
                reason="test_history_clear",
                message_id="history-user-1",
            )
            (data_dir / "file_workspace" / "private-1" / "session_old").mkdir(parents=True, exist_ok=True)
            (data_dir / "agent_workspace" / "old-task").mkdir(parents=True, exist_ok=True)

            result = clear_sidebar_history_data(data_dir)
            after = load_config(data_dir)
            runtime_state = RuntimeCardStore(data_dir).state()

            self.assertEqual(result["status"], "ok")
            self.assertEqual(after.accepted_contacts, {"wxid_friend"})
            self.assertEqual(after.accepted_groups, {"family@chatroom"})
            self.assertEqual(after.search_blocklist, ["ads.example", "login.example"])
            self.assertEqual(after.wechat_native_send_text_path, "/custom-text")
            self.assertEqual(after.wechat_native_status_path, "/custom-status")
            self.assertEqual(after.providers["chat"].api_key_file, "api_keys.local.md")
            self.assertTrue((data_dir / "api_keys.local.md").exists())
            self.assertTrue((sidecar / "config.json").exists())
            self.assertIn(
                "private-1",
                runtime_state["state"]["channel_overrides"],
            )
            self.assertIn("channel-specific warmth", "\n".join(RuntimeCardStore(data_dir).prompt_lines("private-1")))
            self.assertFalse((data_dir / "conversation_ledgers").exists())
            self.assertFalse((data_dir / "conversation_sessions").exists())
            self.assertFalse((data_dir / "file_workspace").exists())
            self.assertFalse((data_dir / "agent_workspace").exists())
            self.assertTrue((data_dir / "send_bridge" / "outbox.jsonl").exists())
            self.assertTrue((data_dir / "send_bridge" / "acks.jsonl").exists())
            self.assertTrue((data_dir / "send_bridge" / "synced_acks.json").exists())
            self.assertIn(str(Path("send_bridge") / "outbox.jsonl"), result["preserved_runtime"])
            self.assertIn(str(Path("send_bridge") / "acks.jsonl"), result["preserved_runtime"])

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

    def test_history_clear_removes_read_only_content_addressed_workspace(self) -> None:
        from app.personal_wechat_bot.workspace.file_workspace import FileWorkspace

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            source = Path(tmp) / "attachment.txt"
            source.write_text("immutable attachment", encoding="utf-8")
            staged = FileWorkspace(data_dir / "file_workspace").stage_file(
                source,
                conversation_id="conv-read-only",
                session_id="session-read-only",
            )

            result = clear_sidebar_history_data(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertFalse((data_dir / "file_workspace").exists())
            self.assertFalse(Path(staged.staged_path).exists())
            self.assertFalse(Path(staged.blob_path).exists())

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

    def test_weflow_json_projection_does_not_repopulate_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            projection_path = data_dir / "weflow_sidebar_state.json"
            projection_path.write_text(
                json.dumps(
                    {
                        "base_url": "http://127.0.0.1:5039",
                        "talkers": ["wxid_alice"],
                        "operation_history": [
                            {
                                "time": "2026-07-10T00:00:00Z",
                                "action": "projection",
                                "status": "ok",
                                "summary": "projection only",
                                "result": {"status": "ok"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            state = sidebar_api.build_sidebar_weflow_state(data_dir)
            sidebar_api._write_weflow_sidebar_state(
                data_dir,
                {"base_url": "http://127.0.0.1:5040", "talkers": ["wxid_current"]},
            )
            projection_path.unlink()
            sqlite_state = sidebar_api.build_sidebar_weflow_state(data_dir)

            self.assertNotEqual(state["base_url"], "http://127.0.0.1:5039")
            self.assertEqual(state["operation_history"], [])
            self.assertEqual(sqlite_state["base_url"], "http://127.0.0.1:5040")
            self.assertEqual(sqlite_state["requested_talkers"], ["wxid_current"])
            self.assertTrue((data_dir / "sidebar_state.sqlite").exists())

    def test_weflow_operation_history_uses_sqlite_under_concurrent_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            errors: list[Exception] = []

            def worker(index: int) -> None:
                try:
                    sidebar_api._append_weflow_operation_history(data_dir, f"tick-{index}", {"status": "ok", "count": index})
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(64)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            state = sidebar_api.build_sidebar_weflow_state(data_dir)

            self.assertEqual(errors, [])
            self.assertTrue((data_dir / "sidebar_state.sqlite").exists())
            self.assertTrue((data_dir / "weflow_sidebar_state.json").exists())
            self.assertEqual(len(state["operation_history"]), 50)
            self.assertTrue(all(item["action"].startswith("tick-") for item in state["operation_history"]))

    def test_history_clear_resets_weflow_runtime_traces_but_keeps_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            sidebar_api._write_weflow_sidebar_state(
                data_dir,
                {
                    "base_url": "http://127.0.0.1:5039",
                    "talkers": ["wxid_alice"],
                    "last_health": {"status": "ok", "fork_ok": True},
                    "last_pull": {"status": "ok", "count": 2},
                    "pull_job": {"job_id": "pull-old", "status": "completed"},
                    "operation_history": [
                        {
                            "time": "2026-07-10T00:00:00Z",
                            "action": "old",
                            "status": "ok",
                            "summary": "old runtime trace",
                            "result": {"status": "ok"},
                        }
                    ],
                },
            )

            result = clear_sidebar_history_data(data_dir)
            state = sidebar_api.build_sidebar_weflow_state(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(state["base_url"], "http://127.0.0.1:5039")
            self.assertEqual(state["requested_talkers"], ["wxid_alice"])
            self.assertEqual(state["last_health"], {})
            self.assertEqual(state["last_pull"], {})
            self.assertEqual(state["pull_job"], {})
            self.assertEqual(state["operation_history"], [])
            self.assertTrue((data_dir / "sidebar_state.sqlite").exists())

    def test_history_clear_blocks_while_history_writer_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            event_file = data_dir / "backend_events.jsonl"
            event_file.write_text("{}\n", encoding="utf-8")
            stop = threading.Event()
            thread = threading.Thread(target=stop.wait, daemon=True)
            thread.start()
            key = str(data_dir.resolve())
            try:
                with mock.patch.dict(
                    sidebar_api._AGENT_WORKERS,
                    {
                        key: {
                            "thread": thread,
                            "last_status": "running",
                            "event_file": str(event_file),
                            "requested_conversation_ids": ["conv-active"],
                        }
                    },
                    clear=True,
                ):
                    blocked = clear_sidebar_history_data(data_dir)
                    self.assertTrue(event_file.exists())
                    forced = clear_sidebar_history_data(data_dir, {"source": "shutdown_helper"})
            finally:
                stop.set()
                thread.join(timeout=1.0)

            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["reason"], "history_clear_runtime_active")
            self.assertEqual(blocked["active_workers"][0]["worker"], "dialog_agent")
            self.assertEqual(forced["status"], "ok")
            self.assertFalse(event_file.exists())

    def test_history_clear_blocks_while_cross_process_agent_tick_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            event_file = data_dir / "backend_events.jsonl"
            event_file.write_text("{}\n", encoding="utf-8")
            lock = sidebar_api.ProcessLock(
                data_dir / "runtime_locks" / "sidebar_agent_tick.lock",
                label="test_agent_tick",
                stale_after_seconds=3600,
            )
            lock.acquire()
            try:
                result = clear_sidebar_history_data(data_dir)
            finally:
                lock.release()

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason"], "history_clear_runtime_active")
            self.assertEqual(result["active_workers"][0]["worker"], "dialog_agent_tick")
            self.assertTrue(event_file.exists())

    def test_history_clear_blocks_while_external_send_bridge_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            event_file = data_dir / "backend_events.jsonl"
            event_file.write_text("{}\n", encoding="utf-8")
            lock = sidebar_api.ProcessLock(
                sidebar_api.bridge_worker_lock_path(data_dir),
                label="send_bridge_worker",
                stale_after_seconds=sidebar_api.BRIDGE_WORKER_LOCK_STALE_SECONDS,
                metadata={"backend_name": "dry_run", "data_dir": str(data_dir.resolve())},
            )
            lock.acquire()
            try:
                result = clear_sidebar_history_data(data_dir)
            finally:
                lock.release()

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason"], "history_clear_runtime_active")
            self.assertEqual(result["active_workers"][0]["worker"], "send_bridge")
            self.assertEqual(result["active_workers"][0]["source"], "external_process")
            self.assertTrue(event_file.exists())

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

    def test_diagnostics_export_captures_core_state_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = create_default_config(data_dir)
            config.weflow_token_env = "SECRET_WEFLOW_TOKEN"
            save_config(config)
            os.environ["SECRET_WEFLOW_TOKEN"] = "super-secret-token"
            self.addCleanup(lambda: os.environ.pop("SECRET_WEFLOW_TOKEN", None))
            (data_dir / "backend_events.jsonl").write_text(
                json.dumps({"message_id": "m1", "token": "do-not-leak", "text": "hello"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (data_dir / "send_audit.jsonl").write_text(
                json.dumps({"action": "probe", "api_key": "sk-secret"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            export = sidebar_diagnostics_export(data_dir, {"limit": 5})
            serialized = json.dumps(export, ensure_ascii=False)

            self.assertEqual(export["status"], "ok")
            self.assertEqual(export["schema"], "sidebar_diagnostics_export_v1")
            self.assertIn("task_manager", export)
            self.assertIn("send_bridge", export)
            self.assertIn("readiness", export)
            self.assertIn("driver_probe", export)
            self.assertIn("storage_migration", export)
            self.assertEqual(export["storage_migration"]["schema"], "storage_migration_status_v1")
            self.assertIn("recent_backend_events", export)
            self.assertTrue(Path(export["export_path"]).exists())
            self.assertNotIn("super-secret-token", serialized)
            self.assertNotIn("do-not-leak", serialized)
            self.assertNotIn("sk-secret", serialized)
            self.assertEqual(export["recent_backend_events"][0]["token"], "<redacted>")
            self.assertEqual(export["recent_send_audit_raw"][0]["api_key"], "<redacted>")

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
            self.assertIn("send_bridge/outbox.jsonl", result["preserved_runtime_policy"])

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
            key_pool = ApiKeyPool(config.providers["chat"], data_dir)
            store = ConversationChannelStore(
                data_dir,
                key_pool,
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            normalizer = MessageNormalizer()
            visible = normalizer.normalize(RawWeChatMessage("1", "PAGE", "PAGE", "hello", driver_meta={"source": "backend_events_jsonl"}))
            noisy = normalizer.normalize(RawWeChatMessage("2", "+25", "+25", "8/10/16", driver_meta={"source": "windows_snapshot"}))
            assert visible is not None and noisy is not None
            store.ensure_channel(visible)
            store.ensure_channel(noisy)

            state = build_sidebar_state(data_dir)

            self.assertEqual(state["channels"]["count"], 1)
            self.assertEqual(state["channels"]["hidden_count"], 1)
            self.assertEqual(state["channels"]["items"][0]["chat_title"], "PAGE")

    def test_sidebar_channel_state_hides_unaccepted_emoji_only_private_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            key_pool = ApiKeyPool(config.providers["chat"], data_dir)
            store = ConversationChannelStore(
                data_dir,
                key_pool,
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            message = MessageNormalizer().normalize(
                RawWeChatMessage(
                    "emoji-1",
                    "🎧",
                    "🎧",
                    "hello",
                    sender_wechat_id="wxid_emoji_friend",
                    driver_meta={
                        "source": "weflow_discovery",
                        "trusted_channel_source": True,
                        "conversation_key": "wxid_emoji_friend",
                    },
                )
            )
            assert message is not None
            store.ensure_channel(message)

            state = build_sidebar_state(data_dir)

            self.assertEqual(state["channels"]["count"], 0)
            self.assertEqual(state["channels"]["hidden_count"], 1)
            self.assertEqual(state["channels"]["hidden_reasons"]["private_contact_unknown_or_unidentified"], 1)

    def test_sidebar_channel_state_keeps_accepted_emoji_only_private_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            config.accepted_contacts.add("wxid_emoji_friend")
            save_config(config)
            key_pool = ApiKeyPool(config.providers["chat"], data_dir)
            store = ConversationChannelStore(
                data_dir,
                key_pool,
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            message = MessageNormalizer().normalize(
                RawWeChatMessage(
                    "emoji-accepted-1",
                    "🎧",
                    "🎧",
                    "hello",
                    sender_wechat_id="wxid_emoji_friend",
                    driver_meta={
                        "source": "weflow_discovery",
                        "trusted_channel_source": True,
                        "conversation_key": "wxid_emoji_friend",
                    },
                )
            )
            assert message is not None
            store.ensure_channel(message)

            state = build_sidebar_state(data_dir)

            self.assertEqual(state["channels"]["count"], 1)
            self.assertEqual(state["channels"]["hidden_count"], 0)
            self.assertEqual(state["channels"]["items"][0]["chat_title"], "🎧")

    def test_sidebar_channel_state_hides_weflow_symbol_title_without_friend_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            key_pool = ApiKeyPool(config.providers["chat"], data_dir)
            store = ConversationChannelStore(
                data_dir,
                key_pool,
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            message = MessageNormalizer().normalize(
                RawWeChatMessage(
                    "symbol-1",
                    "!",
                    "!",
                    "hello",
                    sender_wechat_id="wxid_symbol_friend",
                    driver_meta={"source": "weflow_discovery", "conversation_key": "wxid_symbol_friend"},
                )
            )
            assert message is not None
            channel = store.ensure_channel(message)
            channel_path = next((data_dir / "conversation_channels").glob("*/channel.json"))
            payload = json.loads(channel_path.read_text(encoding="utf-8"))
            payload["trusted_channel_source"] = False
            payload["source_names"] = ["weflow_discovery"]
            store.registry.upsert(payload)
            channel_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            state = build_sidebar_state(data_dir)

            self.assertEqual(channel.chat_title, "!")
            self.assertEqual(state["channels"]["count"], 0)
            self.assertEqual(state["channels"]["hidden_count"], 1)
            self.assertEqual(state["channels"]["hidden_reasons"]["private_contact_unknown_or_unidentified"], 1)

    def test_router_does_not_register_windows_snapshot_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            key_pool = ApiKeyPool(config.providers["chat"], data_dir)
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

            self.assertEqual(decision.action, "ignore")
            self.assertEqual(decision.reason, "untrusted_channel_source_blocked:windows_snapshot")
            self.assertEqual(store.list_channels(), [])

    def test_sidebar_hides_untrusted_channels_but_keeps_trusted_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            key_pool = ApiKeyPool(config.providers["chat"], data_dir)
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
            store.registry.upsert(
                {
                    "conversation_id": stale.conversation_id,
                    "conversation_type": "private",
                    "chat_title": "PTURE",
                    "segment": stale.conversation_id,
                    "status": "active",
                    "key_slots": 1,
                    "api_key_refs": [],
                    "session_scope": "per_conversation_current_session",
                    "sender_names": ["PTURE"],
                    "sender_wechat_ids": [],
                    "source_names": [],
                    "trusted_channel_source": False,
                    "created_at": "2026-06-30T00:00:00+00:00",
                    "updated_at": "2026-06-30T00:00:00+00:00",
                }
            )

            state = build_sidebar_state(data_dir)

            self.assertEqual([item["chat_title"] for item in state["channels"]["items"]], ["PAGE"])
            self.assertEqual(state["channels"]["hidden_reasons"]["untrusted_channel"], 1)

    def test_sidebar_hides_trusted_unidentified_private_weflow_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("private", "wxid_ghost")
            config = load_config(data_dir)
            store = ConversationChannelStore(
                data_dir,
                ApiKeyPool(config.providers["chat"], data_dir),
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            store.registry.upsert(
                {
                        "conversation_id": conversation_id,
                        "conversation_type": "private",
                        "chat_title": "馃帶",
                        "segment": "wxid_ghost_current",
                        "status": "active",
                        "key_slots": 1,
                        "api_key_refs": [],
                        "session_scope": "per_conversation_current_session",
                        "backend_dir": "",
                        "context_dir": "",
                        "file_workspace_dir": "",
                        "sender_names": ["wxid_ghost", "馃帶"],
                        "sender_wechat_ids": ["wxid_ghost"],
                        "conversation_key": "wxid_ghost",
                        "source_names": ["weflow_discovery", "backend_events_jsonl"],
                        "trusted_channel_source": True,
                        "created_at": "2026-07-08T00:00:00+00:00",
                        "updated_at": "2026-07-08T00:00:00+00:00",
                        "next_key_index": 0,
                    }
            )

            state = build_sidebar_state(data_dir)

            self.assertEqual(state["channels"]["count"], 0)
            self.assertEqual(state["channels"]["hidden_count"], 1)
            self.assertEqual(state["channels"]["hidden_reasons"]["private_contact_unknown_or_unidentified"], 1)
            self.assertEqual(state["send_bridge"]["channels"], [])

    def test_sidebar_cleanup_hidden_channels_deletes_only_hidden_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            key_pool = ApiKeyPool(config.providers["chat"], data_dir)
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
                RawWeChatMessage("2", "+25", "+25", "8/10/16", driver_meta={"source": "windows_snapshot"})
            )
            assert trusted is not None and hidden is not None
            store.ensure_channel(trusted)
            hidden_channel = store.ensure_channel(hidden)
            ledger_file = data_dir / "conversation_ledgers" / hidden_channel.segment / "conversation.md"
            ledger_file.parent.mkdir(parents=True, exist_ok=True)
            ledger_file.write_text("keep", encoding="utf-8")

            result = cleanup_sidebar_channels(data_dir)
            state = build_sidebar_state(data_dir)

            self.assertEqual(result["deleted_conversation_ids"], [hidden.conversation_id])
            self.assertEqual(result["cleanups"][0]["cleanup_policy"], "non_wechat_purge")
            self.assertEqual(state["channels"]["count"], 1)
            self.assertEqual(state["channels"]["hidden_count"], 0)
            self.assertIsNotNone(store.get_channel(trusted.conversation_id))
            self.assertIsNone(store.get_channel(hidden.conversation_id))
            self.assertFalse(ledger_file.exists())

    def test_sidebar_delete_untrusted_channel_fully_purges_associated_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = "untrusted-noise"
            segment = "NOISE_untruste"
            channel_file = data_dir / "conversation_channels" / segment / "channel.json"
            ledger_dir = data_dir / "conversation_ledgers" / segment
            workspace_dir = data_dir / "file_workspace" / segment
            session_dir = data_dir / "conversation_sessions" / segment
            channel_file.parent.mkdir(parents=True, exist_ok=True)
            ledger_dir.mkdir(parents=True, exist_ok=True)
            workspace_dir.mkdir(parents=True, exist_ok=True)
            session_dir.mkdir(parents=True, exist_ok=True)
            config = load_config(data_dir)
            store = ConversationChannelStore(
                data_dir,
                ApiKeyPool(config.providers["chat"], data_dir),
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            payload = {
                "conversation_id": conversation_id,
                "conversation_type": "private",
                "chat_title": "NOISE",
                "segment": segment,
                "status": "active",
                "key_slots": 1,
                "api_key_refs": [],
                "session_scope": "per_conversation_current_session",
                "sender_names": ["NOISE"],
                "sender_wechat_ids": [],
                "source_names": [],
                "trusted_channel_source": False,
            }
            store.registry.upsert(payload)
            channel_file.write_text(json.dumps(payload), encoding="utf-8")
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
                ApiKeyPool(config.providers["chat"], data_dir),
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
                ApiKeyPool(config.providers["chat"], data_dir),
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
                        "is_friend": True,
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

    def test_sidebar_bridge_items_are_grouped_by_conversation_channel(self) -> None:
        from app.personal_wechat_bot.wechat_driver.bridge_send import BridgeAckStatus, BridgeOutboxStore

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            _ensure_test_channel(data_dir, conversation_id="private-1", sender_wechat_id="wxid_alice", chat_title="Alice")
            _ensure_test_channel(data_dir, conversation_id="private-2", sender_wechat_id="wxid_bob", chat_title="Bob")
            store = BridgeOutboxStore(data_dir)
            alice = store.enqueue("private-1", "hello alice", receiver="wxid_alice")
            bob = store.enqueue("private-2", "hello bob", receiver="wxid_bob")
            store.append_ack(bob["bridge_id"], status=BridgeAckStatus.ACCEPTED, reason="native_accepted_unverified")

            state = build_sidebar_bridge_state(data_dir)

            grouped = {item["conversation_id"]: item for item in state["item_channels"]}
            self.assertEqual(grouped["private-1"]["display_name"], "Alice")
            self.assertEqual(grouped["private-1"]["status_counts"]["queued"], 1)
            self.assertEqual(grouped["private-1"]["items"][0]["bridge_id"], alice["bridge_id"])
            self.assertEqual(grouped["private-2"]["display_name"], "Bob")
            self.assertEqual(grouped["private-2"]["status_counts"]["accepted"], 1)
            self.assertEqual(grouped["private-2"]["items"][0]["bridge_id"], bob["bridge_id"])

    def test_sidebar_send_audit_is_grouped_by_channel_and_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            _ensure_test_channel(data_dir, conversation_id="private-1", sender_wechat_id="wxid_alice", chat_title="Alice")
            _ensure_test_channel(data_dir, conversation_id="private-2", sender_wechat_id="wxid_bob", chat_title="Bob")
            audit = SendAuditLog(data_dir / "send_audit.jsonl")
            audit.append("queue_pending", queue_id="q-pending", status="pending", payload={"conversation_id": "private-1"})
            audit.append("confirm_approve", queue_id="q-approved", status="approved", payload={"conversation_id": "private-1"})
            audit.append(
                "confirm_send_attempt",
                queue_id="q-queued",
                status="queued_to_bridge",
                payload={"conversation_id": "private-1", "bridge_id": "bridge-private-1"},
            )
            audit.append("confirm_reject", queue_id="q-rejected", status="rejected", payload={"conversation_id": "private-1"})
            audit.append(
                "confirm_send_blocked",
                queue_id="q-blocked",
                status="blocked",
                reason="receiver_not_admitted",
                payload={"conversation_id": "private-1"},
            )
            audit.append(
                "bridge_ack_sync",
                queue_id="q-accepted",
                status="accepted",
                payload={"conversation_id": "private-2", "ack_status": "accepted", "bridge_id": "bridge-private-2a"},
            )
            audit.append(
                "bridge_ack_sync",
                queue_id="q-sent",
                status="sent",
                payload={"conversation_id": "private-2", "ack_status": "sent", "bridge_id": "bridge-private-2s"},
            )
            audit.append(
                "confirm_send_attempt",
                queue_id="q-failed",
                status="failed",
                reason="native_http_failed",
                payload={"conversation_id": "private-2"},
            )
            audit.append(
                "ledger_sync_recovered",
                queue_id="q-recovered",
                status="",
                payload={"conversation_id": "private-2"},
            )
            audit.append(
                "operator_note",
                queue_id="q-other",
                status="",
                payload={"conversation_id": "private-2"},
            )

            state = build_sidebar_state(data_dir)
            channels = {item["conversation_id"]: item for item in state["audit"]["channels"]}

            self.assertEqual(channels["private-1"]["display_name"], "Alice")
            self.assertEqual(channels["private-1"]["phase_counts"]["pending"], 1)
            self.assertEqual(channels["private-1"]["phase_counts"]["approved"], 1)
            self.assertEqual(channels["private-1"]["phase_counts"]["queued_to_bridge"], 1)
            self.assertEqual(channels["private-1"]["phase_counts"]["rejected"], 1)
            self.assertEqual(channels["private-1"]["phase_counts"]["blocked"], 1)
            self.assertEqual(channels["private-2"]["display_name"], "Bob")
            self.assertEqual(channels["private-2"]["phase_counts"]["accepted"], 1)
            self.assertEqual(channels["private-2"]["phase_counts"]["sent"], 1)
            self.assertEqual(channels["private-2"]["phase_counts"]["failed"], 1)
            self.assertEqual(channels["private-2"]["phase_counts"]["resolved"], 1)
            self.assertEqual(channels["private-2"]["phase_counts"]["other"], 1)
            self.assertEqual(state["audit"]["phases"]["resolved"]["count"], 1)
            self.assertEqual(
                state["audit"]["phases"]["resolved"]["channels"][0]["items"][0]["conversation_id"],
                "private-2",
            )

    def test_sidebar_state_projects_channel_state_files_and_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            channel_store = ConversationChannelStore(
                data_dir,
                ApiKeyPool(config.providers["chat"], data_dir),
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
                ApiKeyPool(config.providers["chat"], data_dir),
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
            def run_once(self_inner, *, process_imported: bool = True) -> dict:
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
                "process_backend_events": True,
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

    def test_weflow_background_start_defaults_to_capture_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            captured: dict[str, Any] = {}
            stop_event = threading.Event()

            def fake_loop(root, payload, stop):
                captured.update(payload)
                stop_event.set()

            with mock.patch.object(sidebar_api, "_weflow_background_loop", side_effect=fake_loop), mock.patch.object(
                sidebar_api, "_start_bridge_worker"
            ):
                result = sidebar_api.sidebar_weflow_start(data_dir, {"interval_seconds": 1})
                stop_event.wait(1)
                sidebar_api.sidebar_weflow_stop(data_dir, {})

            self.assertEqual(result["status"], "ok")
            self.assertTrue(captured["capture_only"])
            self.assertFalse(captured["process_backend_events"])

    def test_weflow_pull_tick_can_import_without_processing_backend_events(self) -> None:
        calls: list[bool] = []

        class FakeRunner:
            def run_once(self, *, process_imported: bool = True) -> dict:
                calls.append(process_imported)
                return {
                    "status": "ok",
                    "processed_count": 0,
                    "processed": [],
                    "poll": {"skipped_reason": "capture_only_handoff_to_dialog_agent"},
                }

        class FakeSource:
            status = "ok"
            scanned_count = 1
            appended_count = 1

        fake_bridge = mock.Mock()
        fake_bridge.base_url = "http://127.0.0.1:5031"
        fake_bridge.pull_once.return_value = FakeSource()
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
                "process_backend_events": False,
                "hook_event_file": "hook.jsonl",
                "backend_event_file": "backend.jsonl",
            },
            "bridge": fake_bridge,
            "runner": FakeRunner(),
            "weflow_ready": {},
            "media_roots": [],
        }

        result = sidebar_api._run_weflow_pull_tick(context)

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["process_backend_events"])
        self.assertEqual(calls, [False])

    def test_weflow_pull_once_blocks_unidentified_requested_talker_in_local_library(self) -> None:
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
            cached = next(item for item in sessions if item["id"] == "wxid_user")
            self.assertEqual(cached["channel_registration_status"], "blocked")
            self.assertFalse(any(item["conversation_key"] == "wxid_user" for item in channels))

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
                    "source_payload": {"conversation_key": "wxid_alice", "talker_id": "wxid_alice", "is_friend": True},
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
            self.assertTrue(result["agent"]["cursor"]["read_offset"] > 0)
            self.assertFalse(result["agent"]["cursor_restored"])
            self.assertEqual(result["agent_state"]["last_tick"]["processed_count"], 1)

            second = sidebar_api.sidebar_agent_tick(data_dir, {})

            self.assertEqual(second["status"], "ok")
            self.assertEqual(second["agent"]["processed_count"], 0)
            self.assertTrue(second["agent"]["cursor_restored"])
            self.assertEqual(len(ConversationLedgerStore(data_dir).read_entries(conversation_id)), len(entries))

    def test_sidebar_agent_tick_without_scope_keeps_global_pending_scan_after_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            ledger = ConversationLedgerStore(data_dir)
            alice_id = conversation_id_for("private", "wxid_alice")
            bob_id = conversation_id_for("private", "wxid_bob")
            ledger.append_message(
                NormalizedMessage(
                    message_id="bob-pending-1",
                    conversation_id=bob_id,
                    conversation_type="private",
                    chat_title="Bob",
                    sender_name="Bob",
                    sender_wechat_id="wxid_bob",
                    text="Bob pending hello",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test", "context_only": True},
                )
            )
            append_sidebar_backend_event(
                data_dir,
                {
                    "raw_id": "agent-tick-alice-1",
                    "chat_title": "Alice",
                    "sender_name": "Alice",
                    "sender_wechat_id": "wxid_alice",
                    "text": "Alice new event",
                    "observed_at": "2026-07-08T09:01:00+08:00",
                    "source_payload": {"conversation_key": "wxid_alice", "talker_id": "wxid_alice", "is_friend": True},
                },
            )

            result = sidebar_api.sidebar_agent_tick(data_dir, {})

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["agent"]["processed_count"], 1)
            self.assertEqual(result["agent"]["proactive_reply_count"], 1)
            self.assertEqual(result["session_snapshot"]["after"]["pending_user_count"], 0)
            self.assertEqual(ledger.read_entries(alice_id)[-1].role, "assistant")
            self.assertEqual(ledger.read_entries(bob_id)[-1].role, "assistant")
            self.assertEqual(set(result["agent"]["processed_conversation_ids"]), {alice_id, bob_id})

    def test_sidebar_agent_tick_scoped_cursor_does_not_consume_other_talkers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            ledger = ConversationLedgerStore(data_dir)
            alice_id = conversation_id_for("private", "wxid_scope_alice")
            bob_id = conversation_id_for("private", "wxid_scope_bob")
            append_sidebar_backend_event(
                data_dir,
                {
                    "raw_id": "scoped-agent-alice-1",
                    "chat_title": "Scope Alice",
                    "sender_name": "Scope Alice",
                    "sender_wechat_id": "wxid_scope_alice",
                    "text": "Alice scoped event",
                    "observed_at": "2026-07-08T09:01:00+08:00",
                    "source_payload": {
                        "conversation_key": "wxid_scope_alice",
                        "talker_id": "wxid_scope_alice",
                        "is_friend": True,
                    },
                },
            )
            append_sidebar_backend_event(
                data_dir,
                {
                    "raw_id": "scoped-agent-bob-1",
                    "chat_title": "Scope Bob",
                    "sender_name": "Scope Bob",
                    "sender_wechat_id": "wxid_scope_bob",
                    "text": "Bob scoped event",
                    "observed_at": "2026-07-08T09:02:00+08:00",
                    "source_payload": {
                        "conversation_key": "wxid_scope_bob",
                        "talker_id": "wxid_scope_bob",
                        "is_friend": True,
                    },
                },
            )

            alice = sidebar_api.sidebar_agent_tick(
                data_dir,
                {"talkers": ["wxid_scope_alice"], "conversation_ids": [alice_id]},
            )
            bob = sidebar_api.sidebar_agent_tick(
                data_dir,
                {"talkers": ["wxid_scope_bob"], "conversation_ids": [bob_id]},
            )
            alice_repeat = sidebar_api.sidebar_agent_tick(
                data_dir,
                {"talkers": ["wxid_scope_alice"], "conversation_ids": [alice_id]},
            )

            self.assertEqual(alice["agent"]["processed_count"], 1)
            self.assertEqual(alice["agent"]["processed_conversation_ids"], [alice_id])
            self.assertEqual(bob["agent"]["processed_count"], 1)
            self.assertEqual(bob["agent"]["processed_conversation_ids"], [bob_id])
            self.assertEqual(alice_repeat["agent"]["processed_count"], 0)
            self.assertEqual(ledger.read_entries(alice_id)[-1].role, "assistant")
            self.assertEqual(ledger.read_entries(bob_id)[-1].role, "assistant")
            self.assertEqual(alice["agent_state"]["event_file_count"], 1)
            self.assertEqual(bob["agent_state"]["event_file_count"], 2)

    def test_agent_requested_conversation_ids_accepts_channel_display_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            conversation_id = conversation_id_for("private", "wxid_alias_friend")
            runtime.channel_store.ensure_channel(
                NormalizedMessage(
                    message_id="alias-channel-1",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Display Alias",
                    sender_name="Sender Alias",
                    sender_wechat_id="wxid_alias_friend",
                    text="hello",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "backend_events_jsonl"},
                )
            )

            by_title = sidebar_api._agent_requested_conversation_ids(data_dir, {"talkers": ["Display Alias"]})
            by_sender = sidebar_api._agent_requested_conversation_ids(data_dir, {"talkers": ["Sender Alias"]})

            self.assertEqual(by_title, [conversation_id])
            self.assertEqual(by_sender, [conversation_id])

    def test_sidebar_agent_tick_survives_missing_task_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            with mock.patch.object(sidebar_api.TaskStatusStore, "transition", side_effect=KeyError("task not found")):
                result = sidebar_api.sidebar_agent_tick(data_dir, {})

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["agent"]["processed_count"], 0)
            self.assertEqual(result["agent_state"]["last_tick"]["status"], "ok")

    def test_sidebar_agent_tick_is_serialized_across_concurrent_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            active = 0
            max_active = 0
            guard = threading.Lock()
            errors: list[BaseException] = []

            def fake_run(_runner, max_loops=1):
                nonlocal active, max_active
                with guard:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with guard:
                    active -= 1
                return {"status": "ok", "processed_count": 0, "processed": []}

            def worker() -> None:
                try:
                    sidebar_api.sidebar_agent_tick(data_dir, {})
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch("app.personal_wechat_bot.control.sidebar_api.PollingRunner.run_forever", fake_run):
                threads = [threading.Thread(target=worker) for _ in range(4)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)

            self.assertEqual(errors, [])
            self.assertEqual(max_active, 1)

    def test_sidebar_agent_tick_replies_to_pending_context_only_private_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("private", "wxid_live_friend")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="context-only-live-1",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Live Friend",
                    sender_name="Live Friend",
                    sender_wechat_id="wxid_live_friend",
                    text="我通过了你的朋友验证请求，现在我们可以开始聊天了",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test", "context_only": True},
                )
            )

            result = sidebar_api.sidebar_agent_tick(data_dir, {})

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["agent"]["processed_count"], 0)
            self.assertEqual(result["agent"]["proactive_reply_count"], 1)
            after = result["session_snapshot"]["after"]
            self.assertEqual(after["pending_user_count"], 0)
            entries = ledger.read_entries(conversation_id)
            self.assertEqual(entries[-1].role, "assistant")
            second = sidebar_api.sidebar_agent_tick(data_dir, {})
            self.assertEqual(second["agent"]["proactive_reply_count"], 0)
            self.assertEqual(len(ledger.read_entries(conversation_id)), len(entries))

    def test_sidebar_agent_tick_never_replies_to_history_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("private", "wxid_history_friend")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="history-only-1",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="History Friend",
                    sender_name="History Friend",
                    sender_wechat_id="wxid_history_friend",
                    text="old unanswered message",
                    is_self=False,
                    received_at="2025-07-08T09:00:00+08:00",
                    metadata={
                        "source": "backend_events_jsonl",
                        "context_only": True,
                        "capture_phase": "history_backfill",
                    },
                )
            )

            result = sidebar_api.sidebar_agent_tick(data_dir, {})

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["agent"]["proactive_reply_count"], 0)
            self.assertEqual(result["session_snapshot"]["after"]["pending_user_count"], 0)
            self.assertEqual([entry.role for entry in ledger.read_entries(conversation_id)], ["user"])

    def test_sidebar_agent_tick_ignores_old_session_pending_after_context_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("private", "wxid_reset_friend")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="old-session-pending-user",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Reset Friend",
                    sender_name="Reset Friend",
                    sender_wechat_id="wxid_reset_friend",
                    text="this was pending before reset",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test", "context_only": True},
                )
            )
            new_session_id = ConversationSessionStore(data_dir).reset_session(
                conversation_id,
                reason="test_context_reset",
                message_id="manual-reset",
            )

            result = sidebar_api.sidebar_agent_tick(data_dir, {})

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["agent"]["processed_count"], 0)
            self.assertEqual(result["agent"]["proactive_reply_count"], 0)
            before = result["session_snapshot"]["before"]["conversations"][0]
            after = result["session_snapshot"]["after"]["conversations"][0]
            self.assertEqual(before["session_id"], new_session_id)
            self.assertEqual(before["previous_session_id"], "session_default")
            self.assertEqual(before["session_reset_count"], 1)
            self.assertEqual(before["session_reset_reason"], "test_context_reset")
            self.assertEqual(before["session_reset_message_id"], "manual-reset")
            self.assertTrue(before["session_started_at"])
            self.assertEqual(before["entry_count"], 0)
            self.assertEqual(before["total_entry_count"], 1)
            self.assertEqual(before["pending_user_count_since_last_assistant"], 0)
            self.assertEqual(before["pending_user_messages"], [])
            self.assertEqual(after["pending_user_count_since_last_assistant"], 0)
            self.assertEqual(len(ledger.read_entries(conversation_id)), 1)

    def test_sidebar_agent_tick_ignores_session_reset_control_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("private", "wxid_reset_control")
            ledger = ConversationLedgerStore(data_dir)
            new_session_id = ConversationSessionStore(data_dir).reset_session(
                conversation_id,
                reason="clear_current_context_command",
                message_id="clear-context-message",
            )
            ledger.append_message(
                NormalizedMessage(
                    message_id="clear-context-message",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Reset Control",
                    sender_name="Reset Control",
                    sender_wechat_id="wxid_reset_control",
                    text="@bot 清空当前对话上下文",
                    is_self=False,
                    received_at="2026-07-08T09:01:00+08:00",
                    metadata={
                        "source": "test",
                        "session_id": new_session_id,
                        "context_only": True,
                        "control_event": "session_reset",
                        "reset_session_id": new_session_id,
                    },
                )
            )

            result = sidebar_api.sidebar_agent_tick(data_dir, {})

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["agent"]["processed_count"], 0)
            self.assertEqual(result["agent"]["proactive_reply_count"], 0)
            before = result["session_snapshot"]["before"]["conversations"][0]
            self.assertEqual(before["session_id"], new_session_id)
            self.assertEqual(before["entry_count"], 1)
            self.assertEqual(before["pending_user_count_since_last_assistant"], 0)
            self.assertEqual(before["pending_user_messages"], [])
            self.assertEqual(before["participant_count"], 0)
            self.assertEqual(before["message_aggregation"]["status"], "settled")
            self.assertEqual(before["last_user_message"], "")

    def test_sidebar_agent_tick_tracks_proactive_confirm_waiting_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=False, driver="bridge_outbox")
            conversation_id = conversation_id_for("private", "wxid_confirm_friend")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="confirm-pending-user-1",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Confirm Friend",
                    sender_name="Confirm Friend",
                    sender_wechat_id="wxid_confirm_friend",
                    text="这个问题需要你现在接一下",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test", "context_only": True},
                )
            )

            result = sidebar_api.sidebar_agent_tick(data_dir, {})

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["agent"]["proactive_reply_count"], 1)
            self.assertEqual(result["session_snapshot"]["after"]["pending_user_count"], 0)
            conversation = result["session_snapshot"]["after"]["conversations"][0]
            self.assertEqual(conversation["topic_lifecycle"]["status"], "responded")
            self.assertIn("queued_for_confirm", conversation["topic_lifecycle"]["reason"])
            entries = ledger.read_entries(conversation_id)
            self.assertEqual(entries[-1].role, "assistant")
            self.assertEqual(entries[-1].send["status"], "queued_for_confirm")
            self.assertEqual(ConfirmQueue(data_dir / "confirm_queue.jsonl").list_by_status("pending")[0]["reply"]["conversation_id"], conversation_id)

    def test_sidebar_agent_tick_tracks_proactive_auto_bridge_waiting_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(
                data_dir,
                mode="auto",
                enabled=True,
                driver="bridge_outbox",
                backend="dry_run",
                confirm_required=False,
            )
            conversation_id = conversation_id_for("private", "wxid_auto_bridge_friend")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="auto-bridge-pending-user-1",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Auto Bridge Friend",
                    sender_name="Auto Bridge Friend",
                    sender_wechat_id="wxid_auto_bridge_friend",
                    text="are you there?",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test", "context_only": True},
                )
            )

            result = sidebar_api.sidebar_agent_tick(data_dir, {})

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["agent"]["proactive_reply_count"], 1)
            self.assertEqual(result["session_snapshot"]["after"]["pending_user_count"], 0)
            conversation = result["session_snapshot"]["after"]["conversations"][0]
            self.assertEqual(conversation["topic_lifecycle"]["status"], "responded")
            self.assertIn("queued_to_bridge", conversation["topic_lifecycle"]["reason"])
            entries = ledger.read_entries(conversation_id)
            self.assertEqual(entries[-1].role, "assistant")
            self.assertEqual(entries[-1].send["status"], "queued_to_bridge")
            bridge = sidebar_api.build_sidebar_bridge_state(data_dir)
            grouped = {item["conversation_id"]: item for item in bridge["item_channels"]}
            self.assertEqual(grouped[conversation_id]["status_counts"]["queued"], 1)

    def test_sidebar_agent_tick_replies_to_all_pending_private_channels_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            ledger = ConversationLedgerStore(data_dir)
            conversation_ids: list[str] = []
            for index in range(5):
                talker = f"wxid_live_friend_{index}"
                conversation_id = conversation_id_for("private", talker)
                conversation_ids.append(conversation_id)
                ledger.append_message(
                    NormalizedMessage(
                        message_id=f"context-only-live-{index}",
                        conversation_id=conversation_id,
                        conversation_type="private",
                        chat_title=f"Live Friend {index}",
                        sender_name=f"Live Friend {index}",
                        sender_wechat_id=talker,
                        text=f"第 {index} 个待接私聊",
                        is_self=False,
                        received_at=f"2026-07-08T09:0{index}:00+08:00",
                        metadata={"source": "test", "context_only": True},
                    )
                )

            result = sidebar_api.sidebar_agent_tick(data_dir, {})

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["agent"]["processed_count"], 0)
            self.assertEqual(result["agent"]["proactive_attempt_count"], 5)
            self.assertEqual(result["agent"]["proactive_reply_count"], 5)
            self.assertEqual(set(result["agent"]["processed_conversation_ids"]), set(conversation_ids))
            self.assertEqual(result["session_snapshot"]["after"]["pending_user_count"], 0)
            for conversation_id in conversation_ids:
                self.assertEqual(ledger.read_entries(conversation_id)[-1].role, "assistant")

    def test_sidebar_agent_tick_does_not_repeat_failed_unseen_reply_until_new_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("private", "wxid_live_friend")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="pending-user-1",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Live Friend",
                    sender_name="Live Friend",
                    sender_wechat_id="wxid_live_friend",
                    text="你在吗",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test", "context_only": True},
                )
            )
            failed_reply = ReplyCandidate(
                message_id="failed-reply-1",
                conversation_id=conversation_id,
                text="在的",
                send_mode="auto",
                model="fake",
            )
            failed_entry = ledger.append_reply(
                failed_reply,
                chat_title="Live Friend",
                conversation_type="private",
                session_id="session_default",
            )
            ledger.update_reply_send_result(
                conversation_id,
                failed_entry.entry_id,
                {"status": "failed", "reason": "wechat_native_backend_unavailable:ConnectionRefusedError:refused", "message_id": "bridge:failed"},
            )

            blocked = sidebar_api.sidebar_agent_tick(data_dir, {})

            self.assertEqual(blocked["agent"]["proactive_reply_count"], 0)
            self.assertEqual(blocked["session_snapshot"]["after"]["pending_user_count"], 0)
            self.assertEqual(blocked["session_snapshot"]["after"]["blocked_pending_user_count"], 1)
            summary = blocked["agent_state"]["last_tick"]["session_summary"]
            self.assertEqual(summary["blocked_pending_user_count"], 1)
            self.assertEqual(summary["blocked_conversation_ids"], [conversation_id])

            ledger.append_message(
                NormalizedMessage(
                    message_id="pending-user-2",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Live Friend",
                    sender_name="Live Friend",
                    sender_wechat_id="wxid_live_friend",
                    text="刚才没收到，你还在吗",
                    is_self=False,
                    received_at="2026-07-08T09:02:00+08:00",
                    metadata={"source": "test", "context_only": True},
                )
            )

            resumed = sidebar_api.sidebar_agent_tick(data_dir, {})

            self.assertEqual(resumed["agent"]["proactive_reply_count"], 1)

    def test_sidebar_agent_tick_reports_opening_greeting_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("private", "wxid_new_friend")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="self-open-1",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="New Friend",
                    sender_name="Me",
                    sender_wechat_id="wxid_self",
                    text="我通过了你的朋友验证请求，现在我们可以开始聊天了",
                    is_self=True,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test", "context_only": True},
                )
            )

            result = sidebar_api.sidebar_agent_tick(data_dir, {})

            before = result["session_snapshot"]["before"]
            self.assertEqual(before["opening_greeting_count"], 1)
            self.assertEqual(before["opening_greeting_conversation_ids"], [conversation_id])
            self.assertEqual(result["agent"]["proactive_reply_count"], 1)
            summary = result["agent_state"]["last_tick"]["session_summary"]
            self.assertEqual(summary["opening_greeting_count"], 0)

    def test_sidebar_agent_tick_treats_self_messages_as_human_not_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("private", "wxid_three_party")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="manual-self-1",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Three Party",
                    sender_name="Me",
                    sender_wechat_id="wxid_self",
                    text="一会儿我会接入 Agent，你先随便聊聊",
                    is_self=True,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test"},
                )
            )
            ledger.append_message(
                NormalizedMessage(
                    message_id="friend-after-self-1",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Three Party",
                    sender_name="Three Party",
                    sender_wechat_id="wxid_three_party",
                    text="好的",
                    is_self=False,
                    received_at="2026-07-08T09:01:00+08:00",
                    metadata={"source": "test", "context_only": True},
                )
            )

            result = sidebar_api.sidebar_agent_tick(data_dir, {})

            self.assertEqual(result["agent"]["processed_count"], 0)
            self.assertEqual(result["agent"]["proactive_reply_count"], 1)
            entries = ledger.read_entries(conversation_id)
            self.assertEqual([entry.role for entry in entries], ["self", "user", "assistant"])

    def test_sidebar_agent_tick_self_message_settles_older_failed_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("private", "wxid_owner_takeover")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="old-user-before-self",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Owner Takeover",
                    sender_name="Owner Takeover",
                    sender_wechat_id="wxid_owner_takeover",
                    text="哪个群？我在里面吗",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test", "context_only": True},
                )
            )
            failed_reply = ReplyCandidate(
                message_id="failed-before-owner-takeover",
                conversation_id=conversation_id,
                text="我确认一下",
                send_mode="auto",
                model="fake",
            )
            failed_entry = ledger.append_reply(
                failed_reply,
                chat_title="Owner Takeover",
                conversation_type="private",
                session_id="session_default",
            )
            ledger.update_reply_send_result(
                conversation_id,
                failed_entry.entry_id,
                {"status": "failed", "reason": "wechat_native_backend_unavailable:ConnectionRefusedError:refused"},
            )
            ledger.append_message(
                NormalizedMessage(
                    message_id="manual-owner-takeover",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Owner Takeover",
                    sender_name="Me",
                    sender_wechat_id="wxid_self",
                    text="我来答这个：你不在，我拉你了",
                    is_self=True,
                    received_at="2026-07-08T09:02:00+08:00",
                    metadata={"source": "test", "context_only": True},
                )
            )

            result = sidebar_api.sidebar_agent_tick(data_dir, {})

            before = result["session_snapshot"]["before"]["conversations"][0]
            self.assertEqual(before["pending_user_count_since_last_assistant"], 0)
            self.assertEqual(before["blocked_pending_user_count"], 0)
            self.assertEqual(result["agent"]["proactive_reply_count"], 0)
            self.assertEqual(len(ledger.read_entries(conversation_id)), 3)

    def test_sidebar_agent_tick_after_self_only_replies_to_new_user_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("private", "wxid_owner_boundary")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="old-user-before-owner-boundary",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Owner Boundary",
                    sender_name="Owner Boundary",
                    sender_wechat_id="wxid_owner_boundary",
                    text="哪个群？我在里面吗",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test", "context_only": True},
                )
            )
            failed_reply = ReplyCandidate(
                message_id="failed-before-owner-boundary",
                conversation_id=conversation_id,
                text="我确认一下",
                send_mode="auto",
                model="fake",
            )
            failed_entry = ledger.append_reply(
                failed_reply,
                chat_title="Owner Boundary",
                conversation_type="private",
                session_id="session_default",
            )
            ledger.update_reply_send_result(
                conversation_id,
                failed_entry.entry_id,
                {"status": "failed", "reason": "wechat_native_backend_unavailable:ConnectionRefusedError:refused"},
            )
            ledger.append_message(
                NormalizedMessage(
                    message_id="manual-owner-boundary",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Owner Boundary",
                    sender_name="Me",
                    sender_wechat_id="wxid_self",
                    text="你不在，我拉你了",
                    is_self=True,
                    received_at="2026-07-08T09:02:00+08:00",
                    metadata={"source": "test", "context_only": True},
                )
            )
            ledger.append_message(
                NormalizedMessage(
                    message_id="new-user-after-owner-boundary",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Owner Boundary",
                    sender_name="Owner Boundary",
                    sender_wechat_id="wxid_owner_boundary",
                    text="好的，拉我一下",
                    is_self=False,
                    received_at="2026-07-08T09:03:00+08:00",
                    metadata={"source": "test", "context_only": True},
                )
            )

            result = sidebar_api.sidebar_agent_tick(data_dir, {})

            before = result["session_snapshot"]["before"]["conversations"][0]
            self.assertEqual(before["pending_user_count_since_last_assistant"], 1)
            self.assertEqual(before["pending_user_messages"][0]["message_id"], "new-user-after-owner-boundary")
            self.assertEqual(before["blocked_pending_user_count"], 0)
            self.assertEqual(result["agent"]["proactive_reply_count"], 1)
            self.assertEqual(result["agent"]["proactive_replies"][0]["pending_count"], 1)
            self.assertEqual([entry.role for entry in ledger.read_entries(conversation_id)], ["user", "assistant", "self", "user", "assistant"])

    def test_sidebar_agent_worker_processes_new_events_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("private", "wxid_worker_alice")
            started = sidebar_api.sidebar_agent_start(data_dir, {"interval_seconds": 0.05})
            try:
                self.assertEqual(started["status"], "ok")
                self.assertTrue(started["worker"]["running"])
                again = sidebar_api.sidebar_agent_start(data_dir, {"interval_seconds": 0.05})
                self.assertEqual(again["status"], "ok")
                self.assertTrue(again["worker"]["running"])

                append_sidebar_backend_event(
                    data_dir,
                    {
                        "raw_id": "agent-worker-message-1",
                        "chat_title": "Worker Alice",
                        "sender_name": "Worker Alice",
                        "sender_wechat_id": "wxid_worker_alice",
                        "text": "worker should pick this up",
                        "observed_at": "2026-07-08T09:10:00+08:00",
                        "source_payload": {
                            "conversation_key": "wxid_worker_alice",
                            "talker_id": "wxid_worker_alice",
                            "is_friend": True,
                        },
                    },
                )

                deadline = threading.Event()
                for _ in range(80):
                    state = sidebar_api.build_sidebar_agent_state(data_dir)
                    last_tick = state.get("last_tick") if isinstance(state.get("last_tick"), dict) else {}
                    worker = state.get("worker") if isinstance(state.get("worker"), dict) else {}
                    if int(last_tick.get("processed_count") or 0) >= 1 and int(worker.get("loops") or 0) >= 1:
                        break
                    deadline.wait(0.05)

                state = sidebar_api.build_sidebar_agent_state(data_dir)
                self.assertGreaterEqual(state["worker"]["loops"], 1)
                self.assertEqual(state["last_tick"]["processed_count"], 1)
                entries = ConversationLedgerStore(data_dir).read_entries(conversation_id)
                self.assertEqual(entries[-1].role, "assistant")
                tasks = sidebar_api.build_sidebar_task_manager(data_dir)["tasks"]
                self.assertTrue(any(item.get("external_id") == "agent-worker" for item in tasks))
            finally:
                stopped = sidebar_api.sidebar_agent_stop(data_dir, {})

            self.assertEqual(stopped["status"], "ok")
            self.assertFalse(stopped["worker"]["running"])
            self.assertTrue(stopped["finished_tasks"])
            self.assertEqual(stopped["finished_tasks"][0]["external_id"], "agent-worker")

    def test_sidebar_agent_worker_blocks_restart_with_different_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            alice_id = conversation_id_for("private", "wxid_worker_scope_alice")
            bob_id = conversation_id_for("private", "wxid_worker_scope_bob")
            started = sidebar_api.sidebar_agent_start(
                data_dir,
                {
                    "interval_seconds": 1,
                    "talkers": ["wxid_worker_scope_alice"],
                    "conversation_ids": [alice_id],
                },
            )
            try:
                self.assertEqual(started["status"], "ok")
                self.assertTrue(started["worker"]["running"])

                blocked = sidebar_api.sidebar_agent_start(
                    data_dir,
                    {
                        "interval_seconds": 1,
                        "talkers": ["wxid_worker_scope_bob"],
                        "conversation_ids": [bob_id],
                    },
                )

                self.assertEqual(blocked["status"], "blocked")
                self.assertEqual(blocked["reason"], "agent_worker_scope_mismatch")
                self.assertEqual(blocked["running_scope"]["conversation_ids"], [alice_id])
                self.assertEqual(blocked["requested_scope"]["conversation_ids"], [bob_id])
                self.assertTrue(blocked["worker"]["running"])
            finally:
                sidebar_api.sidebar_agent_stop(data_dir, {})

    def test_sidebar_agent_worker_allows_restart_alias_for_same_conversation_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            conversation_id = conversation_id_for("private", "wxid_worker_alias")
            runtime.channel_store.ensure_channel(
                NormalizedMessage(
                    message_id="worker-alias-channel-1",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Worker Alias",
                    sender_name="Worker Sender Alias",
                    sender_wechat_id="wxid_worker_alias",
                    text="hello",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "backend_events_jsonl"},
                )
            )
            started = sidebar_api.sidebar_agent_start(
                data_dir,
                {"interval_seconds": 1, "talkers": ["Worker Alias"], "conversation_ids": [conversation_id]},
            )
            try:
                self.assertEqual(started["status"], "ok")
                again = sidebar_api.sidebar_agent_start(
                    data_dir,
                    {"interval_seconds": 1, "talkers": ["wxid_worker_alias"], "conversation_ids": [conversation_id]},
                )

                self.assertEqual(again["status"], "ok")
                self.assertEqual(again["message"], "Dialog Agent worker is already running")
                self.assertTrue(again["worker"]["running"])
            finally:
                sidebar_api.sidebar_agent_stop(data_dir, {})

    def test_sidebar_agent_snapshot_aggregates_multi_user_group_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("group", "room_alpha@chatroom")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="group-msg-a",
                    conversation_id=conversation_id,
                    conversation_type="group",
                    chat_title="Alpha Group",
                    sender_name="Alice",
                    sender_wechat_id="wxid_alice",
                    text="Agent 什么时候能连续工作？",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test"},
                )
            )
            ledger.append_message(
                NormalizedMessage(
                    message_id="group-msg-b",
                    conversation_id=conversation_id,
                    conversation_type="group",
                    chat_title="Alpha Group",
                    sender_name="Bob",
                    sender_wechat_id="wxid_bob",
                    text="我也想看主题聚合和任务分发。",
                    is_self=False,
                    received_at="2026-07-08T09:01:00+08:00",
                    metadata={"source": "test"},
                )
            )

            result = sidebar_api.sidebar_agent_tick(data_dir, {})

            conversation = result["session_snapshot"]["after"]["conversations"][0]
            aggregation = conversation["message_aggregation"]
            dispatch = conversation["dispatch_preview"]

            self.assertEqual(result["agent"]["processed_count"], 0)
            self.assertEqual(conversation["participant_count"], 2)
            self.assertEqual(conversation["pending_user_count_since_last_assistant"], 2)
            self.assertEqual(aggregation["status"], "needs_agent_reply")
            self.assertEqual(set(aggregation["pending_senders"]), {"Alice", "Bob"})
            self.assertTrue(any(item["resource_class"] == "llm_interactive" for item in dispatch))
            self.assertTrue(any(item["title"] for item in conversation["topic_candidates"]))

    def test_agent_snapshot_discovers_sqlite_ledger_without_readable_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = "db-only-agent-conversation"
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="db-only-user-1",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="DB Only",
                    sender_name="DB Only",
                    sender_wechat_id="wxid_db_only",
                    text="pending from sqlite",
                    is_self=False,
                    received_at="2026-07-10T00:00:00+08:00",
                    metadata={"source": "test"},
                )
            )
            projection_dir = ledger.conversation_markdown_path(conversation_id).parent
            (projection_dir / "messages.jsonl").unlink()
            (projection_dir / "conversation.md").unlink()
            runtime = build_runtime(load_config(data_dir))

            snapshot = sidebar_api._agent_session_snapshot(data_dir, runtime=runtime)

            self.assertEqual(snapshot["conversation_count"], 1)
            self.assertEqual(snapshot["conversations"][0]["conversation_id"], conversation_id)
            self.assertEqual(snapshot["conversations"][0]["pending_user_count_since_last_assistant"], 1)
            self.assertTrue((projection_dir / "messages.jsonl").exists())
            self.assertTrue((projection_dir / "conversation.md").exists())

    def test_sidebar_agent_snapshot_excludes_group_owner_messages_from_pending_and_participants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("group", "room_owner_scope")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="group-owner-1",
                    conversation_id=conversation_id,
                    conversation_type="group",
                    chat_title="Owner Group",
                    sender_name="Me",
                    sender_wechat_id="wxid_self",
                    text="这个我已经处理了，大家不用再等 Agent。",
                    is_self=True,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test", "context_only": True},
                )
            )
            runtime = build_runtime(load_config(data_dir))

            snapshot = sidebar_api._agent_session_snapshot(data_dir, runtime=runtime, conversation_ids=[conversation_id])

            conversation = snapshot["conversations"][0]
            aggregation = conversation["message_aggregation"]
            self.assertEqual(conversation["pending_user_count_since_last_assistant"], 0)
            self.assertEqual(conversation["participant_count"], 0)
            self.assertEqual(aggregation["status"], "settled")
            self.assertEqual(aggregation["pending_senders"], [])
            self.assertEqual(aggregation["recent_senders"], [])

    def test_agent_snapshot_reports_topic_lifecycle_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            ledger = ConversationLedgerStore(data_dir)

            open_id = conversation_id_for("private", "wxid_lifecycle_open")
            ledger.append_message(
                NormalizedMessage(
                    message_id="lifecycle-open-user",
                    conversation_id=open_id,
                    conversation_type="private",
                    chat_title="Lifecycle Open",
                    sender_name="Lifecycle Open",
                    sender_wechat_id="wxid_lifecycle_open",
                    text="这个问题还没回复",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test"},
                )
            )

            sent_id = conversation_id_for("private", "wxid_lifecycle_sent")
            ledger.append_message(
                NormalizedMessage(
                    message_id="lifecycle-sent-user",
                    conversation_id=sent_id,
                    conversation_type="private",
                    chat_title="Lifecycle Sent",
                    sender_name="Lifecycle Sent",
                    sender_wechat_id="wxid_lifecycle_sent",
                    text="这条已经回复了吗",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test"},
                )
            )
            sent_reply = ledger.append_reply(
                ReplyCandidate(
                    message_id="lifecycle-sent-reply",
                    conversation_id=sent_id,
                    text="已经回复了",
                    send_mode="auto",
                    model="fake",
                ),
                chat_title="Lifecycle Sent",
                conversation_type="private",
            )
            ledger.update_reply_send_result(
                sent_id,
                sent_reply.entry_id,
                {"status": "sent", "reason": "wechat_native_http_send_text_verified"},
            )

            responded_id = conversation_id_for("private", "wxid_lifecycle_responded")
            ledger.append_message(
                NormalizedMessage(
                    message_id="lifecycle-responded-user",
                    conversation_id=responded_id,
                    conversation_type="private",
                    chat_title="Lifecycle Responded",
                    sender_name="Lifecycle Responded",
                    sender_wechat_id="wxid_lifecycle_responded",
                    text="失败回复不能重复生成",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test"},
                )
            )
            failed_reply = ledger.append_reply(
                ReplyCandidate(
                    message_id="lifecycle-failed-reply",
                    conversation_id=responded_id,
                    text="这条还没送达",
                    send_mode="auto",
                    model="fake",
                ),
                chat_title="Lifecycle Responded",
                conversation_type="private",
            )
            ledger.update_reply_send_result(
                responded_id,
                failed_reply.entry_id,
                {"status": "failed", "reason": "wechat_native_backend_unavailable"},
            )

            reopened_id = conversation_id_for("private", "wxid_lifecycle_reopened")
            ledger.append_message(
                NormalizedMessage(
                    message_id="lifecycle-reopened-user-1",
                    conversation_id=reopened_id,
                    conversation_type="private",
                    chat_title="Lifecycle Reopened",
                    sender_name="Lifecycle Reopened",
                    sender_wechat_id="wxid_lifecycle_reopened",
                    text="先回答了一个问题",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test"},
                )
            )
            reopened_reply = ledger.append_reply(
                ReplyCandidate(
                    message_id="lifecycle-reopened-reply",
                    conversation_id=reopened_id,
                    text="先答这个",
                    send_mode="auto",
                    model="fake",
                ),
                chat_title="Lifecycle Reopened",
                conversation_type="private",
            )
            ledger.update_reply_send_result(
                reopened_id,
                reopened_reply.entry_id,
                {"status": "sent", "reason": "wechat_native_http_send_text_verified"},
            )
            ledger.append_message(
                NormalizedMessage(
                    message_id="lifecycle-reopened-user-2",
                    conversation_id=reopened_id,
                    conversation_type="private",
                    chat_title="Lifecycle Reopened",
                    sender_name="Lifecycle Reopened",
                    sender_wechat_id="wxid_lifecycle_reopened",
                    text="我又补充一个新问题",
                    is_self=False,
                    received_at="2026-07-08T09:02:00+08:00",
                    metadata={"source": "test"},
                )
            )

            closed_id = conversation_id_for("private", "wxid_lifecycle_closed")
            ledger.append_message(
                NormalizedMessage(
                    message_id="lifecycle-closed-self",
                    conversation_id=closed_id,
                    conversation_type="private",
                    chat_title="Lifecycle Closed",
                    sender_name="Me",
                    sender_wechat_id="wxid_self",
                    text="我手动处理了这个话题",
                    is_self=True,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={"source": "test"},
                )
            )
            runtime = build_runtime(load_config(data_dir))

            snapshot = sidebar_api._agent_session_snapshot(
                data_dir,
                runtime=runtime,
                conversation_ids=[open_id, sent_id, responded_id, reopened_id, closed_id],
            )

            statuses = {
                item["conversation_id"]: item["topic_lifecycle"]["status"]
                for item in snapshot["conversations"]
            }
            self.assertEqual(statuses[open_id], "open")
            self.assertEqual(statuses[sent_id], "sent")
            self.assertEqual(statuses[responded_id], "responded")
            self.assertEqual(statuses[reopened_id], "reopened")
            self.assertEqual(statuses[closed_id], "closed")
            self.assertEqual(
                snapshot["topic_lifecycle_counts"],
                {"open": 1, "responded": 1, "sent": 1, "closed": 1, "reopened": 1},
            )

    def test_agent_self_echo_confirms_assistant_entry_without_repeat_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("private", "wxid_agent_echo")
            ledger = ConversationLedgerStore(data_dir)
            reply = ReplyCandidate(
                message_id="agent-reply-echo",
                conversation_id=conversation_id,
                text="我已经收到，稍后补充。",
                send_mode="auto",
                model="fake",
            )
            entry = ledger.append_reply(reply, chat_title="Agent Echo", conversation_type="private")
            ledger.update_reply_send_result(
                conversation_id,
                entry.entry_id,
                SendResult(
                    message_id="bridge:agent-echo",
                    conversation_id=conversation_id,
                    status="queued_to_bridge",
                    reason="queued_to_non_foreground_bridge:bridge:agent-echo",
                ),
            )

            result = ledger.append_message_result(
                NormalizedMessage(
                    message_id="weflow-self-echo",
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="Agent Echo",
                    sender_name="Me",
                    sender_wechat_id="wxid_self",
                    text="我已经收到，稍后补充。",
                    is_self=True,
                    received_at="2026-07-08T09:01:00+08:00",
                    metadata={"source": "weflow"},
                )
            )
            tick = sidebar_api.sidebar_agent_tick(data_dir, {})
            entries = ledger.read_entries(conversation_id)

            self.assertEqual(result.status, "self_echo_confirmed")
            self.assertEqual([item.role for item in entries], ["assistant"])
            self.assertEqual(entries[0].send["status"], "sent")
            self.assertEqual(entries[0].send["reason"], "queued_to_non_foreground_bridge:bridge:agent-echo")
            self.assertEqual(entries[0].send["echo_message_id"], "weflow-self-echo")
            self.assertTrue(entries[0].send["echo_confirmed_at"])
            self.assertEqual(tick["agent"]["processed_count"], 0)
            self.assertEqual(tick["agent"]["proactive_reply_count"], 0)

    def test_agent_repeat_guard_blocks_highly_similar_proactive_reply(self) -> None:
        conversation = {
            "last_assistant_reply": "我已经收到，稍后补充这份资料的重点。",
            "recent_turns": [
                {
                    "role": "assistant",
                    "text": "我已经收到，稍后补充这份资料的重点。",
                }
            ],
        }

        repeated = sidebar_api._agent_reply_repeats_recent_assistant(
            "我已经收到，稍后补充这份资料的重点。",
            conversation,
        )
        fresh = sidebar_api._agent_reply_repeats_recent_assistant(
            "这次我先按你新发的表格口径整理重点。",
            conversation,
        )

        self.assertTrue(repeated)
        self.assertFalse(fresh)

    def test_agent_proactive_reply_skips_when_newer_linear_message_arrives_during_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            conversation_id = conversation_id_for("private", "wxid_linear_race")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="linear-race-1",
                    conversation_id=conversation_id,
                    conversation_type="private",  # type: ignore[arg-type]
                    chat_title="Linear Race",
                    sender_name="Alice",
                    text="第一句，等你回复",
                    is_self=False,
                    received_at="2026-07-10T00:00:00+00:00",
                    sender_wechat_id="wxid_linear_race",
                )
            )
            real_runtime = build_runtime(load_config(data_dir))
            snapshot = sidebar_api._agent_session_snapshot(data_dir, runtime=real_runtime, conversation_ids=[conversation_id])
            conversation = snapshot["conversations"][0]
            watermark = conversation["pending_user_messages"][0]["sequence"]
            candidate = {
                "kind": "pending_private_reply",
                "conversation": conversation,
                "conversation_id": conversation_id,
                "pending_count": 1,
                "source_watermark": watermark,
            }

            class _RaceConversation:
                def generate_reply(self, message, speak):
                    ledger.append_message(
                        NormalizedMessage(
                            message_id="linear-race-2",
                            conversation_id=conversation_id,
                            conversation_type="private",  # type: ignore[arg-type]
                            chat_title="Linear Race",
                            sender_name="Alice",
                            text="第二句，生成期间新来的内容",
                            is_self=False,
                            received_at="2026-07-10T00:00:01+00:00",
                            sender_wechat_id="wxid_linear_race",
                        )
                    )
                    return ReplyCandidate(
                        message_id="agent-stale-reply",
                        conversation_id=conversation_id,
                        text="这是只看过第一句的过期回复",
                        send_mode="confirm",
                        model="fake",
                    )

            runtime = SimpleNamespace(
                ledger_store=ledger,
                conversation=_RaceConversation(),
                reply_gate=SimpleNamespace(handle=lambda reply: self.fail("stale reply must not be sent")),
                event_logger=SimpleNamespace(log=lambda *args, **kwargs: None),
            )

            result = sidebar_api._agent_generate_one_proactive_reply(data_dir, runtime=runtime, candidate=candidate)
            entries = ledger.read_entries(conversation_id)

            self.assertEqual(result["status"], "skipped")
            self.assertIn("stale_linear_context", result["reason"])
            self.assertEqual([entry.role for entry in entries], ["user", "user"])
            self.assertEqual([entry.message_id for entry in entries], ["linear-race-1", "linear-race-2"])

    def test_agent_dispatch_preview_only_schedules_file_work_for_incoming_attachments(self) -> None:
        entries = [
            {"role": "user", "is_self": False, "attachments": [], "sequence": 1},
            {
                "role": "assistant",
                "is_self": True,
                "attachments": [{"name": "agent-result.csv", "source": "tool_result"}],
                "sequence": 2,
            },
            {"role": "user", "is_self": False, "attachments": [], "sequence": 3},
        ]

        outgoing_only = sidebar_api._agent_dispatch_preview(
            "conv1",
            entries,
            [entries[-1]],
            [{"topic_id": "topic-1", "title": "followup"}],
        )
        with_incoming = sidebar_api._agent_dispatch_preview(
            "conv1",
            [
                *entries,
                {
                    "role": "user",
                    "is_self": False,
                    "attachments": [{"name": "user-input.pdf", "source": "weflow"}],
                    "sequence": 4,
                },
            ],
            [entries[-1]],
            [{"topic_id": "topic-1", "title": "followup"}],
        )

        self.assertFalse(any(item["kind"] == "file" for item in outgoing_only))
        incoming_file_tasks = [item for item in with_incoming if item["kind"] == "file"]
        self.assertEqual(len(incoming_file_tasks), 1)
        self.assertEqual(incoming_file_tasks[0]["reason"], "conversation_has_incoming_attachments")

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
                    "send_backend": "wechat_native_http",
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
            self.assertEqual(config.send_backend, "wechat_native_http")
            self.assertEqual(config.ocr_mode, "gpu")
            self.assertEqual(config.asr_mode, "cpu")
            self.assertEqual(config.file_max_bytes, 32 * 1024 * 1024)
            self.assertEqual(build_sidebar_state(data_dir)["config"]["send_backend"], "wechat_native_http")
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

    def test_channel_test_reply_creates_pending_review_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            _ensure_test_channel(data_dir)

            result = sidebar_channel_test_reply(
                data_dir,
                "private-1",
                {"text": "probe reply", "talkers": ["wxid_alice"], "require_scope": True},
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["item"]["status"], "pending")
            self.assertEqual(result["reply"]["text"], "probe reply")
            pending = build_sidebar_state(data_dir)["queues"]["pending"]
            self.assertEqual(pending["count"], 1)
            entry = ConversationLedgerStore(data_dir).read_entries("private-1")[-1]
            self.assertEqual(entry.role, "assistant")
            self.assertEqual(entry.text_blocks[0]["text"], "probe reply")
            self.assertEqual(entry.send["metadata"]["origin"], "sidebar_channel_test_reply")

    def test_channel_test_file_upload_creates_file_only_review_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            _ensure_test_channel(data_dir)

            result = sidebar_channel_test_file(
                data_dir,
                "private-1",
                {
                    "file": {
                        "name": "report.txt",
                        "mime_type": "text/plain",
                        "content_base64": base64.b64encode(b"hello file").decode("ascii"),
                    },
                    "talkers": ["wxid_alice"],
                    "require_scope": True,
                },
            )

            self.assertEqual(result["status"], "ok")
            self.assertTrue(Path(result["stored_path"]).exists())
            self.assertEqual(Path(result["stored_path"]).read_bytes(), b"hello file")
            self.assertEqual(result["item"]["status"], "pending")
            reply = result["item"]["reply"]
            self.assertEqual(reply["text"], "")
            self.assertEqual(reply["attachments"][0]["name"], "report.txt")
            entry = ConversationLedgerStore(data_dir).read_entries("private-1")[-1]
            self.assertEqual(entry.attachments[0]["path"], result["stored_path"])

    def test_channel_test_reply_blocks_without_selected_talker_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            _ensure_test_channel(data_dir)

            result = sidebar_channel_test_reply(data_dir, "private-1", {"text": "probe reply"})

            self.assertEqual(result["status"], "blocked")
            self.assertIn("send_scope_required", result["reason"])
            self.assertEqual(ConfirmQueue(data_dir / "confirm_queue.jsonl").list_by_status("pending"), [])
            self.assertEqual(ConversationLedgerStore(data_dir).read_entries("private-1"), [])

    def test_channel_test_file_blocks_when_selected_talker_scope_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            _ensure_test_channel(data_dir)

            result = sidebar_channel_test_file(
                data_dir,
                "private-1",
                {
                    "file": {
                        "name": "report.txt",
                        "mime_type": "text/plain",
                        "content_base64": base64.b64encode(b"hello file").decode("ascii"),
                    },
                    "talkers": ["wxid_other"],
                    "require_scope": True,
                },
            )

            self.assertEqual(result["status"], "blocked")
            self.assertIn("send_scope_mismatch", result["reason"])
            self.assertFalse((data_dir / "outgoing_uploads").exists())
            self.assertEqual(ConversationLedgerStore(data_dir).read_entries("private-1"), [])

    def test_channel_test_reply_auto_mode_dispatches_without_confirm_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="auto", enabled=True, driver="bridge_outbox", backend="wechat_native_http")
            _ensure_test_channel(data_dir)
            executor = mock.Mock()
            executor.execute_auto.return_value = SendResult(
                "bridge:test",
                "private-1",
                "queued_to_bridge",
                "queued_to_non_foreground_bridge:bridge:test",
            )

            with mock.patch.object(sidebar_api, "_start_bridge_worker") as start_worker, mock.patch.object(
                sidebar_api, "build_send_driver", return_value=mock.Mock()
            ), mock.patch.object(sidebar_api, "GuardedSendExecutor", return_value=executor):
                result = sidebar_channel_test_reply(
                    data_dir,
                    "private-1",
                    {"text": "probe reply", "talkers": ["wxid_alice"], "require_scope": True},
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["dispatch_mode"], "auto")
            self.assertEqual(result["queue_id"], "")
            self.assertEqual(result["send_result"]["status"], "queued_to_bridge")
            self.assertEqual(result["reply"]["send_mode"], "auto")
            self.assertFalse(result["reply"]["send_metadata"]["review_required"])
            self.assertEqual(ConfirmQueue(data_dir / "confirm_queue.jsonl").list_by_status("pending"), [])
            start_worker.assert_called_once()
            executor.execute_auto.assert_called_once()

    def test_agent_requested_talkers_map_to_registered_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            _ensure_test_channel(data_dir, conversation_id="conv-alice", sender_wechat_id="wxid_alice")
            _ensure_test_channel(data_dir, conversation_id="conv-bob", sender_wechat_id="wxid_bob")

            ids = sidebar_api._agent_requested_conversation_ids(
                data_dir,
                {"talkers": ["wxid_bob"]},
            )

            self.assertEqual(ids, ["conv-bob"])

    def test_agent_session_snapshot_respects_requested_talker_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            _ensure_test_channel(data_dir, conversation_id="conv-alice", sender_wechat_id="wxid_alice")
            _ensure_test_channel(data_dir, conversation_id="conv-bob", sender_wechat_id="wxid_bob")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="alice-1",
                    conversation_id="conv-alice",
                    conversation_type="private",
                    chat_title="Alice",
                    sender_name="Alice",
                    sender_wechat_id="wxid_alice",
                    text="alice pending message",
                    is_self=False,
                    received_at="2026-07-09T00:00:00+00:00",
                    metadata={},
                )
            )
            ledger.append_message(
                NormalizedMessage(
                    message_id="bob-1",
                    conversation_id="conv-bob",
                    conversation_type="private",
                    chat_title="Bob",
                    sender_name="Bob",
                    sender_wechat_id="wxid_bob",
                    text="bob pending message",
                    is_self=False,
                    received_at="2026-07-09T00:00:01+00:00",
                    metadata={},
                )
            )
            requested = sidebar_api._agent_requested_conversation_ids(data_dir, {"talkers": ["wxid_bob"]})
            runtime = build_runtime(load_config(data_dir))

            snapshot = sidebar_api._agent_session_snapshot(data_dir, runtime=runtime, conversation_ids=requested)

            self.assertEqual(snapshot["conversation_count"], 1)
            self.assertEqual(snapshot["pending_user_count"], 1)
            self.assertEqual([item["conversation_id"] for item in snapshot["conversations"]], ["conv-bob"])

    def test_agent_requested_talker_scope_does_not_fallback_when_unmatched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            _ensure_test_channel(data_dir, conversation_id="conv-alice", sender_wechat_id="wxid_alice")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="alice-1",
                    conversation_id="conv-alice",
                    conversation_type="private",
                    chat_title="Alice",
                    sender_name="Alice",
                    sender_wechat_id="wxid_alice",
                    text="alice pending message",
                    is_self=False,
                    received_at="2026-07-09T00:00:00+00:00",
                    metadata={},
                )
            )
            requested = sidebar_api._agent_requested_conversation_ids(data_dir, {"talkers": ["wxid_missing"]})
            runtime = build_runtime(load_config(data_dir))

            snapshot = sidebar_api._agent_session_snapshot(data_dir, runtime=runtime, conversation_ids=requested)
            pending = sidebar_api._agent_pending_event_snapshot(data_dir, {"talkers": ["wxid_missing"]})

            self.assertEqual(requested, [])
            self.assertEqual(snapshot["conversation_count"], 0)
            self.assertEqual(snapshot["pending_user_count"], 0)
            self.assertFalse(pending["has_pending_ledger"])
            self.assertEqual(pending["pending_user_count"], 0)

    def test_send_queue_ledger_sync_handles_attachment_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="attachment-message",
                    conversation_id="private-1",
                    conversation_type="private",
                    chat_title="Alice",
                    sender_name="Alice",
                    sender_wechat_id="wxid_alice",
                    text="please read this deck",
                    is_self=False,
                    received_at="2026-07-08T09:00:00+08:00",
                    metadata={
                        "attachments": [
                            {
                                "name": "deck.pptx",
                                "kind": "presentation",
                                "status": "blocked",
                                "reason": "presentation parsing skipped for safety",
                                "parse": {"summary": "a long deck summary that must be compacted for markdown rendering"},
                            }
                        ]
                    },
                )
            )
            reply = _reply()
            ledger.append_reply(reply, chat_title="Alice")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)

            approved = sidebar_queue_action(data_dir, "approve", queue_id, {"reviewer": "test"})

            self.assertEqual(approved["item"]["status"], "approved")
            audit = build_sidebar_state(data_dir)["audit"]["items"]
            self.assertFalse(any(item.get("action") == "ledger_sync_failed" for item in audit))

    def test_dry_run_bridge_ack_is_synced_as_not_delivered_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="auto", enabled=True, driver="bridge_outbox", backend="dry_run")
            reply = _reply()
            ConversationLedgerStore(data_dir).append_reply(reply, chat_title="Alice")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="test")

            sent = send_approved_confirm_item(data_dir, queue_id)
            bridge_id = sent["send_result"]["message_id"]
            synced = sync_bridge_ack_to_send_state(
                data_dir,
                bridge_id,
                status="sent",
                reason="dry_run_not_delivered:text",
            )

            self.assertEqual(sent["status"], "queued_to_bridge")
            self.assertEqual(synced["status"], "ok")
            item = queue.get(queue_id)
            self.assertEqual(item["status"], "sent")
            self.assertEqual(item["note"], "dry_run_not_delivered:text")
            state = build_sidebar_state(data_dir)
            self.assertEqual(state["capture"]["background_send_status"], "bridge_outbox_dry_run_backend")
            send_task = next(task for task in state["task_manager"]["tasks"] if task["task_id"].startswith("send-"))
            self.assertEqual(send_task["phase"], "非前台桥演练完成，未投递微信")

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

    def test_sidebar_bridge_retry_requeues_failed_item(self) -> None:
        from app.personal_wechat_bot.wechat_driver.bridge_send import BridgeAckStatus, BridgeOutboxStore

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            rec = store.enqueue("wxid_a", "retry me")
            store.append_ack(
                rec["bridge_id"],
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
            )

            with mock.patch("app.personal_wechat_bot.control.sidebar_api._start_bridge_worker") as start_worker:
                result = retry_sidebar_bridge_item(data_dir, {"bridge_id": rec["bridge_id"], "reviewer": "tester"})
            state = build_sidebar_bridge_state(data_dir)
            retry = next(item for item in state["items"] if item["bridge_id"] == result["new_bridge_id"])

            self.assertEqual(result["status"], "ok")
            self.assertEqual(retry["status"], "queued")
            self.assertEqual(retry["retry_of"], rec["bridge_id"])
            start_worker.assert_called_once()

    def test_sidebar_manual_ack_rejects_nonterminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            with self.assertRaisesRegex(ValueError, "sent, accepted, failed, or blocked"):
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
        provider = config.providers["chat"]
        provider.api_key_file = relative
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
            provider = config.providers["chat"]
            provider.api_key_file = "keys.md"
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
            provider = config.providers["chat"]
            provider.api_key_file = "keys.md"
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
            self.assertTrue(result["migration_notes"])
            for group in result["groups"]:
                self.assertIn("install_command", group)
                self.assertIn("requirements_exists", group)
                self.assertTrue(group["portable_from_github"])


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


def _reply(
    *,
    message_id: str = "message-1",
    conversation_id: str = "private-1",
    text: str = "hello",
) -> ReplyCandidate:
    return ReplyCandidate(
        message_id=message_id,
        conversation_id=conversation_id,
        text=text,
        send_mode="confirm",
        model="fake",
    )


def _ensure_test_channel(
    data_dir: Path,
    *,
    conversation_id: str = "private-1",
    sender_wechat_id: str = "wxid_alice",
    chat_title: str = "Alice",
) -> None:
    sidebar_api._channel_store(data_dir).ensure_channel(
        NormalizedMessage(
            message_id=f"channel-{conversation_id}",
            conversation_id=conversation_id,
            conversation_type="private",
            chat_title=chat_title,
            sender_name=chat_title,
            sender_wechat_id=sender_wechat_id,
            text="hello",
            is_self=False,
            received_at="2026-07-09T00:00:00+00:00",
            metadata={
                "source": "weflow_discovery",
                "trusted_channel_source": True,
                "conversation_key": sender_wechat_id,
            },
        )
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
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="dry_run")
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
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="dry_run")
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
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="dry_run", enabled=False, driver="not_implemented")
            root = data_dir.resolve()
            sidebar_api._start_bridge_worker(root, {})
            try:
                self.assertFalse(sidebar_api._bridge_worker_state(root)["running"])
            finally:
                sidebar_api._stop_bridge_worker(root)

    def test_start_bridge_worker_noop_when_external_worker_lock_is_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="dry_run")
            root = data_dir.resolve()
            lock_path = root / "send_bridge" / ".bridge_worker.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps({"pid": os.getpid(), "label": "send_bridge_worker", "heartbeat_at": time.time()}),
                encoding="utf-8",
            )

            with mock.patch("app.personal_wechat_bot.runtime.send_bridge_worker.run_bridge_worker") as run_worker:
                sidebar_api._start_bridge_worker(root, {"bridge_interval_seconds": 0.1})
                time.sleep(0.1)

            self.assertFalse(sidebar_api._bridge_worker_state(root)["running"])
            run_worker.assert_not_called()

    def test_start_bridge_worker_repairs_stale_external_worker_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = (Path(tmp) / "data").resolve()
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="dry_run")
            old_signature = sidebar_api._bridge_worker_config_signature(load_config(data_dir))
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="wechat_native_http")
            root = data_dir.resolve()
            lock_path = root / "send_bridge" / ".bridge_worker.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 123456,
                        "label": "send_bridge_worker",
                        "heartbeat_at": time.time(),
                        "data_dir": str(root),
                        "backend_name": "dry_run",
                        "config_signature": old_signature,
                    }
                ),
                encoding="utf-8",
            )

            started = threading.Event()

            def fake_run(_root, *, stop_event=None, **_kwargs):
                started.set()
                if stop_event is not None:
                    stop_event.wait(1)

            try:
                with mock.patch("app.personal_wechat_bot.control.sidebar_api.bridge_worker_lock_alive", side_effect=[True, True, False]), mock.patch.object(
                    sidebar_api, "_pid_exists", side_effect=[True, False, False, False]
                ), mock.patch.object(sidebar_api, "_terminate_process_tree", return_value=True) as terminate, mock.patch(
                    "app.personal_wechat_bot.runtime.send_bridge_worker.run_bridge_worker",
                    side_effect=fake_run,
                ):
                    sidebar_api._start_bridge_worker(root, {"bridge_interval_seconds": 0.1})
                    self.assertTrue(started.wait(1), "bridge worker was not restarted after stale external repair")

                terminate.assert_called_once_with(123456)
                self.assertTrue(sidebar_api._bridge_worker_state(root)["running"])
            finally:
                sidebar_api._stop_bridge_worker(root)

    def test_start_bridge_worker_restarts_when_backend_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = (Path(tmp) / "data").resolve()
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="dry_run")
            calls: list[str] = []

            def fake_run(root, *, stop_event=None, **_kwargs):
                calls.append(load_config(root).send_backend)
                while stop_event is not None and not stop_event.is_set():
                    time.sleep(0.02)

            try:
                with mock.patch("app.personal_wechat_bot.runtime.send_bridge_worker.run_bridge_worker", side_effect=fake_run):
                    sidebar_api._start_bridge_worker(data_dir, {"bridge_interval_seconds": 0.1})
                    deadline = time.time() + 2
                    while time.time() < deadline and calls != ["dry_run"]:
                        time.sleep(0.02)

                    set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="wechat_native_http")
                    sidebar_api._start_bridge_worker(data_dir, {"bridge_interval_seconds": 0.1})
                    deadline = time.time() + 2
                    while time.time() < deadline and (len(calls) < 2 or calls[-1] != "wechat_native_http"):
                        time.sleep(0.02)

                self.assertGreaterEqual(len(calls), 2)
                self.assertEqual(calls[0], "dry_run")
                self.assertEqual(calls[-1], "wechat_native_http")
                self.assertEqual(sidebar_api._bridge_worker_state(data_dir)["config_signature"]["send_backend"], "wechat_native_http")
            finally:
                sidebar_api._stop_bridge_worker(data_dir)

    def test_update_controls_stops_running_bridge_worker_when_send_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = (Path(tmp) / "data").resolve()
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="dry_run")

            def fake_run(root, *, stop_event=None, **_kwargs):
                while stop_event is not None and not stop_event.is_set():
                    time.sleep(0.02)

            try:
                with mock.patch("app.personal_wechat_bot.runtime.send_bridge_worker.run_bridge_worker", side_effect=fake_run):
                    sidebar_api._start_bridge_worker(data_dir, {"bridge_interval_seconds": 0.1})
                    deadline = time.time() + 2
                    while time.time() < deadline and not sidebar_api._bridge_worker_state(data_dir)["running"]:
                        time.sleep(0.02)

                    result = update_sidebar_controls(
                        data_dir,
                        {"mode": "confirm", "send_enabled": False, "send_driver": "bridge_outbox", "send_backend": "dry_run"},
                    )

                self.assertEqual(result["status"], "ok")
                self.assertFalse(result["bridge_worker"]["running"])
                self.assertEqual(result["bridge_worker"]["last_status"], "stopped")
            finally:
                sidebar_api._stop_bridge_worker(data_dir)

    def test_background_send_status_reports_worker_down(self) -> None:
        # bridge_outbox + send_enabled but no live worker lock: the status must
        # reflect that nothing is delivering, not a config-only "ready".
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="wechat_native_http")
            config = load_config(data_dir)

            # No worker lock at all -> worker down.
            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.wechat_native_http_status",
                return_value={"available": True, "reason": "", "health": {"IsLogin": 1}},
            ):
                status = sidebar_api._background_send_status(config, {"pending_count": 0}, data_dir)
            self.assertEqual(status, "bridge_outbox_worker_down")

            # A pending backlog with no live worker -> down-with-backlog.
            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.wechat_native_http_status",
                return_value={"available": True, "reason": "", "health": {"IsLogin": 1}},
            ):
                status = sidebar_api._background_send_status(config, {"pending_count": 3}, data_dir)
            self.assertEqual(status, "bridge_outbox_worker_down_backlog")

    def test_background_send_status_reports_wechat_native_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="wechat_native_http")
            config = load_config(data_dir)

            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.wechat_native_http_status",
                return_value={"available": False, "reason": "wechat_native_not_login"},
            ):
                status = sidebar_api._background_send_status(config, {"pending_count": 3}, data_dir)

            self.assertEqual(status, "bridge_outbox_wechat_native_http_unavailable")

    def test_background_send_status_reports_weflow_token_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="weflow_http")
            config = load_config(data_dir)

            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.weflow_http_status",
                return_value={"available": False, "token_present": False, "reason": ""},
            ):
                status = sidebar_api._background_send_status(config, {"pending_count": 0}, data_dir)

            self.assertEqual(status, "bridge_outbox_weflow_token_missing")

    def test_background_send_status_reports_weflow_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="weflow_http")
            config = load_config(data_dir)

            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.weflow_http_status",
                return_value={"available": False, "token_present": True, "reason": "http_404"},
            ):
                status = sidebar_api._background_send_status(config, {"pending_count": 0}, data_dir)

            self.assertEqual(status, "bridge_outbox_weflow_http_unavailable")

    def test_background_send_status_reports_weflow_send_not_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="weflow_http")
            config = load_config(data_dir)

            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.weflow_http_status",
                return_value={
                    "available": True,
                    "token_present": True,
                    "reason": "",
                    "send_capabilities": {"text": {"supports": False}},
                },
            ):
                status = sidebar_api._background_send_status(config, {"pending_count": 0}, data_dir)

            self.assertEqual(status, "bridge_outbox_weflow_send_not_supported")

    def test_background_send_status_reports_stale_worker_before_weflow_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="dry_run")
            old_config = load_config(data_dir)
            stale_signature = sidebar_api._bridge_worker_config_signature(old_config)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="weflow_http")
            current_config = load_config(data_dir)
            lock_path = data_dir / "send_bridge" / ".bridge_worker.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "label": "send_bridge_worker",
                        "heartbeat_at": time.time(),
                        "backend_name": "dry_run",
                        "config_signature": stale_signature,
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.weflow_http_status",
                return_value={
                    "available": True,
                    "token_present": True,
                    "reason": "",
                    "send_capabilities": {"text": {"supports": False}},
                },
            ):
                bridge = sidebar_api.build_sidebar_bridge_state(data_dir)
                status = sidebar_api._background_send_status(current_config, bridge, data_dir)

            self.assertEqual(status, "bridge_outbox_worker_stale_config")

    def test_background_send_status_ignores_dead_stale_worker_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="dry_run")
            old_config = load_config(data_dir)
            stale_signature = sidebar_api._bridge_worker_config_signature(old_config)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="weflow_http")
            current_config = load_config(data_dir)
            lock_path = data_dir / "send_bridge" / ".bridge_worker.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 99999999,
                        "label": "send_bridge_worker",
                        "heartbeat_at": time.time(),
                        "backend_name": "dry_run",
                        "config_signature": stale_signature,
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.weflow_http_status",
                return_value={
                    "available": True,
                    "token_present": True,
                    "reason": "",
                    "send_capabilities": {"text": {"supports": False}},
                },
            ):
                bridge = sidebar_api.build_sidebar_bridge_state(data_dir)
                status = sidebar_api._background_send_status(current_config, bridge, data_dir)

            self.assertEqual(bridge["worker"]["config_status"], "not_running")
            self.assertEqual(status, "bridge_outbox_weflow_send_not_supported")

    def test_background_send_status_reports_wechat_native_unavailable_without_worker_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="wechat_native_http")
            config = load_config(data_dir)

            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.wechat_native_http_status",
                return_value={"available": False, "reason": "wechat_native_not_login"},
            ):
                status = sidebar_api._background_send_status(config, {"pending_count": 0}, data_dir)

            self.assertEqual(status, "bridge_outbox_wechat_native_http_unavailable")

    def test_background_send_status_reports_weflow_ready_when_worker_lock_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="weflow_http")
            config = load_config(data_dir)
            lock_path = data_dir / "send_bridge" / ".bridge_worker.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps({"pid": os.getpid(), "label": "send_bridge_worker", "heartbeat_at": time.time()}),
                encoding="utf-8",
            )

            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.weflow_http_status",
                return_value={"available": True, "token_present": True, "reason": ""},
            ):
                status = sidebar_api._background_send_status(config, {"pending_count": 0}, data_dir)

            self.assertEqual(status, "bridge_outbox_worker_config_unknown")

            lock_payload = {
                "pid": os.getpid(),
                "label": "send_bridge_worker",
                "heartbeat_at": time.time(),
                "backend_name": "weflow_http",
                "config_signature": sidebar_api._bridge_worker_config_signature(config),
            }
            lock_path.write_text(json.dumps(lock_payload), encoding="utf-8")
            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.weflow_http_status",
                return_value={"available": True, "token_present": True, "reason": ""},
            ):
                status = sidebar_api._background_send_status(config, {"pending_count": 0}, data_dir)

            self.assertEqual(status, "bridge_outbox_ready")

    def test_background_send_status_ready_when_worker_lock_fresh(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="wechat_native_http")
            config = load_config(data_dir)

            # Simulate a live worker by writing a fresh heartbeat lock.
            lock_path = data_dir / "send_bridge" / ".bridge_worker.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                _json.dumps({"pid": os.getpid(), "label": "send_bridge_worker", "heartbeat_at": time.time()}),
                encoding="utf-8",
            )
            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.wechat_native_http_status",
                return_value={"available": True, "reason": "", "health": {"IsLogin": 1}},
            ):
                status = sidebar_api._background_send_status(config, {"pending_count": 2}, data_dir)
            self.assertEqual(status, "bridge_outbox_worker_config_unknown")

            lock_payload = {
                "pid": os.getpid(),
                "label": "send_bridge_worker",
                "heartbeat_at": time.time(),
                "backend_name": "wechat_native_http",
                "config_signature": sidebar_api._bridge_worker_config_signature(config),
            }
            lock_path.write_text(_json.dumps(lock_payload), encoding="utf-8")
            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.wechat_native_http_status",
                return_value={"available": True, "reason": "", "health": {"IsLogin": 1}},
            ), mock.patch.dict(os.environ, {"WEFLOW_API_TOKEN": "token"}, clear=False):
                status = sidebar_api._background_send_status(config, {"pending_count": 2}, data_dir)
            self.assertEqual(status, "bridge_outbox_ready")

            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.wechat_native_http_status",
                return_value={"available": True, "reason": "", "health": {"IsLogin": 1}},
            ), mock.patch.dict(os.environ, {}, clear=True):
                status = sidebar_api._background_send_status(
                    config,
                    {"pending_count": 2, "active_unverified_count": 0},
                    data_dir,
                )
            self.assertEqual(status, "bridge_outbox_ready")

            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.wechat_native_http_status",
                return_value={"available": True, "reason": "", "health": {"IsLogin": 1}},
            ), mock.patch.dict(os.environ, {}, clear=True):
                status = sidebar_api._background_send_status(
                    config,
                    {"pending_count": 0, "active_unverified_count": 1},
                    data_dir,
                )
            self.assertEqual(status, "bridge_outbox_wechat_native_accepted_unverified")

    def test_background_send_status_reports_stale_worker_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="dry_run")
            old_config = load_config(data_dir)
            stale_signature = sidebar_api._bridge_worker_config_signature(old_config)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="wechat_native_http")
            current_config = load_config(data_dir)
            lock_path = data_dir / "send_bridge" / ".bridge_worker.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "label": "send_bridge_worker",
                        "heartbeat_at": time.time(),
                        "backend_name": "dry_run",
                        "config_signature": stale_signature,
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch(
                "app.personal_wechat_bot.control.sidebar_api.wechat_native_http_status",
                return_value={"available": True, "reason": "", "health": {"IsLogin": 1}},
            ):
                bridge = sidebar_api.build_sidebar_bridge_state(data_dir)
                status = sidebar_api._background_send_status(current_config, bridge, data_dir)

            self.assertEqual(status, "bridge_outbox_worker_stale_config")
            self.assertEqual(bridge["worker"]["config_status"], "stale")


if __name__ == "__main__":
    unittest.main()

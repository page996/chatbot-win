from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.personal_wechat_bot.config.loader import create_default_config
from app.personal_wechat_bot.config.loader import load_config
from app.personal_wechat_bot.control.sidebar_server import _handler_factory
from app.personal_wechat_bot.conversation.channel_store import ConversationChannelStore
from app.personal_wechat_bot.domain.models import ReplyCandidate
from app.personal_wechat_bot.domain.models import NormalizedMessage
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue
from app.personal_wechat_bot.wechat_driver.bridge_send import BridgeAckStatus, BridgeOutboxStore


def _edge_executable() -> Path | None:
    resolved = shutil.which("msedge")
    if resolved:
        return Path(resolved)
    for candidate in (
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files (x86)/Microsoft/EdgeCore/Optimized/msedge.exe"),
    ):
        if candidate.exists():
            return candidate
    return None


class SidebarServerTest(unittest.TestCase):
    def test_sidebar_server_serves_state_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                state = json.loads(urlopen(f"http://{host}:{port}/api/state", timeout=5).read().decode("utf-8"))
                index = urlopen(f"http://{host}:{port}/", timeout=5).read().decode("utf-8")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(state["status"], "ok")
            self.assertIn("微信 Agent 后台控制台", index)
            self.assertIn("清空历史数据", index)
            self.assertIn('input id="modelName" type="text"', index)
            self.assertIn('list="modelNameOptions"', index)
            self.assertIn("gpt-5.4", index)
            self.assertIn("gpt-5.4-mini", index)
            self.assertIn("gpt-5.5", index)
            self.assertIn("deepseek-v4-flash", index)
            self.assertIn("deepseek-v4-pro", index)
            self.assertIn("<strong>API 密钥池</strong>", index)
            self.assertIn('id="agentTickButton"', index)
            self.assertIn('id="agentStartButton"', index)
            self.assertIn('id="agentStopButton"', index)
            self.assertIn('id="agentStatusSummary"', index)
            self.assertIn('id="diagnosticsExportButton"', index)
            self.assertIn('id="storageStatusButton"', index)
            self.assertIn('id="nativeMigrationProbeButton"', index)
            self.assertIn('id="sendBackendSelect"', index)
            removed_backend = "w" + "cf"
            self.assertNotIn(f'value="{removed_backend}"', index)
            self.assertIn('value="wechat_native_http"', index)
            self.assertIn("对话 Agent", index)
            self.assertIn("locked-note", index)
            self.assertNotIn('id="weflowContextOnly"', index)
            self.assertNotIn('<select id="modelName"', index)
            self.assertNotIn("<h2>API 密钥池</h2>", index)
            self.assertNotIn('id="refreshKeys"', index)
            self.assertIn("wechat_window_probe", state)

    def test_sidebar_server_serves_dirty_state_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                script = urlopen(f"http://{host}:{port}/app.js", timeout=5).read().decode("utf-8")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertIn("controlsDirty", script)
            self.assertIn("controlsSaving", script)
            self.assertIn("markControlsDirty", script)
            self.assertNotIn("delayedQueueAction", script)
            self.assertNotIn("countdown", script)
            self.assertIn("force: true", script)
            self.assertIn("setActiveStatus", script)
            self.assertIn("setStatusMessage", script)
            self.assertIn("probeNow", script)
            self.assertIn("renderBridge", script)
            self.assertIn("/api/bridge/ack", script)
            self.assertIn("/api/bridge/retry", script)
            self.assertIn("renderRuntimeCards", script)
            self.assertIn("/api/runtime-cards/", script)
            self.assertIn("probeRuntimeGpu", script)
            self.assertIn("/api/runtime/probe", script)
            self.assertIn("clearHistoryData", script)
            self.assertIn("/api/history/clear", script)
            self.assertIn("shutdown_processes", script)
            self.assertIn("shutdown_scheduled", script)
            self.assertNotIn("restart_processes", script)
            self.assertNotIn("restart_scheduled", script)
            self.assertIn("window.confirm", script)
            self.assertIn("MODEL_QUICK_OPTIONS", script)
            self.assertIn("setModelSuggestions", script)
            self.assertIn("modelQuickSelect", script)
            self.assertIn("await loadKeyPool()", script)
            self.assertNotIn("#refreshKeys", script)
            self.assertIn("savePersonaCard", script)
            self.assertIn("queued_to_bridge", script)
            self.assertIn("syncBackendTask", script)
            self.assertIn("/api/tasks", script)
            self.assertIn("renderLaneControl", script)
            self.assertIn("/api/channel-state", script)
            self.assertIn("lane-pin-input", script)
            self.assertIn('"pin"', script)
            self.assertIn("toggleTaskEvents", script)
            self.assertIn("task-event-list", script)
            self.assertIn("renderDispatchPreview", script)
            self.assertIn("dispatch-preview-panel", script)
            self.assertIn("channel_pinned", script)
            self.assertIn("channelLaneOpenState", script)
            self.assertIn("runAgentTick", script)
            self.assertIn("runAgentWorkerAction", script)
            self.assertIn("agentWorkerStatusText", script)
            self.assertIn("selectedSendBackend", script)
            self.assertIn("dry_run_not_delivered", script)
            self.assertIn("bridge_outbox_dry_run_backend", script)
            self.assertIn("topicProgressText", script)
            self.assertIn("renderAgentStatus", script)
            self.assertIn("exportDiagnosticsBundle", script)
            self.assertIn("/api/diagnostics/export", script)
            self.assertIn("inspectStorageStatus", script)
            self.assertIn("/api/storage/status", script)
            self.assertIn("nativeMigrationProbe", script)
            self.assertIn("/api/native/migration-probe", script)
            self.assertIn("diagnostic:native-migration", script)
            self.assertIn("diagnostic:export", script)
            self.assertIn("history:storage-status", script)
            self.assertIn("/api/agent/tick", script)
            self.assertIn("/api/agent/${action}", script)
            self.assertIn("agent:tick", script)
            self.assertIn("agent:worker", script)

    def test_sidebar_buttons_are_bound_or_form_owned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                index = urlopen(f"http://{host}:{port}/", timeout=5).read().decode("utf-8")
                script = urlopen(f"http://{host}:{port}/app.js", timeout=5).read().decode("utf-8")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            button_ids = set(re.findall(r"<button[^>]+id=\"([^\"]+)\"", index))
            task_bound = set(re.findall(r"bindTaskButton\(\"#([^\"]+)\"", script))
            listener_bound = set(re.findall(r"\$\(('#|\"#)([^'\"]+)['\"]\)\??\.addEventListener", script))
            listener_ids = {item[1] for item in listener_bound}
            form_owned = {"saveModelConfig"}

            self.assertEqual(button_ids - task_bound - listener_ids - form_owned, set())

    def test_sidebar_api_calls_are_routed_by_server(self) -> None:
        script = Path("app/personal_wechat_bot/ui/sidebar/app.js").read_text(encoding="utf-8")
        server_source = Path("app/personal_wechat_bot/control/sidebar_server.py").read_text(encoding="utf-8")

        static_calls = set(re.findall(r"api\(\s*['\"](/api/[^'\"]+)['\"]", script))
        template_calls = set(re.findall(r"api\(\s*`(/api/[^`]+)`", script))
        exact_routes = set(re.findall(r"parsed\.path == \"([^\"]+)\"", server_source))
        dynamic_templates = {
            "/api/agent/${action}",
            "/api/channels/${encodeURIComponent(id)}/test-file",
            "/api/channels/${encodeURIComponent(id)}/test-reply",
            "/api/channels/delete/${encodeURIComponent(conversationId)}",
            "/api/queue/${encodeURIComponent(queueId)}/${action}",
            "/api/queue/${encodeURIComponent(queueId)}/remove",
            "/api/runtime-cards/${action}",
            "/api/weflow/${action}",
        }

        self.assertEqual(static_calls - exact_routes, set())
        self.assertEqual(template_calls - dynamic_templates, set())

    def test_sidebar_server_serves_wechat_probe_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                payload = json.loads(urlopen(f"http://{host}:{port}/api/wechat-probe", timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertIn(payload["status"], {"ok", "not_found"})
            self.assertIn("windows", payload)
            self.assertIn("ui_automation", payload)

    def test_sidebar_server_routes_runtime_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                with mock.patch(
                    "app.personal_wechat_bot.control.sidebar_server.sidebar_runtime_probe",
                    return_value={"status": "ok", "same_path_as_ingest": True},
                ):
                    request = Request(
                        f"http://{host}:{port}/api/runtime/probe",
                        data=json.dumps({"ocr_mode": "gpu", "asr_mode": "gpu"}).encode("utf-8"),
                        headers={"content-type": "application/json"},
                        method="POST",
                    )
                    payload = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["same_path_as_ingest"])

    def test_sidebar_server_accepts_backend_event_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            event_file = data_dir / "backend_events.jsonl"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                request = Request(
                    f"http://{host}:{port}/api/backend-events",
                    data=json.dumps(
                        {
                            "event_file": str(event_file),
                            "chat_title": "PAGE",
                            "sender_name": "PAGE",
                            "text": "current",
                            "history": [{"sender_name": "PAGE", "text": "old"}],
                        }
                    ).encode("utf-8"),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                payload = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            raw_event = json.loads(event_file.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["capture_source"], "backend_http_ingest")
            self.assertEqual(raw_event["history"][0]["text"], "old")

    def test_sidebar_queue_action_decodes_encoded_queue_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            queue_id = ConfirmQueue(data_dir / "confirm_queue.jsonl").enqueue(
                ReplyCandidate(
                    message_id="message-1",
                    conversation_id="private-1",
                    text="hello",
                    send_mode="confirm",
                    model="fake",
                    created_at="2026-06-29T00:00:00+00:00",
                )
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                encoded = quote(queue_id, safe="")
                request = Request(
                    f"http://{host}:{port}/api/queue/{encoded}/approve",
                    data=b'{"reviewer":"test"}',
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                payload = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["item"]["status"], "approved")

    def test_sidebar_server_serves_bridge_state_and_ack_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            record = BridgeOutboxStore(data_dir).enqueue("private-1", "hello")
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                state = json.loads(urlopen(f"http://{host}:{port}/api/bridge", timeout=5).read().decode("utf-8"))
                request = Request(
                    f"http://{host}:{port}/api/bridge/ack",
                    data=json.dumps({"bridge_id": record["bridge_id"], "status": "sent"}).encode("utf-8"),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                ack = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(state["status"], "ok")
            self.assertEqual(state["pending_count"], 1)
            self.assertEqual(ack["status"], "ok")

    def test_sidebar_server_serves_bridge_retry_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            record = store.enqueue("private-1", "hello")
            store.append_ack(
                record["bridge_id"],
                status=BridgeAckStatus.FAILED,
                reason="wechat_native_http_send_text_error:ConnectionRefusedError:refused",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                request = Request(
                    f"http://{host}:{port}/api/bridge/retry",
                    data=json.dumps({"bridge_id": record["bridge_id"], "reviewer": "tester"}).encode("utf-8"),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                with mock.patch("app.personal_wechat_bot.control.sidebar_api._start_bridge_worker"):
                    retry = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
                state = json.loads(urlopen(f"http://{host}:{port}/api/bridge", timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(retry["status"], "ok")
            self.assertTrue(any(item["bridge_id"] == retry["new_bridge_id"] for item in state["items"]))
            self.assertEqual(state["pending_count"], 1)

    def test_sidebar_server_serves_runtime_cards_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                state = json.loads(urlopen(f"http://{host}:{port}/api/runtime-cards", timeout=5).read().decode("utf-8"))
                request = Request(
                    f"http://{host}:{port}/api/runtime-cards/save-task",
                    data=json.dumps({"name": "HTTP 任务卡", "content": "通过 HTTP 装备"}).encode("utf-8"),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                saved = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(state["status"], "ok")
            self.assertEqual(saved["status"], "ok")
            self.assertIn("通过 HTTP 装备", saved["runtime_cards"]["active"]["tasks"][0]["content"])

    def test_sidebar_server_serves_task_manager_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                request = Request(
                    f"http://{host}:{port}/api/tasks",
                    data=json.dumps(
                        {
                            "action": "create",
                            "task": {
                                "task_id": "http-task-1",
                                "title": "HTTP task",
                                "conversation_id": "conv-a",
                            },
                        }
                    ).encode("utf-8"),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                created = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
                events_request = Request(
                    f"http://{host}:{port}/api/tasks",
                    data=json.dumps({"action": "events", "task_id": "http-task-1"}).encode("utf-8"),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                events = json.loads(urlopen(events_request, timeout=5).read().decode("utf-8"))
                state = json.loads(urlopen(f"http://{host}:{port}/api/tasks", timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(created["status"], "ok")
            self.assertEqual(created["task"]["task_id"], "http-task-1")
            self.assertEqual(events["status"], "ok")
            self.assertTrue(any(item["event"] == "created" for item in events["events"]))
            self.assertEqual(state["counts"]["queued"], 1)

    def test_sidebar_server_routes_channel_state_api(self) -> None:
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
            channel_store.ensure_channel(
                NormalizedMessage(
                    message_id="msg-http-channel",
                    conversation_id="conv-http",
                    conversation_type="private",
                    chat_title="HTTP Channel",
                    sender_name="HTTP Channel",
                    sender_wechat_id="wxid_http_channel",
                    text="hello",
                    is_self=False,
                    received_at="2026-07-08T10:00:00+08:00",
                    metadata={"source": "test", "trusted_channel_source": True},
                )
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                request = Request(
                    f"http://{host}:{port}/api/channel-state",
                    data=json.dumps(
                        {
                            "action": "pause",
                            "conversation_id": "conv-http",
                            "wait_reason": "HTTP pause",
                            "priority": 87,
                            "updated_by": "server-test",
                        }
                    ).encode("utf-8"),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                payload = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["channel_state"]["control"]["mode"], "paused")
            self.assertEqual(payload["channel_state"]["control"]["priority"], 87)
            self.assertEqual(payload["channel_state"]["control"]["wait_reason"], "HTTP pause")
            self.assertIn("task_manager", payload)
            self.assertEqual(payload["channels"]["items"][0]["state"]["effective_status"], "paused")

    def test_sidebar_server_channel_controls_roundtrip_into_state_and_dispatch_preview(self) -> None:
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
            channel_store.ensure_channel(
                NormalizedMessage(
                    message_id="msg-http-dynamic",
                    conversation_id="conv-http-dynamic",
                    conversation_type="private",
                    chat_title="Dynamic Channel",
                    sender_name="Dynamic Channel",
                    sender_wechat_id="wxid_http_dynamic",
                    text="hello",
                    is_self=False,
                    received_at="2026-07-08T10:00:00+08:00",
                    metadata={"source": "test", "trusted_channel_source": True},
                )
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                base_url = f"http://{host}:{port}"

                def post(path: str, payload: dict[str, object]) -> dict[str, object]:
                    request = Request(
                        base_url + path,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"content-type": "application/json"},
                        method="POST",
                    )
                    return json.loads(urlopen(request, timeout=5).read().decode("utf-8"))

                post(
                    "/api/tasks",
                    {
                        "action": "create",
                        "task": {
                            "task_id": "dyn-pinned",
                            "title": "Pinned dynamic task",
                            "conversation_id": "conv-http-dynamic",
                            "scope": "conversation:conv-http-dynamic",
                            "resource_class": "cpu_io",
                            "priority": 10,
                        },
                    },
                )
                post(
                    "/api/tasks",
                    {
                        "action": "create",
                        "task": {
                            "task_id": "dyn-normal",
                            "title": "Normal dynamic task",
                            "conversation_id": "conv-http-normal",
                            "scope": "conversation:conv-http-normal",
                            "resource_class": "cpu_io",
                            "priority": 90,
                        },
                    },
                )
                pinned = post(
                    "/api/channel-state",
                    {"action": "pin", "conversation_id": "conv-http-dynamic", "updated_by": "server-test"},
                )
                state_after_pin = json.loads(urlopen(f"{base_url}/api/state", timeout=5).read().decode("utf-8"))
                paused = post(
                    "/api/channel-state",
                    {
                        "action": "pause",
                        "conversation_id": "conv-http-dynamic",
                        "wait_reason": "roundtrip pause",
                        "updated_by": "server-test",
                    },
                )
                state_after_pause = json.loads(urlopen(f"{base_url}/api/state", timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            channel_after_pin = state_after_pin["channels"]["items"][0]["state"]
            preview_after_pin = state_after_pin["task_manager"]["scheduler"]["dispatch_preview"]
            blocked_after_pause = {
                item["task_id"]: item["reason"]
                for item in state_after_pause["task_manager"]["scheduler"]["dispatch_preview"]["blocked"]
            }

            self.assertEqual(pinned["status"], "ok")
            self.assertTrue(channel_after_pin["control"]["pinned"])
            self.assertEqual(preview_after_pin["runnable"][0]["task_id"], "dyn-pinned")
            self.assertTrue(preview_after_pin["runnable"][0]["channel_pinned"])
            self.assertEqual(paused["channel_state"]["control"]["mode"], "paused")
            self.assertEqual(state_after_pause["channels"]["items"][0]["state"]["effective_status"], "paused")
            self.assertEqual(blocked_after_pause["dyn-pinned"], "channel_paused:conv-http-dynamic")

    def test_sidebar_headless_edge_renders_channel_control_and_dispatch_preview(self) -> None:
        edge = _edge_executable()
        if edge is None:
            self.skipTest("Microsoft Edge was not found for headless sidebar smoke")
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
            channel_store.ensure_channel(
                NormalizedMessage(
                    message_id="msg-browser-smoke",
                    conversation_id="conv-browser-smoke",
                    conversation_type="private",
                    chat_title="Browser Smoke Channel",
                    sender_name="Browser Smoke Channel",
                    sender_wechat_id="wxid_browser_smoke",
                    text="hello",
                    is_self=False,
                    received_at="2026-07-08T10:00:00+08:00",
                    metadata={"source": "test", "trusted_channel_source": True},
                )
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                base_url = f"http://{host}:{port}"

                def post(path: str, payload: dict[str, object]) -> dict[str, object]:
                    request = Request(
                        base_url + path,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"content-type": "application/json"},
                        method="POST",
                    )
                    return json.loads(urlopen(request, timeout=5).read().decode("utf-8"))

                post(
                    "/api/tasks",
                    {
                        "action": "create",
                        "task": {
                            "task_id": "browser-smoke-task",
                            "title": "Browser dynamic task",
                            "conversation_id": "conv-browser-smoke",
                            "scope": "conversation:conv-browser-smoke",
                            "resource_class": "cpu_io",
                            "priority": 80,
                        },
                    },
                )
                post(
                    "/api/channel-state",
                    {"action": "pin", "conversation_id": "conv-browser-smoke", "updated_by": "browser-smoke"},
                )
                completed = subprocess.run(
                    [
                        str(edge),
                        "--headless=new",
                        "--disable-gpu",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-extensions",
                        f"--user-data-dir={Path(tmp) / 'edge-profile'}",
                        "--virtual-time-budget=5000",
                        "--dump-dom",
                        base_url + "/",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            dom = completed.stdout or ""
            self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
            self.assertIn("Browser Smoke Channel", dom)
            self.assertIn("Browser dynamic task", dom)
            self.assertIn("dispatch-preview-panel", dom)
            self.assertIn("lane-control-panel mode-active is-pinned", dom)
            self.assertIn("lane-pin-input", dom)
            self.assertIn('type="checkbox" checked=""', dom)

    def test_sidebar_server_routes_resource_audit_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                with mock.patch(
                    "app.personal_wechat_bot.control.sidebar_server.sidebar_resource_audit",
                    return_value={"status": "ok", "schema": "local_resource_audit_v1"},
                ):
                    request = Request(
                        f"http://{host}:{port}/api/resources/audit",
                        data=json.dumps({"manual": True}).encode("utf-8"),
                        headers={"content-type": "application/json"},
                        method="POST",
                    )
                    payload = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["schema"], "local_resource_audit_v1")

    def test_sidebar_server_routes_native_migration_probe_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                with mock.patch(
                    "app.personal_wechat_bot.control.sidebar_server.sidebar_native_migration_probe",
                    return_value={"status": "ready", "schema": "native_migration_probe_v1"},
                ):
                    request = Request(
                        f"http://{host}:{port}/api/native/migration-probe",
                        data=json.dumps({"persist": False}).encode("utf-8"),
                        headers={"content-type": "application/json"},
                        method="POST",
                    )
                    payload = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(payload["status"], "ready")
            self.assertEqual(payload["schema"], "native_migration_probe_v1")

    def test_sidebar_server_routes_storage_status_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                request = Request(
                    f"http://{host}:{port}/api/storage/status",
                    data=json.dumps({"include_sizes": False}).encode("utf-8"),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                payload = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["schema"], "storage_migration_status_v1")
            self.assertIn("migration_boundaries", payload)

    def test_sidebar_server_routes_agent_tick_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                with mock.patch(
                    "app.personal_wechat_bot.control.sidebar_server.sidebar_agent_tick",
                    return_value={"status": "ok", "agent": {"processed_count": 0}},
                ):
                    request = Request(
                        f"http://{host}:{port}/api/agent/tick",
                        data=json.dumps({"loops": 1}).encode("utf-8"),
                        headers={"content-type": "application/json"},
                        method="POST",
                    )
                    payload = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["agent"]["processed_count"], 0)

    def test_sidebar_server_routes_agent_worker_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                with mock.patch(
                    "app.personal_wechat_bot.control.sidebar_server.sidebar_agent_start",
                    return_value={"status": "ok", "worker": {"running": True}},
                ), mock.patch(
                    "app.personal_wechat_bot.control.sidebar_server.sidebar_agent_stop",
                    return_value={"status": "ok", "worker": {"running": False}},
                ):
                    start_request = Request(
                        f"http://{host}:{port}/api/agent/start",
                        data=json.dumps({"interval_seconds": 0.1}).encode("utf-8"),
                        headers={"content-type": "application/json"},
                        method="POST",
                    )
                    start_payload = json.loads(urlopen(start_request, timeout=5).read().decode("utf-8"))
                    stop_request = Request(
                        f"http://{host}:{port}/api/agent/stop",
                        data=json.dumps({}).encode("utf-8"),
                        headers={"content-type": "application/json"},
                        method="POST",
                    )
                    stop_payload = json.loads(urlopen(stop_request, timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(start_payload["status"], "ok")
            self.assertTrue(start_payload["worker"]["running"])
            self.assertEqual(stop_payload["status"], "ok")
            self.assertFalse(stop_payload["worker"]["running"])

    def test_sidebar_server_routes_weflow_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                # A system account short-circuits before touching WeFlow, so this
                # proves the route reaches sidebar_weflow_backfill without needing
                # a live bridge.
                request = Request(
                    f"http://{host}:{port}/api/weflow/backfill",
                    data=json.dumps({"talkers": ["filehelper"]}).encode("utf-8"),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                payload = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["backfilled_talkers"], [])

    def test_cross_origin_post_is_rejected(self) -> None:
        # A malicious page in the operator's browser must not be able to drive a
        # mutating endpoint (here: model-config, which could redirect the key).
        from urllib.error import HTTPError

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                request = Request(
                    f"http://{host}:{port}/api/model-config",
                    data=json.dumps({"provider": "relay"}).encode("utf-8"),
                    headers={
                        "content-type": "application/json",
                        "origin": "http://evil.example.com",
                    },
                    method="POST",
                )
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(request, timeout=5)
                self.assertEqual(ctx.exception.code, 403)
                body = json.loads(ctx.exception.read().decode("utf-8"))
                self.assertEqual(body["error"], "cross_origin_forbidden")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_non_json_content_type_post_is_rejected(self) -> None:
        # A simple cross-site form POST (text/plain, no preflight) must be
        # blocked by the content-type gate.
        from urllib.error import HTTPError

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                request = Request(
                    f"http://{host}:{port}/api/model-config",
                    data=b'{"provider":"relay"}',
                    headers={"content-type": "text/plain"},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as ctx:
                    urlopen(request, timeout=5)
                self.assertEqual(ctx.exception.code, 403)
                body = json.loads(ctx.exception.read().decode("utf-8"))
                self.assertEqual(body["error"], "unsupported_content_type")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_same_origin_post_is_allowed(self) -> None:
        # A request whose Origin host matches our Host header passes the guard.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(data_dir))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                request = Request(
                    f"http://{host}:{port}/api/model-config",
                    data=json.dumps({}).encode("utf-8"),
                    headers={
                        "content-type": "application/json",
                        "origin": f"http://{host}:{port}",
                    },
                    method="POST",
                )
                payload = json.loads(urlopen(request, timeout=5).read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(payload["status"], "ok")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.personal_wechat_bot.config.loader import create_default_config
from app.personal_wechat_bot.control.sidebar_server import _handler_factory
from app.personal_wechat_bot.domain.models import ReplyCandidate
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue
from app.personal_wechat_bot.wechat_driver.bridge_send import BridgeOutboxStore


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
            self.assertIn("微信 Agent 审计面板", index)
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
            self.assertIn("delayedQueueAction", script)
            self.assertIn("countdown", script)
            self.assertIn("force: true", script)
            self.assertIn("setActiveStatus", script)
            self.assertIn("setStatusMessage", script)
            self.assertIn("probeNow", script)
            self.assertIn("renderBridge", script)
            self.assertIn("/api/bridge/ack", script)
            self.assertIn("renderRuntimeCards", script)
            self.assertIn("/api/runtime-cards/", script)
            self.assertIn("savePersonaCard", script)
            self.assertIn("queued_to_bridge", script)

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

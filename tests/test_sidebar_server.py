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
            self.assertIn("WeChat Agent Console", index)
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


if __name__ == "__main__":
    unittest.main()

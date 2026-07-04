from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.personal_wechat_bot.config.loader import create_default_config
from app.personal_wechat_bot.control.sidebar_api import build_sidebar_weflow_state, run_weflow_backfill_sync, sidebar_weflow_backfill, sidebar_weflow_discover_sessions
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.normalizer.normalizer import conversation_id_for
from app.personal_wechat_bot.wechat_driver.hook_source_bridge import WEFLOW_LOCAL_BUILD_FLAVOR


class WeflowBackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        create_default_config(self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_backfill_pulls_full_history_as_context_only(self) -> None:
        with _FakeWeFlowServer() as server:
            result = sidebar_weflow_backfill(
                self.data_dir,
                {
                    "base_url": server.base_url,
                    "token": "test-token",
                    "talkers": ["wxid_history"],
                    "message_limit": 2,  # small page so backfill must walk multiple pages
                },
            )
            self.assertEqual(result.get("status"), "started", result)
            result = self._wait_for_backfill_result()

        self.assertEqual(result.get("status"), "ok", result)
        self.assertTrue(result.get("backfill"))
        self.assertEqual(result.get("backfilled_talkers"), ["wxid_history"])

        # History landed in the ledger under the talker-derived conversation_id.
        conversation_id = conversation_id_for("private", "wxid_history")
        ledger = ConversationLedgerStore(self.data_dir)
        entries = ledger.read_entries(conversation_id)
        texts = [entry.text_blocks[0]["text"] for entry in entries if entry.text_blocks]
        self.assertEqual(texts, ["msg-1", "msg-2", "msg-3", "msg-4", "msg-5"])
        # Backfilled history must be context-only: recorded but never replied to.
        processed = result.get("pull", {}).get("processed", [])
        self.assertTrue(processed)
        self.assertTrue(all(item.get("context_only") for item in processed), processed)
        self.assertFalse(any(item.get("reply") for item in processed))

    def _wait_for_backfill_result(self) -> dict:
        for _ in range(80):
            state = build_sidebar_weflow_state(self.data_dir)
            if state.get("backfill_job", {}).get("status") in {"completed", "cancelled", "error"}:
                return state.get("last_backfill", {})
            threading.Event().wait(0.05)
        return build_sidebar_weflow_state(self.data_dir).get("last_backfill", {})

    def test_backfill_skips_system_accounts(self) -> None:
        with _FakeWeFlowServer():
            result = sidebar_weflow_backfill(
                self.data_dir,
                {"base_url": "http://127.0.0.1:1", "talkers": ["filehelper"]},
            )
        self.assertEqual(result.get("status"), "error")
        self.assertEqual(result.get("backfilled_talkers"), [])

    def test_sync_backfill_runs_inline_for_cli(self) -> None:
        # The CLI path (run_weflow_backfill_sync) must complete synchronously and
        # return the final result — a short-lived CLI process cannot poll a
        # daemon thread. Same context-only, full-history semantics as the async UI path.
        with _FakeWeFlowServer() as server:
            result = run_weflow_backfill_sync(
                self.data_dir,
                {
                    "base_url": server.base_url,
                    "token": "test-token",
                    "talkers": ["wxid_history"],
                    "message_limit": 2,
                },
            )

        self.assertEqual(result.get("status"), "ok", result)
        self.assertTrue(result.get("backfill"))
        self.assertEqual(result.get("backfilled_talkers"), ["wxid_history"])
        conversation_id = conversation_id_for("private", "wxid_history")
        ledger = ConversationLedgerStore(self.data_dir)
        texts = [entry.text_blocks[0]["text"] for entry in ledger.read_entries(conversation_id) if entry.text_blocks]
        self.assertEqual(texts, ["msg-1", "msg-2", "msg-3", "msg-4", "msg-5"])
        processed = result.get("pull", {}).get("processed", [])
        self.assertTrue(processed)
        self.assertTrue(all(item.get("context_only") for item in processed), processed)
        self.assertFalse(any(item.get("reply") for item in processed))

    def test_sync_backfill_rejects_system_accounts(self) -> None:
        result = run_weflow_backfill_sync(self.data_dir, {"talkers": ["filehelper"]})
        self.assertEqual(result.get("status"), "error")
        self.assertEqual(result.get("backfilled_talkers"), [])

    def test_discover_sessions_uses_ready_bridge_and_persists_status(self) -> None:
        with _FakeWeFlowServer() as server:
            result = sidebar_weflow_discover_sessions(
                self.data_dir,
                {"base_url": server.base_url, "token": "test-token", "limit": 20},
            )

        self.assertEqual(result.get("status"), "ok", result)
        self.assertEqual(result.get("count"), 2)
        self.assertEqual([item["id"] for item in result["sessions"]], ["wxid_history", "room@chatroom"])
        self.assertEqual(result["sessions"][0]["name"], "History Friend")

        state = json.loads((self.data_dir / "weflow_sidebar_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["last_discover"]["status"], "ok")
        self.assertTrue(state["token_present"])


class _FakeWeFlowServer:
    def __enter__(self):
        history = [
            {"localId": i, "serverId": f"s{i}", "localType": 1, "createTime": i * 10, "sortSeq": i * 10, "senderUsername": "wxid_history", "content": f"msg-{i}"}
            for i in range(1, 6)
        ]
        sessions = [
            {"username": "wxid_history", "displayName": "History Friend", "type": "private"},
            {"sessionId": "room@chatroom", "name": "Room", "type": "group"},
            {"id": "filehelper", "name": "File Helper", "type": "private"},
        ]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                return

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                if parsed.path in ("/health", "/api/v1/health"):
                    self._send({"status": "ok", "buildFlavor": WEFLOW_LOCAL_BUILD_FLAVOR})
                    return
                if parsed.path == "/api/v1/sessions":
                    self._send({"status": "ok", "sessions": sessions})
                    return
                if parsed.path == "/api/v1/messages":
                    query = parse_qs(parsed.query)
                    offset = int((query.get("offset") or ["0"])[0])
                    limit = int((query.get("limit") or ["100"])[0])
                    page = history[offset : offset + limit]
                    self._send(
                        {
                            "success": True,
                            "talker": (query.get("talker") or [""])[0],
                            "count": len(page),
                            "hasMore": offset + limit < len(history),
                            "messages": page,
                        }
                    )
                    return
                self.send_response(404)
                self.end_headers()

            def _send(self, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

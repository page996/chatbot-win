from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent


class _FakeWeFlowServer:
    def __enter__(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                return

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                if parsed.path == "/api/v1/messages":
                    query = parse_qs(parsed.query)
                    talker = (query.get("talker") or [""])[0]
                    messages = [
                        {
                            "senderUsername": talker,
                            "accountName": "PAGE",
                            "createTime": 1719900000,
                            "localType": 1,
                            "content": "hello from weflow",
                            "serverId": "wf-msg-1",
                            "sortSeq": 1719900000,
                        }
                    ] if talker == "wxid_page" else []
                    self._send(
                        {
                            "success": True,
                            "talker": talker,
                            "count": len(messages),
                            "hasMore": False,
                            "media": {"enabled": True, "exportPath": str(Path("C:/WeFlow/api-media"))},
                            "messages": messages,
                        }
                    )
                    return
                if self.path.startswith("/api/v1/sessions/wxid_page/messages"):
                    self._send(
                        {
                            "chatlab": {"version": "0.0.2"},
                            "meta": {"name": "PAGE", "type": "private"},
                            "messages": [
                                {
                                    "sender": "wxid_page",
                                    "accountName": "PAGE",
                                    "timestamp": 1719900000000,
                                    "type": 0,
                                    "content": "hello from weflow",
                                    "platformMessageId": "wf-msg-1",
                                }
                            ],
                            "sync": {"hasMore": False, "nextSince": 1719900000},
                        }
                    )
                    return
                if self.path.startswith("/api/v1/sessions"):
                    self._send({"sessions": [{"id": "wxid_page", "name": "PAGE", "type": "private"}]})
                    return
                if self.path.startswith("/api/v1/health") or self.path.startswith("/health"):
                    self._send({
                        "status": "ok",
                        "buildFlavor": "chatbot-win-local-fork",
                        "requiresToken": True,
                        "mediaExportPath": str(Path("C:/WeFlow/api-media")),
                    })
                    return
                self._send({"error": "not found"}, code=404)

            def _send(self, payload: dict, code: int = 200) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class BackendEventsCliTest(unittest.TestCase):
    def test_append_and_poll_backend_events_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            inbox = data_dir / "inbox"
            event_file = data_dir / "backend_events.jsonl"
            self._run("--data-dir", str(data_dir), "init")
            self._run("--data-dir", str(data_dir), "accept-contact", "PAGE")
            (inbox / "note.txt").write_text("hello backend", encoding="utf-8")

            append_output = self._run(
                "--data-dir",
                str(data_dir),
                "append-backend-event",
                "--event-file",
                str(event_file),
                "--chat-title",
                "PAGE",
                "--sender-name",
                "PAGE",
                "--text",
                "后台事件测试",
                "--attachment",
                "note.txt",
            )
            append_payload = json.loads(append_output)
            self.assertEqual(append_payload["status"], "ok")
            self.assertFalse(append_payload["send_enabled"])

            poll_payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "poll-backend-events",
                    "--event-file",
                    str(event_file),
                    "--loops",
                    "1",
                    "--interval",
                    "0",
                    "--verbose",
                )
            )

            self.assertEqual(poll_payload["status"], "stopped")
            self.assertEqual(poll_payload["processed_count"], 1)
            self.assertFalse(poll_payload["send_enabled"])
            self.assertIn("[后台附件] note.txt", poll_payload["processed"][0]["message"]["text"])

    def test_scan_backend_files_cli_then_poll_backend_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            inbox = data_dir / "inbox"
            event_file = data_dir / "backend_events.jsonl"
            self._run("--data-dir", str(data_dir), "init")
            self._run("--data-dir", str(data_dir), "accept-contact", "PAGE")
            (inbox / "scan-note.txt").write_text("scan backend", encoding="utf-8")

            scan_payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "scan-backend-files",
                    "--event-file",
                    str(event_file),
                    "--chat-title",
                    "PAGE",
                    "--sender-name",
                    "PAGE",
                )
            )

            self.assertEqual(scan_payload["created_count"], 1)
            self.assertFalse(scan_payload["send_enabled"])

            poll_payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "poll-backend-events",
                    "--event-file",
                    str(event_file),
                    "--loops",
                    "1",
                    "--interval",
                    "0",
                    "--verbose",
                )
            )

            self.assertEqual(poll_payload["processed_count"], 1)
            self.assertIn("收到后台文件: scan-note.txt", poll_payload["processed"][0]["message"]["text"])

    def test_append_backend_event_cli_accepts_quote_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            event_file = data_dir / "backend_events.jsonl"
            self._run("--data-dir", str(data_dir), "init")

            append_payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "append-backend-event",
                    "--event-file",
                    str(event_file),
                    "--chat-title",
                    "PAGE",
                    "--sender-name",
                    "PAGE",
                    "--text",
                    "引用继续处理",
                    "--quote-text",
                    "被引用的正文",
                    "--quote-message-id",
                    "quoted-id",
                    "--quote-sender-name",
                    "PAGE",
                )
            )
            raw_event = json.loads(event_file.read_text(encoding="utf-8").splitlines()[0])

            self.assertEqual(append_payload["status"], "ok")
            self.assertEqual(raw_event["quote"]["text"], "被引用的正文")
            self.assertEqual(raw_event["quote"]["message_id"], "quoted-id")

    def test_append_backend_event_cli_accepts_history_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            event_file = data_dir / "backend_events.jsonl"
            self._run("--data-dir", str(data_dir), "init")

            append_payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "append-backend-event",
                    "--event-file",
                    str(event_file),
                    "--chat-title",
                    "PAGE",
                    "--sender-name",
                    "PAGE",
                    "--text",
                    "current",
                    "--history-json",
                    '[{"sender_name":"PAGE","text":"old one"},{"sender_name":"Agent","text":"old self"}]',
                )
            )
            raw_event = json.loads(event_file.read_text(encoding="utf-8").splitlines()[0])

            self.assertEqual(append_payload["status"], "ok")
            self.assertEqual(len(raw_event["history"]), 2)
            self.assertEqual(raw_event["history"][0]["text"], "old one")

    def test_run_agent_cli_starts_backend_event_loop_without_wechat_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            event_file = Path(tmp) / "events.jsonl"
            self._run("--data-dir", str(data_dir), "init")

            payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "run-agent",
                    "--loops",
                    "1",
                    "--interval",
                    "0",
                    "--backend-event-file",
                    str(event_file),
                    "--no-wechat-ocr",
                )
            )

            self.assertEqual(payload["status"], "stopped")
            self.assertEqual(payload["loops"], 1)
            self.assertEqual(payload["runners"][0]["name"], "backend-events")

    def test_run_agent_cli_defaults_to_backend_events_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            event_file = Path(tmp) / "events.jsonl"
            self._run("--data-dir", str(data_dir), "init")

            payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "run-agent",
                    "--loops",
                    "1",
                    "--interval",
                    "0",
                    "--backend-event-file",
                    str(event_file),
                )
            )

            self.assertEqual([item["name"] for item in payload["runners"]], ["backend-events"])

    def test_page_ocr_cli_commands_are_deprecated_and_do_not_write_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            image = Path(tmp) / "wechat.bmp"
            image.write_bytes(b"not an actual image")
            self._run("--data-dir", str(data_dir), "init")

            snapshot_payload = json.loads(
                self._run("--data-dir", str(data_dir), "ocr-snapshot", str(image), "--chat-title", "PAGE")
            )
            poll_payload = json.loads(
                self._run("--data-dir", str(data_dir), "poll-ocr-window", "--loops", "1", "--interval", "0")
            )
            diagnose_payload = json.loads(
                self._run("--data-dir", str(data_dir), "ocr-window-diagnose")
            )

            self.assertEqual(snapshot_payload["status"], "deprecated")
            self.assertEqual(poll_payload["status"], "deprecated")
            self.assertEqual(diagnose_payload["status"], "deprecated")
            self.assertFalse(snapshot_payload["will_write_ledger"])
            self.assertEqual(list((data_dir / "conversation_ledgers").glob("*/messages.jsonl")), [])

    def test_wechat_voice_cache_probe_cli_resolves_readable_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            voice_root = Path(tmp) / "voice-cache"
            voice_root.mkdir()
            audio = voice_root / "msg_12345.m4a"
            audio.write_bytes(b"fake audio")
            self._run("--data-dir", str(data_dir), "init")

            payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "wechat-voice-cache-probe",
                    "--root",
                    str(voice_root),
                    "--audio-name",
                    "msg_12345",
                )
            )

            self.assertEqual(payload["status"], "resolved")
            self.assertEqual(Path(payload["result"]["path"]), audio)
            self.assertEqual(payload["capability"]["mode"], "readable_file_cache_only")
            self.assertFalse(payload["send_enabled"])

    def test_import_hook_events_cli_appends_backend_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            hook_file = Path(tmp) / "hook_events.jsonl"
            backend_file = Path(tmp) / "backend_events.jsonl"
            self._run("--data-dir", str(data_dir), "init")
            hook_file.write_text(
                json.dumps(
                    {
                        "talker": "wxid_page",
                        "talker_name": "PAGE",
                        "sender_name": "PAGE",
                        "msgid": "hook-1",
                        "text": "hook hello",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "import-hook-events",
                    "--hook-event-file",
                    str(hook_file),
                    "--backend-event-file",
                    str(backend_file),
                )
            )
            raw_event = json.loads(backend_file.read_text(encoding="utf-8").splitlines()[0])

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["appended_count"], 1)
            self.assertEqual(raw_event["text"], "hook hello")
            self.assertEqual(raw_event["source_payload"]["conversation_key"], "wxid_page")
            self.assertFalse(payload["send_enabled"])

    def test_run_agent_cli_can_import_hook_events_before_polling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            hook_file = Path(tmp) / "hook_events.jsonl"
            backend_file = Path(tmp) / "backend_events.jsonl"
            self._run("--data-dir", str(data_dir), "init")
            hook_file.write_text(
                json.dumps(
                    {
                        "talker": "wxid_page",
                        "talker_name": "PAGE",
                        "sender_name": "PAGE",
                        "msgid": "hook-2",
                        "text": "hook task",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "run-agent",
                    "--loops",
                    "1",
                    "--interval",
                    "0",
                    "--hook-event-file",
                    str(hook_file),
                    "--backend-event-file",
                    str(backend_file),
                )
            )

            self.assertEqual(payload["status"], "stopped")
            self.assertEqual(payload["processed_count"], 1)
            self.assertTrue(backend_file.exists())
            self.assertEqual(payload["runners"][0]["name"], "hook-messages")

    def test_pull_hook_messages_cli_imports_and_processes_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            hook_file = Path(tmp) / "hook_events.jsonl"
            backend_file = Path(tmp) / "backend_events.jsonl"
            self._run("--data-dir", str(data_dir), "init")
            hook_file.write_text(
                json.dumps(
                    {
                        "msgId": "pull-1",
                        "talkerId": "wxid_page",
                        "talkerName": "PAGE",
                        "senderWxid": "wxid_page",
                        "senderNickname": "PAGE",
                        "displayContent": "pull task",
                        "sortSeq": "9101",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "pull-hook-messages",
                    "--hook-event-file",
                    str(hook_file),
                    "--backend-event-file",
                    str(backend_file),
                    "--loops",
                    "1",
                    "--interval",
                    "0",
                    "--verbose",
                )
            )

            self.assertEqual(payload["status"], "stopped")
            self.assertEqual(payload["imported_count"], 1)
            self.assertEqual(payload["processed_count"], 1)
            self.assertEqual(payload["last_import"]["appended_count"], 1)
            self.assertEqual(payload["queue"]["backend_event_count"], 1)
            self.assertEqual(payload["queue"]["estimated_backend_events_unread"], 0)
            self.assertEqual(payload["processed"][0]["message"]["text"], "pull task")
            self.assertFalse(payload["send_enabled"])

    def test_pull_hook_messages_cli_reports_missing_source_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            hook_file = Path(tmp) / "missing_hook_events.jsonl"
            backend_file = Path(tmp) / "backend_events.jsonl"
            self._run("--data-dir", str(data_dir), "init")

            payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "pull-hook-messages",
                    "--hook-event-file",
                    str(hook_file),
                    "--backend-event-file",
                    str(backend_file),
                    "--loops",
                    "1",
                    "--interval",
                    "0",
                )
            )

            self.assertEqual(payload["status"], "stopped")
            self.assertEqual(payload["last_status"], "waiting_for_hook_source")
            self.assertEqual(payload["processed_count"], 0)
            self.assertFalse(payload["queue"]["source_exists"])
            self.assertTrue(backend_file.exists())

    def test_pull_weflow_messages_cli_pulls_local_api_into_processing_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            hook_file = Path(tmp) / "hook_events.jsonl"
            backend_file = Path(tmp) / "backend_events.jsonl"
            self._run("--data-dir", str(data_dir), "init")
            with _FakeWeFlowServer() as server:
                payload = json.loads(
                    self._run(
                        "--data-dir",
                        str(data_dir),
                        "pull-weflow-messages",
                        "--base-url",
                        server.base_url,
                        "--token",
                        "test-token",
                        "--hook-event-file",
                        str(hook_file),
                        "--backend-event-file",
                        str(backend_file),
                        "--talker",
                        "wxid_page",
                        "--loops",
                        "1",
                        "--interval",
                        "0",
                        "--workers",
                        "2",
                        "--verbose",
                    )
                )

            self.assertEqual(payload["status"], "stopped")
            self.assertEqual(payload["source_appended_count"], 1)
            self.assertEqual(payload["imported_count"], 1)
            self.assertEqual(payload["processed_count"], 1)
            self.assertEqual(payload["workers"], 2)
            self.assertEqual(payload["last_source"]["scanned_count"], 1)
            self.assertEqual(payload["processed"][0]["message"]["text"], "hello from weflow")
            self.assertTrue(hook_file.exists())
            self.assertTrue(backend_file.exists())

    def _run(self, *args: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-m", "app.personal_wechat_bot.main", *args],
            cwd=ROOT.parent,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )
        return completed.stdout


if __name__ == "__main__":
    unittest.main()

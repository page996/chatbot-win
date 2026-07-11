from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.personal_wechat_bot.wechat_driver.hook_events import hook_event_from_payload
from app.personal_wechat_bot.wechat_driver.hook_source_bridge import (
    HookEventJsonlWriter,
    WEFLOW_LOCAL_BUILD_FLAVOR,
    WeFlowHttpBridge,
    _path_lock,
    _read_bounded_response,
    _weflow_session_pull_admitted,
    append_hook_source_event,
    normalize_weflow_message,
    normalize_weflow_push_event,
    weflow_health_status,
)


class HookSourceBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hook_file = self.root / "hook_events.jsonl"
        self.state_file = self.root / "weflow_state.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_weflow_bridge_rejects_non_local_url_by_default(self) -> None:
        with self.assertRaises(ValueError):
            WeFlowHttpBridge("http://192.0.2.10:5031", hook_event_file=self.hook_file)
        with self.assertRaises(ValueError):
            WeFlowHttpBridge("https://127.0.0.1:5031", hook_event_file=self.hook_file)

        bridge = WeFlowHttpBridge(
            "http://192.0.2.10:5031",
            hook_event_file=self.hook_file,
            allow_non_local=True,
        )
        self.assertEqual(bridge.base_url, "http://192.0.2.10:5031/api/v1")

    def test_weflow_health_and_bridge_reject_cross_authority_redirect_without_leaking_token(self) -> None:
        target_tokens: list[str] = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                target_tokens.append(self.headers.get("Authorization", ""))
                self.send_response(200)
                self.end_headers()

            def log_message(self, _format: str, *_args) -> None:
                return

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target.daemon_threads = True
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()

        class OriginHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/stolen")
                self.end_headers()

            def log_message(self, _format: str, *_args) -> None:
                return

        origin = ThreadingHTTPServer(("127.0.0.1", 0), OriginHandler)
        origin.daemon_threads = True
        origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
        origin_thread.start()
        try:
            base_url = f"http://127.0.0.1:{origin.server_port}"
            status = weflow_health_status(base_url, token="secret")
            self.assertEqual(status["status"], "error")
            self.assertIn("redirect_authority", status["message"])
            non_local_status = weflow_health_status(base_url, token="secret", allow_non_local=True)
            self.assertEqual(non_local_status["status"], "error")
            self.assertIn("redirect_authority", non_local_status["message"])

            bridge = WeFlowHttpBridge(base_url, token="secret", hook_event_file=self.hook_file)
            with self.assertRaisesRegex(ValueError, "redirect_authority"):
                bridge._json("/redirect")
            non_local_bridge = WeFlowHttpBridge(
                base_url,
                token="secret",
                hook_event_file=self.hook_file,
                allow_non_local=True,
            )
            with self.assertRaisesRegex(ValueError, "redirect_authority"):
                non_local_bridge._json("/redirect")
        finally:
            origin.shutdown()
            origin.server_close()
            origin_thread.join(timeout=2)
            target.shutdown()
            target.server_close()
            target_thread.join(timeout=2)

        self.assertEqual(target_tokens, [])

    def test_weflow_bridge_open_ignores_environment_proxy(self) -> None:
        seen_tokens: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen_tokens.append(self.headers.get("Authorization", ""))
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            bridge = WeFlowHttpBridge(
                f"http://127.0.0.1:{server.server_port}",
                token="secret",
                hook_event_file=self.hook_file,
            )
            with mock.patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "http_proxy": "http://127.0.0.1:1",
                    "NO_PROXY": "",
                    "no_proxy": "",
                },
                clear=False,
            ):
                self.assertEqual(bridge._json("/direct"), {})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(seen_tokens, ["Bearer secret"])

    def test_non_local_bridge_mode_keeps_same_authority_redirect_and_ignores_proxy(self) -> None:
        seen: list[tuple[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen.append((self.path, self.headers.get("Authorization", "")))
                if self.path == "/api/v1/start":
                    self.send_response(302)
                    self.send_header("Location", "/api/v1/finish")
                    self.end_headers()
                    return
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            bridge = WeFlowHttpBridge(
                f"http://127.0.0.1:{server.server_port}",
                token="secret",
                hook_event_file=self.hook_file,
                allow_non_local=True,
            )
            with mock.patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "http_proxy": "http://127.0.0.1:1",
                    "NO_PROXY": "",
                    "no_proxy": "",
                },
                clear=False,
            ):
                self.assertEqual(bridge._json("/start"), {})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(
            seen,
            [
                ("/api/v1/start", "Bearer secret"),
                ("/api/v1/finish", "Bearer secret"),
            ],
        )

    def test_weflow_json_reader_enforces_streamed_size_limit(self) -> None:
        class _Response:
            def __init__(self) -> None:
                self.chunks = [b"1234", b"5", b""]

            def read(self, _size: int) -> bytes:
                return self.chunks.pop(0)

        with self.assertRaisesRegex(ValueError, "weflow_response_too_large"):
            _read_bounded_response(_Response(), max_bytes=4, timeout_seconds=1.0)

    def test_weflow_json_reader_enforces_total_deadline(self) -> None:
        class _SlowResponse:
            def read(self, _size: int) -> bytes:
                time.sleep(0.04)
                return b"x"

        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "weflow_response_deadline"):
            _read_bounded_response(_SlowResponse(), max_bytes=100, timeout_seconds=0.2)
        self.assertLess(time.monotonic() - started, 0.4)

    def test_weflow_health_status_validates_local_fork_marker_without_touching_hook_file(self) -> None:
        with _FakeWeFlowHealthServer({"status": "ok", "buildFlavor": WEFLOW_LOCAL_BUILD_FLAVOR}) as server:
            result = weflow_health_status(server.base_url, require_fork=True)

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["fork_ok"])
        self.assertFalse(self.hook_file.exists())

        with _FakeWeFlowHealthServer({"status": "ok", "buildFlavor": "upstream"}) as server:
            bad = weflow_health_status(server.base_url, require_fork=True)

        self.assertEqual(bad["status"], "error")
        self.assertIn("fork marker", bad["message"])

    def test_weflow_health_status_requires_token_for_formal_pull(self) -> None:
        with _FakeWeFlowHealthServer({"status": "ok", "buildFlavor": WEFLOW_LOCAL_BUILD_FLAVOR}) as server:
            result = weflow_health_status(server.base_url, require_token=True, require_fork=True)

        self.assertEqual(result["status"], "error")
        self.assertIn("TOKEN", result["message"])

    def test_weflow_session_pull_blocks_display_only_private_wxid(self) -> None:
        self.assertFalse(
            _weflow_session_pull_admitted(
                {"id": "wxid_stranger", "displayName": "Readable Stranger", "type": "private"}
            )
        )
        self.assertFalse(
            _weflow_session_pull_admitted(
                {"id": "temp-private", "displayName": "微信用户", "type": "private"}
            )
        )
        self.assertFalse(
            _weflow_session_pull_admitted(
                {
                    "id": "temp-private",
                    "displayName": "Readable Stranger",
                    "type": "private",
                    "banner": "对方还不是你的朋友",
                }
            )
        )
        self.assertTrue(
            _weflow_session_pull_admitted(
                {"id": "wxid_friend", "displayName": "Readable Friend", "type": "private", "is_friend": True}
            )
        )
        self.assertTrue(_weflow_session_pull_admitted({"id": "12345@chatroom", "name": "Study Room", "type": "group"}))

    def test_weflow_message_normalizes_attachment_and_quote(self) -> None:
        normalized = normalize_weflow_message(
            {
                "platformMessageId": "wf-msg-1",
                "senderUsername": "wxid_member",
                "accountName": "Member",
                "timestamp": 1719900000000,
                "type": 49,
                "content": "file",
                "mediaLocalPath": "C:\\Inbox\\report.pdf",
                "mediaType": "file",
                "quote": {"platformMessageId": "quoted-1", "accountName": "Other", "content": "old"},
            },
            session_id="12345@chatroom",
            session_meta={"name": "Study Room", "type": "group"},
        )

        event = hook_event_from_payload(normalized)

        self.assertEqual(normalized["raw_id"], "weflow:message:12345@chatroom:wf-msg-1")
        self.assertEqual(normalized["attachments"][0]["name"], "report.pdf")
        self.assertEqual(normalized["quote"]["message_id"], "quoted-1")
        self.assertEqual(event.conversation_key, "12345@chatroom")
        self.assertEqual(event.text, "file")
        self.assertTrue(event.is_group)

    def test_weflow_raw_message_normalizes_voice_file_metadata_and_ordering(self) -> None:
        normalized = normalize_weflow_message(
            {
                "localId": 20,
                "serverId": "0",
                "messageKey": "db:Msg_0:20",
                "localType": 34,
                "createTime": 1719900000,
                "sortSeq": 1719900000001,
                "isSend": 1,
                "senderUsername": "wxid_me",
                "content": "[语音]",
                "mediaType": "voice",
                "mediaFileName": "voice_20.wav",
                "mediaLocalPath": "C:\\Users\\Alice\\Documents\\WeFlow\\api-media\\wxid_page\\voices\\voice_20.wav",
                "quote": {"platformMessageId": "quoted-1", "accountName": "Other", "content": "old", "type": 1},
            },
            session_id="wxid_page",
            session_meta={"name": "PAGE", "type": "private", "media": {"exportPath": "C:\\Users\\Alice\\Documents\\WeFlow\\api-media"}},
            context_only=True,
        )
        event = hook_event_from_payload(normalized)

        self.assertEqual(normalized["source"], "weflow_http_raw")
        self.assertEqual(normalized["raw_id"], "weflow:message:wxid_page:db:Msg_0:20")
        self.assertEqual(normalized["message_key"], "db:Msg_0:20")
        self.assertEqual(normalized["sort_key"], "1719900000001")
        self.assertEqual(normalized["voice"]["audio_name"], "voice_20.wav")
        self.assertEqual(normalized["attachments"][0]["kind"], "audio")
        self.assertTrue(normalized["context_only"])
        self.assertTrue(event.is_self)
        self.assertEqual(event.voice["audio_name"], "voice_20.wav")

    def test_weflow_path_like_message_key_falls_back_to_stable_local_identity(self) -> None:
        first = normalize_weflow_message(
            {
                "localId": 20,
                "serverId": "0",
                "messageKey": "E%3A%5CWeChat%5Cmessage_0.db:Msg_a:20",
                "localType": 1,
                "createTime": 1719900000,
                "sortSeq": 1719900000001,
                "senderUsername": "wxid_page",
                "content": "hello",
            },
            session_id="wxid_page",
            session_meta={"name": "PAGE", "type": "private"},
        )
        second = normalize_weflow_message(
            {
                "localId": 20,
                "serverId": "0",
                "messageKey": "E%3A%5COtherPath%5Cmessage_0.db:Msg_b:20",
                "localType": 1,
                "createTime": 1719900000,
                "sortSeq": 1719900000001,
                "senderUsername": "wxid_page",
                "content": "hello",
            },
            session_id="wxid_page",
            session_meta={"name": "PAGE", "type": "private"},
        )

        self.assertEqual(first["raw_id"], "weflow:message:wxid_page:local:20:1719900000:1719900000001")
        self.assertEqual(first["raw_id"], second["raw_id"])

    def test_weflow_message_recognizes_from_me_self_alias(self) -> None:
        normalized = normalize_weflow_message(
            {
                "platformMessageId": "wf-self-1",
                "senderUsername": "wxid_me",
                "accountName": "Me",
                "content": "agent sent this",
                "fromMe": True,
            },
            session_id="wxid_page",
            session_meta={"name": "PAGE", "type": "private"},
        )

        self.assertTrue(normalized["is_self"])
        self.assertTrue(hook_event_from_payload(normalized).is_self)

    def test_weflow_message_prefers_display_name_over_wxid_title(self) -> None:
        normalized = normalize_weflow_message(
            {
                "platformMessageId": "wf-1",
                "senderUsername": "wxid_page",
                "content": "hello",
            },
            session_id="wxid_page",
            session_meta={"name": "wxid_page", "displayName": "PAGE", "type": "private"},
        )

        self.assertEqual(normalized["talker_name"], "PAGE")

    def test_weflow_raw_pull_uses_messages_endpoint_and_keeps_talker_order_isolated(self) -> None:
        with _FakeWeFlowRawServer() as server:
            bridge = WeFlowHttpBridge(
                server.base_url,
                hook_event_file=self.hook_file,
                state_path=self.state_file,
                timeout_seconds=2,
            )
            result = bridge.pull_once(
                talkers=["wxid_a", "wxid_b"],
                message_limit=1,
                max_pages=2,
                since=0,
                media=True,
            )

        lines = [json.loads(line) for line in self.hook_file.read_text(encoding="utf-8").splitlines()]
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        calls_by_talker: dict[str, list[dict[str, str]]] = {}
        for call in _FakeWeFlowRawServer.calls:
            calls_by_talker.setdefault(call["talker"], []).append(call)

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.scanned_count, 4)
        self.assertEqual(result.appended_count, 4)
        self.assertIn("C:\\WeFlow\\api-media", result.media_export_paths)
        self.assertEqual([(item["talker"], item["text"]) for item in lines], [
            ("wxid_a", "a-older"),
            ("wxid_a", "a-newer"),
            ("wxid_b", "b-older"),
            ("wxid_b", "[文件] report.pdf"),
        ])
        self.assertTrue(all(item["context_only"] for item in lines))
        self.assertEqual([call["offset"] for call in calls_by_talker["wxid_a"]], ["0", "1"])
        self.assertEqual([call["offset"] for call in calls_by_talker["wxid_b"]], ["0", "1"])
        self.assertEqual(_FakeWeFlowRawServer.calls[0]["media"], "1")
        self.assertEqual(_FakeWeFlowRawServer.calls[0]["file"], "1")
        self.assertIn("wxid_a", state["sessions"])
        self.assertIn("wxid_b", state["sessions"])

    def test_weflow_raw_pull_workers_keep_same_talker_serial_and_deduped(self) -> None:
        with _FakeWeFlowRawServer() as server:
            bridge = WeFlowHttpBridge(
                server.base_url,
                hook_event_file=self.hook_file,
                state_path=self.state_file,
                timeout_seconds=2,
            )
            result = bridge.pull_once(
                talkers=["wxid_a", "wxid_a", "wxid_b"],
                message_limit=10,
                max_pages=1,
                since=0,
                media=True,
                workers=3,
            )

        lines = [json.loads(line) for line in self.hook_file.read_text(encoding="utf-8").splitlines()]
        texts_by_talker: dict[str, list[str]] = {}
        for item in lines:
            texts_by_talker.setdefault(item["talker"], []).append(item["text"])

        self.assertEqual(result.status, "ok", result.errors)
        self.assertEqual(result.scanned_count, 4)
        self.assertEqual(result.appended_count, 4)
        self.assertEqual(texts_by_talker["wxid_a"], ["a-older", "a-newer"])
        self.assertEqual(texts_by_talker["wxid_b"], ["b-older", "[文件] report.pdf"])
        self.assertEqual(len({item["raw_id"] for item in lines}), 4)
        self.assertEqual(len(lines), 4)
        self.assertCountEqual(
            [(call["talker"], call["offset"]) for call in _FakeWeFlowRawServer.calls],
            [("wxid_a", "0"), ("wxid_b", "0")],
        )

    def test_repeated_complete_pull_keeps_seen_message_out_of_synthetic_recalls(self) -> None:
        now = int(time.time())
        bridge = WeFlowHttpBridge(
            hook_event_file=self.hook_file,
            state_path=self.state_file,
            timeout_seconds=2,
        )
        bridge.list_sessions = lambda limit=100: [
            {
                "username": "wxid_a",
                "displayName": "A",
                "type": "private",
                "isFriend": True,
                "lastMessageTime": now,
            }
        ]
        bridge.raw_message_pages = lambda _talker, **_kwargs: [
            {
                "messages": [
                    {
                        "platformMessageId": "message-1",
                        "senderUsername": "wxid_a",
                        "content": "still present",
                        "timestamp": now,
                    }
                ],
                "hasMore": False,
                "count": 1,
            }
        ]

        first = bridge.pull_once(talkers=["wxid_a"], lookback_seconds=300)
        second = bridge.pull_once(talkers=["wxid_a"], lookback_seconds=300)

        rows = [json.loads(line) for line in self.hook_file.read_text(encoding="utf-8").splitlines()]
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        raw_id = "weflow:message:wxid_a:message-1"
        self.assertEqual(first.appended_count, 1)
        self.assertEqual(second.appended_count, 0)
        self.assertEqual([(row["event_type"], row["raw_id"]) for row in rows], [("message", raw_id)])
        self.assertEqual(state["sessions"]["wxid_a"]["recent_raw_ids"], {raw_id: now})

    def test_weflow_state_lock_retries_transient_windows_permission_error(self) -> None:
        lock_path = self.root / "weflow_state.json.lock"
        real_open = os.open
        attempts = 0

        def transient_permission_error(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError(13, "lock is temporarily unavailable")
            return real_open(*args, **kwargs)

        with mock.patch(
            "app.personal_wechat_bot.runtime.process_lock.os.open",
            side_effect=transient_permission_error,
        ):
            with _path_lock(lock_path, timeout_seconds=0.2):
                self.assertTrue(lock_path.exists())

        self.assertGreaterEqual(attempts, 2)
        self.assertFalse(lock_path.exists())

    def test_weflow_recall_targets_previous_message_raw_id(self) -> None:
        normalized = normalize_weflow_push_event(
            {
                "event": "message.revoke",
                "sessionId": "12345@chatroom",
                "sessionType": "group",
                "rawid": "revoke-event-1",
                "sourceName": "Member",
                "groupName": "Study Room",
                "content": "<sysmsg><revokemsg><newmsgid>wf-msg-1</newmsgid></revokemsg></sysmsg>",
                "timestamp": 1719900001,
            }
        )
        event = hook_event_from_payload(normalized)

        self.assertEqual(normalized["event_type"], "recall")
        self.assertEqual(normalized["recall"]["target_message_id"], "wf-msg-1")
        self.assertEqual(normalized["recall"]["target_raw_id"], "weflow:message:12345@chatroom:wf-msg-1")
        self.assertEqual(event.recall["target_raw_id"], "weflow:message:12345@chatroom:wf-msg-1")

    def test_weflow_message_normalizes_group_media_message(self) -> None:
        normalized = normalize_weflow_message(
            {
                "platformMessageId": "wf-media-1",
                "localType": 3,
                "senderUsername": "wxid_member",
                "content": "image",
                "mediaLocalPath": "C:\\WeChat\\thumb.jpg",
                "mediaType": "image",
            },
            session_id="12345@chatroom",
            session_meta={"name": "Study Room", "type": "group"},
        )
        event = hook_event_from_payload(normalized)

        self.assertEqual(normalized["source"], "weflow_http_raw")
        self.assertEqual(normalized["attachments"][0]["kind"], "image")
        self.assertEqual(event.conversation_key, "12345@chatroom")
        self.assertEqual(event.sender_wechat_id, "wxid_member")

    def test_weflow_message_marks_self_from_owner_data_path(self) -> None:
        normalized = normalize_weflow_message(
            {
                "rawid": "wf-self-1",
                "sessionName": "Friend",
                "sender": "wxid_owner123",
                "content": "manual owner message",
                "timestamp": 1719900001,
                "dbPath": r"E:\WeChat-doc\xwechat_files\wxid_owner123_abcd\db_storage\message\message_0.db",
            },
            session_id="wxid_friend",
        )

        self.assertTrue(normalized["is_self"])

    def test_append_hook_source_event_writes_normalized_jsonl(self) -> None:
        result = append_hook_source_event(
            self.hook_file,
            {
                "event": "message.new",
                "sessionId": "wxid_page",
                "rawid": "wf-1",
                "sourceName": "PAGE",
                "content": "hello",
            },
            source="weflow-push",
        )
        raw = json.loads(self.hook_file.read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual(result.status, "ok")
        self.assertEqual(raw["raw_id"], "weflow:message:wxid_page:wf-1")
        self.assertEqual(hook_event_from_payload(raw).text, "hello")

    def test_hook_writer_appends_valid_jsonl(self) -> None:
        writer = HookEventJsonlWriter(self.hook_file)

        writer.append({"talker": "wxid_page", "sender_name": "PAGE", "text": "one"})
        writer.append({"talker": "wxid_page", "sender_name": "PAGE", "text": "two"})
        lines = self.hook_file.read_text(encoding="utf-8").splitlines()

        self.assertEqual([json.loads(line)["text"] for line in lines], ["one", "two"])

    def test_hook_writer_dedupes_stable_raw_id(self) -> None:
        writer = HookEventJsonlWriter(self.hook_file)

        first = writer.append({"raw_id": "same-raw", "talker": "wxid_page", "sender_name": "PAGE", "text": "one"})
        second = writer.append({"raw_id": "same-raw", "talker": "wxid_page", "sender_name": "PAGE", "text": "two"})
        lines = self.hook_file.read_text(encoding="utf-8").splitlines()

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["text"], "one")

    def test_weflow_sse_skips_ready_saves_last_event_id_and_dedupes(self) -> None:
        with _FakeWeFlowSseServer() as server:
            bridge = WeFlowHttpBridge(
                server.base_url,
                hook_event_file=self.hook_file,
                state_path=self.state_file,
                timeout_seconds=2,
            )
            first = bridge.listen_sse(max_events=1)
            second = bridge.listen_sse(max_seconds=0.2)

        lines = self.hook_file.read_text(encoding="utf-8").splitlines()
        state = json.loads(self.state_file.read_text(encoding="utf-8"))

        self.assertEqual(first.status, "ok")
        self.assertEqual(first.appended_count, 1)
        self.assertEqual(first.skipped_count, 1)
        self.assertEqual(first.last_event_id, "10")
        self.assertEqual(second.appended_count, 0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["event_id"], "10")
        self.assertEqual(state["weflow_sse_last_event_id"], "10")
        self.assertIn("message.new|wxid_page|wf-sse-1||10", state["weflow_sse_seen"])
        self.assertTrue(_FakeWeFlowSseServer.last_event_ids[-1] in {"10", ""})


class _FakeWeFlowHealthServer:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def __enter__(self):
        payload = self.payload

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                return

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                if parsed.path not in {"/health", "/api/v1/health"}:
                    self.send_response(404)
                    self.end_headers()
                    return
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
        self.thread.join(timeout=5)


class _FakeWeFlowSseServer:
    last_event_ids: list[str] = []

    def __enter__(self):
        _FakeWeFlowSseServer.last_event_ids = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                return

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                if parsed.path != "/api/v1/push/messages":
                    self.send_response(404)
                    self.end_headers()
                    return
                query = parse_qs(parsed.query)
                last_id = self.headers.get("Last-Event-ID", "") or (query.get("lastEventId") or [""])[0]
                _FakeWeFlowSseServer.last_event_ids.append(last_id)
                body = (
                    "event: ready\n"
                    "data: {\"success\": true, \"stream\": \"local\"}\n\n"
                    "id: 10\n"
                    "event: message.new\n"
                    "data: {\"event\":\"message.new\",\"sessionId\":\"wxid_page\",\"rawid\":\"wf-sse-1\","
                    "\"sourceName\":\"PAGE\",\"content\":\"from sse\",\"timestamp\":1719900000}\n\n"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
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
        self.thread.join(timeout=5)


class _FakeWeFlowRawServer:
    calls: list[dict[str, str]] = []

    def __enter__(self):
        _FakeWeFlowRawServer.calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                return

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                if parsed.path != "/api/v1/messages":
                    self.send_response(404)
                    self.end_headers()
                    return
                query = parse_qs(parsed.query)
                talker = (query.get("talker") or [""])[0]
                offset = int((query.get("offset") or ["0"])[0])
                limit = int((query.get("limit") or ["100"])[0])
                _FakeWeFlowRawServer.calls.append(
                    {
                        "talker": talker,
                        "offset": str(offset),
                        "limit": str(limit),
                        "media": (query.get("media") or [""])[0],
                        "file": (query.get("file") or [""])[0],
                    }
                )
                messages = {
                    "wxid_a": [
                        {"localId": 2, "serverId": "a2", "localType": 1, "createTime": 20, "sortSeq": 20, "senderUsername": "wxid_a", "content": "a-newer"},
                        {"localId": 1, "serverId": "a1", "localType": 1, "createTime": 10, "sortSeq": 10, "senderUsername": "wxid_a", "content": "a-older"},
                    ],
                    "wxid_b": [
                        {
                            "localId": 11,
                            "serverId": "b2",
                            "localType": 49,
                            "createTime": 40,
                            "sortSeq": 40,
                            "senderUsername": "wxid_b",
                            "content": "[文件] report.pdf",
                            "fileName": "report.pdf",
                            "mediaType": "file",
                            "mediaFileName": "11_report.pdf",
                            "mediaLocalPath": "C:\\WeFlow\\api-media\\wxid_b\\file\\pdf\\11_report.pdf",
                        },
                        {"localId": 10, "serverId": "b1", "localType": 1, "createTime": 30, "sortSeq": 30, "senderUsername": "wxid_b", "content": "b-older"},
                    ],
                }.get(talker, [])
                page = messages[offset : offset + limit]
                body = json.dumps(
                    {
                        "success": True,
                        "talker": talker,
                        "count": len(page),
                        "hasMore": offset + limit < len(messages),
                        "media": {"enabled": True, "exportPath": "C:\\WeFlow\\api-media", "count": 1},
                        "messages": page,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
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
        self.thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

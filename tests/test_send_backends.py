from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.wechat_driver import send_backends
from app.personal_wechat_bot.wechat_driver.send_backends import (
    WeChatNativeHttpSendBackend,
    WeFlowHttpSendBackend,
    build_send_backend,
    wechat_native_http_status,
    weflow_http_status,
)


SAFE_WECHAT_RECEIVER = "wxid_backend12345"


@contextmanager
def _json_server(routes: dict[tuple[str, str], tuple[int, dict] | list[tuple[int, dict]]]):
    requests: list[dict] = []
    route_counts: dict[tuple[str, str], int] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def log_message(self, _format: str, *_args) -> None:
            return

        def _handle(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(length).decode("utf-8") if length else ""
            body = json.loads(raw_body or "{}") if raw_body else {}
            requests.append(
                {
                    "method": method,
                    "path": path,
                    "headers": dict(self.headers),
                    "body": body,
                }
            )
            key = (method, path)
            route = routes.get(key, (404, {"status": "not_found"}))
            if isinstance(route, list):
                index = route_counts.get(key, 0)
                route_counts[key] = index + 1
                status, payload = route[min(index, len(route) - 1)]
            else:
                status, payload = route
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class SendBackendsTest(unittest.TestCase):
    def test_http_json_allows_only_same_authority_redirects_with_token(self) -> None:
        target_headers: list[str] = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                target_headers.append(self.headers.get("Authorization", ""))
                self.send_response(200)
                self.end_headers()

            def log_message(self, _format: str, *_args) -> None:
                return

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target.daemon_threads = True
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()
        finish_headers: list[str] = []

        class OriginHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/same":
                    self.send_response(302)
                    self.send_header("Location", "/finish")
                    self.end_headers()
                    return
                if self.path == "/cross":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{target.server_port}/stolen")
                    self.end_headers()
                    return
                finish_headers.append(self.headers.get("Authorization", ""))
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args) -> None:
                return

        origin = ThreadingHTTPServer(("127.0.0.1", 0), OriginHandler)
        origin.daemon_threads = True
        origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
        origin_thread.start()
        try:
            base = f"http://127.0.0.1:{origin.server_port}"
            self.assertEqual(
                send_backends._http_json(
                    base + "/same",
                    method="GET",
                    token="secret",
                    timeout_seconds=1.0,
                ),
                {},
            )
            with self.assertRaisesRegex(ValueError, "redirect_authority"):
                send_backends._http_json(
                    base + "/cross",
                    method="GET",
                    token="secret",
                    timeout_seconds=1.0,
                )
        finally:
            origin.shutdown()
            origin.server_close()
            origin_thread.join(timeout=2)
            target.shutdown()
            target.server_close()
            target_thread.join(timeout=2)

        self.assertEqual(finish_headers, ["Bearer secret"])
        self.assertEqual(target_headers, [])

    def test_http_json_stops_real_slow_stream_at_total_deadline(self) -> None:
        disconnected = threading.Event()

        class SlowStreamHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.05)
                except ConnectionError:
                    disconnected.set()

            def log_message(self, _format: str, *_args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowStreamHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(TimeoutError, "http_response_deadline_exceeded"):
                send_backends._http_json(
                    f"http://127.0.0.1:{server.server_port}/slow",
                    method="GET",
                    timeout_seconds=0.25,
                )
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertTrue(disconnected.wait(timeout=1.0))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_json_enforces_total_body_deadline(self) -> None:
        clock = [100.0]

        class SlowResponse:
            headers: dict[str, str] = {}

            def __init__(self) -> None:
                self.read_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def read1(self, _size: int) -> bytes:
                self.read_calls += 1
                clock[0] += 0.11
                return b" "

        response = SlowResponse()
        with (
            mock.patch.object(send_backends.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(send_backends, "guarded_local_urlopen", return_value=response) as mocked_open,
        ):
            with self.assertRaisesRegex(TimeoutError, "http_response_deadline_exceeded"):
                send_backends._http_json(
                    "http://127.0.0.1:12345/test",
                    method="GET",
                    timeout_seconds=0.2,
                )

        self.assertEqual(response.read_calls, 2)
        self.assertEqual(mocked_open.call_args.kwargs["timeout_seconds"], 0.2)

    def test_http_json_rejects_streamed_body_over_limit(self) -> None:
        limit = send_backends._MAX_HTTP_JSON_RESPONSE_BYTES

        class OversizedResponse:
            headers: dict[str, str] = {}

            def __init__(self) -> None:
                self.remaining = limit + 1
                self.max_read_size = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def read1(self, size: int) -> bytes:
                self.max_read_size = max(self.max_read_size, size)
                count = min(size, self.remaining)
                self.remaining -= count
                return b" " * count

        response = OversizedResponse()
        with mock.patch.object(send_backends, "guarded_local_urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "http_response_too_large"):
                send_backends._http_json(
                    "http://127.0.0.1:12345/test",
                    method="GET",
                    timeout_seconds=1.0,
                )

        self.assertLessEqual(response.max_read_size, send_backends._HTTP_RESPONSE_READ_CHUNK_BYTES)
        self.assertEqual(response.remaining, 0)

    def test_http_json_rejects_declared_error_body_over_limit_without_reading(self) -> None:
        class OversizedErrorResponse:
            headers = {"Content-Length": str(send_backends._MAX_HTTP_JSON_RESPONSE_BYTES + 1)}

            def read1(self, _size: int) -> bytes:
                raise AssertionError("oversized declared body must not be read")

            def close(self) -> None:
                return None

        error = send_backends.HTTPError(
            "http://127.0.0.1:12345/test",
            500,
            "error",
            OversizedErrorResponse.headers,
            OversizedErrorResponse(),
        )
        with mock.patch.object(send_backends, "guarded_local_urlopen", side_effect=error):
            with self.assertRaisesRegex(ValueError, "http_response_too_large"):
                send_backends._http_json(
                    "http://127.0.0.1:12345/test",
                    method="GET",
                    timeout_seconds=1.0,
                )

    def test_weflow_http_status_uses_local_health_and_token(self) -> None:
        with _json_server(
            {
                ("GET", "/api/v1/health"): (
                    200,
                    {
                        "status": "ok",
                        "capabilities": {"sendText": False, "sendFile": False, "sendBackend": "native-not-implemented"},
                    },
                )
            }
        ) as (base_url, requests):
            with mock.patch.dict(os.environ, {"WEFLOW_TEST_TOKEN": "secret"}, clear=False):
                status = weflow_http_status(base_url, token_env="WEFLOW_TEST_TOKEN")

        self.assertTrue(status["available"])
        self.assertTrue(status["token_present"])
        self.assertFalse(status["send_capabilities"]["text"]["supports"])
        self.assertFalse(status["send_capabilities"]["file"]["supports"])
        self.assertEqual(status["send_capabilities"]["backend"], "native-not-implemented")
        self.assertEqual(requests[0]["headers"].get("Authorization"), "Bearer secret")

    def test_weflow_backend_fails_without_token(self) -> None:
        backend = WeFlowHttpSendBackend(token_env="WEFLOW_TEST_TOKEN")

        with mock.patch.dict(os.environ, {"WEFLOW_API_TOKEN": "", "WEFLOW_TEST_TOKEN": ""}, clear=False):
            result = backend.send_text(SAFE_WECHAT_RECEIVER, "hello")

        self.assertFalse(result.ok)
        self.assertIn("weflow_token_missing", result.reason)

    def test_weflow_backend_posts_text_and_maps_success(self) -> None:
        routes = {("POST", "/api/v1/send/text"): (200, {"ok": True, "messageId": "msg-1"})}
        with _json_server(routes) as (base_url, requests):
            backend = WeFlowHttpSendBackend(base_url=base_url, token="secret")

            result = backend.send_text(SAFE_WECHAT_RECEIVER, "hello")

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "weflow_http_send_text")
        self.assertEqual(result.external_message_id, "msg-1")
        self.assertEqual(requests[0]["headers"].get("Authorization"), "Bearer secret")
        self.assertEqual(requests[0]["body"]["receiver"], SAFE_WECHAT_RECEIVER)
        self.assertEqual(requests[0]["body"]["text"], "hello")

    def test_weflow_backend_rejects_business_failure_in_http_200_response(self) -> None:
        payloads = (
            {"code": 500, "message": "send failed"},
            {"ok": True, "status": "failed", "message": "not delivered"},
            {"success": True, "status": "processing"},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                routes = {("POST", "/api/v1/send/text"): (200, payload)}
                with _json_server(routes) as (base_url, _requests):
                    backend = WeFlowHttpSendBackend(base_url=base_url, token="secret")
                    result = backend.send_text(SAFE_WECHAT_RECEIVER, "hello")

                self.assertFalse(result.ok)
                self.assertIn("weflow_http_send_text_failed", result.reason)

    def test_weflow_backend_blocks_synthetic_private_receiver_before_http(self) -> None:
        routes = {("POST", "/api/v1/send/text"): (200, {"ok": True})}
        with _json_server(routes) as (base_url, requests):
            backend = WeFlowHttpSendBackend(base_url=base_url, token="secret")

            result = backend.send_text("wxid_a", "deliver me")

        self.assertFalse(result.ok)
        self.assertIn("blocked_synthetic_private_receiver", result.reason)
        self.assertEqual(requests, [])

    def test_weflow_backend_maps_http_error_to_failed_outcome(self) -> None:
        with _json_server({}) as (base_url, _requests):
            backend = WeFlowHttpSendBackend(base_url=base_url, token="secret")

            result = backend.send_text(SAFE_WECHAT_RECEIVER, "hello")

        self.assertFalse(result.ok)
        self.assertIn("http_404", result.reason)

    def test_build_send_backend_passes_configured_weflow_http(self) -> None:
        backend = build_send_backend(
            BotConfig(
                send_backend="weflow_http",
                weflow_base_url="http://127.0.0.1:5031",
                weflow_token_env="WEFLOW_TEST_TOKEN",
                weflow_send_text_path="/send/custom-text",
                weflow_send_file_path="/send/custom-file",
                weflow_send_timeout_seconds=4.5,
            )
        )

        self.assertIsInstance(backend, WeFlowHttpSendBackend)
        self.assertEqual(backend.text_path, "/send/custom-text")
        self.assertEqual(backend.file_path, "/send/custom-file")
        self.assertEqual(backend.timeout_seconds, 4.5)

    def test_wechat_native_status_requires_login(self) -> None:
        with _json_server({("GET", "/QueryDB/status"): (200, {"IsLogin": 1, "hWeixin": 123})}) as (base_url, requests):
            status = wechat_native_http_status(base_url)

        self.assertTrue(status["available"])
        self.assertEqual(status["status"], "available")
        self.assertEqual(
            status["send_capabilities"]["image"]["status"],
            "default_route_unsupported_in_text_hook_build",
        )
        self.assertEqual(
            status["send_capabilities"]["file"]["status"],
            "default_route_accepts_unverified_native_file",
        )
        self.assertEqual(requests[0]["method"], "GET")
        self.assertEqual(requests[0]["path"], "/QueryDB/status")

    def test_wechat_native_status_reports_not_login(self) -> None:
        with _json_server({("GET", "/QueryDB/status"): (200, {"IsLogin": 0})}) as (base_url, _requests):
            status = wechat_native_http_status(base_url)

        self.assertFalse(status["available"])
        self.assertEqual(status["status"], "not_login")
        self.assertEqual(status["reason"], "wechat_native_not_login")

    def test_wechat_native_backend_posts_text_and_maps_success_to_unverified_accept(self) -> None:
        routes = {("POST", "/SendTextMsg"): (200, {"ret": 0, "retmsg": "success", "msgId": "msg-1"})}
        with _json_server(routes) as (base_url, requests):
            backend = WeChatNativeHttpSendBackend(base_url=base_url, verify_timeout_seconds=0)

            result = backend.send_text(SAFE_WECHAT_RECEIVER, "hello")

        self.assertTrue(result.ok)
        self.assertFalse(result.delivery_verified)
        self.assertEqual(result.reason, "wechat_native_http_send_text_accepted_unverified")
        self.assertEqual(result.external_message_id, "msg-1")
        self.assertEqual(result.payload["backend"], "wechat_native_http")
        self.assertEqual(result.payload["endpoint_path"], "/SendTextMsg")
        self.assertFalse(result.payload["delivery_verified"])
        self.assertTrue(result.payload["accepted_unverified"])
        self.assertEqual(result.payload["response"]["ret"], 0)
        self.assertEqual(requests[0]["body"], {"wxidorgid": SAFE_WECHAT_RECEIVER, "msg": "hello"})

    def test_wechat_native_backend_blocks_synthetic_private_receiver_before_http(self) -> None:
        routes = {("POST", "/SendTextMsg"): (200, {"ret": 0, "retmsg": "success"})}
        with _json_server(routes) as (base_url, requests):
            backend = WeChatNativeHttpSendBackend(base_url=base_url, verify_timeout_seconds=0)

            result = backend.send_text("wxid_a", "deliver me")

        self.assertFalse(result.ok)
        self.assertIn("blocked_synthetic_private_receiver", result.reason)
        self.assertEqual(requests, [])

    def test_wechat_native_backend_accepts_explicit_delivery_verified_success(self) -> None:
        routes = {
            ("POST", "/SendTextMsg"): (
                200,
                {"ret": 0, "retmsg": "success", "msgId": "msg-1", "delivery_verified": True},
            )
        }
        with _json_server(routes) as (base_url, _requests):
            backend = WeChatNativeHttpSendBackend(base_url=base_url, verify_timeout_seconds=0)

            result = backend.send_text(SAFE_WECHAT_RECEIVER, "hello")

        self.assertTrue(result.ok)
        self.assertTrue(result.delivery_verified)
        self.assertEqual(result.reason, "wechat_native_http_send_text")
        self.assertEqual(result.external_message_id, "msg-1")

    def test_wechat_native_backend_verifies_text_delivery_with_weflow_readback(self) -> None:
        now = int(time.time())
        routes = {
            ("GET", "/api/v1/messages"): [
                (200, {"success": True, "messages": []}),
                (
                    200,
                    {
                        "success": True,
                        "messages": [
                            {
                                "localId": 7,
                                "serverId": "srv-7",
                                "createTime": now,
                                "sortSeq": now * 1000,
                                "isSend": 1,
                                "content": "hello verified",
                                "rawContent": "hello verified",
                                "messageKey": "message-key-7",
                            }
                        ],
                    },
                ),
            ],
            ("POST", "/SendTextMsg"): (200, {"ret": 0, "retmsg": "accepted_unverified"}),
        }
        with _json_server(routes) as (base_url, requests):
            backend = WeChatNativeHttpSendBackend(
                base_url=base_url,
                verify_base_url=base_url,
                verify_token_env="WEFLOW_TEST_TOKEN",
                verify_timeout_seconds=1.0,
            )
            with mock.patch.dict(os.environ, {"WEFLOW_TEST_TOKEN": "secret"}, clear=False):
                result = backend.send_text(SAFE_WECHAT_RECEIVER, "hello verified")

        self.assertTrue(result.ok)
        self.assertTrue(result.delivery_verified)
        self.assertEqual(result.reason, "wechat_native_http_send_text_verified")
        self.assertEqual(result.external_message_id, "srv-7")
        self.assertTrue(result.payload["delivery_verified"])
        self.assertTrue(result.payload["delivery_verification"]["verified"])
        self.assertEqual([item["method"] for item in requests], ["GET", "POST", "GET"])
        self.assertEqual(requests[0]["headers"].get("Authorization"), "Bearer secret")
        self.assertEqual(requests[1]["body"], {"wxidorgid": SAFE_WECHAT_RECEIVER, "msg": "hello verified"})

    def test_wechat_native_text_verification_uses_text_timeout_only(self) -> None:
        routes = {("POST", "/SendTextMsg"): (200, {"ret": 0, "retmsg": "accepted_unverified"})}
        with _json_server(routes) as (base_url, requests):
            backend = WeChatNativeHttpSendBackend(
                base_url=base_url,
                verify_base_url=base_url,
                verify_token_env="WEFLOW_TEST_TOKEN",
                verify_timeout_seconds=0,
                file_verify_timeout_seconds=1.0,
            )
            with mock.patch.dict(os.environ, {"WEFLOW_TEST_TOKEN": "secret"}, clear=False):
                result = backend.send_text(SAFE_WECHAT_RECEIVER, "verification disabled")

        self.assertTrue(result.ok)
        self.assertFalse(result.delivery_verified)
        self.assertEqual([item["method"] for item in requests], ["POST"])

    def test_wechat_native_text_baseline_failure_cannot_verify_recent_duplicate(self) -> None:
        now = int(time.time())
        routes = {
            ("GET", "/api/v1/messages"): [
                (503, {"error": "baseline unavailable"}),
                (
                    200,
                    {
                        "success": True,
                        "messages": [
                            {
                                "localId": 7,
                                "serverId": "stale-text",
                                "createTime": now - 2,
                                "sortSeq": (now - 2) * 1000,
                                "isSend": 1,
                                "content": "same text",
                                "messageKey": "stale-text-key",
                            }
                        ],
                    },
                ),
            ],
            ("POST", "/SendTextMsg"): (200, {"ret": 0, "retmsg": "accepted_unverified"}),
        }
        with _json_server(routes) as (base_url, requests):
            backend = WeChatNativeHttpSendBackend(
                base_url=base_url,
                verify_base_url=base_url,
                verify_token_env="WEFLOW_TEST_TOKEN",
                verify_timeout_seconds=0.2,
            )
            with mock.patch.dict(os.environ, {"WEFLOW_TEST_TOKEN": "secret"}, clear=False):
                result = backend.send_text(SAFE_WECHAT_RECEIVER, "same text")

        self.assertTrue(result.ok)
        self.assertFalse(result.delivery_verified)
        self.assertEqual(result.reason, "wechat_native_http_send_text_accepted_unverified")
        self.assertEqual(result.payload["delivery_verification"]["reason"], "baseline_readback_failed")
        self.assertEqual([item["method"] for item in requests], ["GET", "POST"])

    def test_wechat_native_backend_blocks_default_image_but_accepts_default_file_route(self) -> None:
        routes = {("POST", "/send_file_msg"): (200, {"ret": 0, "retmsg": "accepted_unverified_file_native"})}
        with _json_server(routes) as (base_url, requests):
            backend = WeChatNativeHttpSendBackend(
                base_url=base_url,
                verify_timeout_seconds=0,
                file_verify_timeout_seconds=0,
            )

            image = backend.send_file(SAFE_WECHAT_RECEIVER, "C:\\tmp\\a.png")
            document = backend.send_file(SAFE_WECHAT_RECEIVER, "C:\\tmp\\a.pptx")

        self.assertFalse(image.ok)
        self.assertIn("unsupported_on_411053_text_only", image.reason)
        self.assertTrue(document.ok)
        self.assertFalse(document.delivery_verified)
        self.assertEqual(document.reason, "wechat_native_http_send_file_accepted_unverified")
        self.assertEqual(requests[0]["path"], "/send_file_msg")
        self.assertEqual(requests[0]["body"], {"wxid": SAFE_WECHAT_RECEIVER, "filepath": "C:\\tmp\\a.pptx", "stage": "send"})

    def test_wechat_native_backend_normalizes_existing_relative_file_path(self) -> None:
        routes = {("POST", "/send_file_msg"): (200, {"ret": 0, "retmsg": "accepted_unverified_file_native"})}
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp, _json_server(routes) as (base_url, requests):
            target = Path(tmp) / "backend-relative-file.txt"
            target.write_text("file", encoding="utf-8")
            relative_target = os.path.relpath(target, Path.cwd())
            backend = WeChatNativeHttpSendBackend(
                base_url=base_url,
                verify_timeout_seconds=0,
                file_verify_timeout_seconds=0,
            )

            result = backend.send_file(SAFE_WECHAT_RECEIVER, relative_target)

        self.assertTrue(result.ok)
        self.assertEqual(requests[0]["body"]["filepath"], str(target.resolve()))

    def test_wechat_native_backend_verifies_file_delivery_with_weflow_readback(self) -> None:
        routes = {
            ("GET", "/api/v1/messages"): [
                (200, {"success": True, "messages": []}),
                (
                    200,
                    {
                        "success": True,
                        "messages": [
                            {
                                "localId": 9,
                                "serverId": "srv-file-9",
                                "createTime": int(time.time()),
                                "sortSeq": int(time.time()) * 1000,
                                "isSend": 1,
                                "content": (
                                    "<msg><appmsg><title>report.txt</title><type>6</type>"
                                    "<appattach><totallen>4</totallen></appattach></appmsg></msg>"
                                ),
                                "rawContent": (
                                    "<msg><appmsg><title>report.txt</title><type>6</type>"
                                    "<appattach><totallen>4</totallen></appattach></appmsg></msg>"
                                ),
                                "messageKey": "message-key-file-9",
                            }
                        ],
                    },
                ),
            ],
            ("POST", "/send_file_msg"): (200, {"ret": 0, "retmsg": "accepted_unverified_file_native"}),
        }
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp, _json_server(routes) as (base_url, requests):
            target = Path(tmp) / "report.txt"
            target.write_text("file", encoding="utf-8")
            backend = WeChatNativeHttpSendBackend(
                base_url=base_url,
                verify_base_url=base_url,
                verify_token_env="WEFLOW_TEST_TOKEN",
                verify_timeout_seconds=0,
                file_verify_timeout_seconds=1.0,
            )
            with mock.patch.dict(os.environ, {"WEFLOW_TEST_TOKEN": "secret"}, clear=False):
                result = backend.send_file(SAFE_WECHAT_RECEIVER, str(target))

        self.assertTrue(result.ok)
        self.assertTrue(result.delivery_verified)
        self.assertEqual(result.reason, "wechat_native_http_send_file_verified")
        self.assertEqual(result.external_message_id, "srv-file-9")
        self.assertTrue(result.payload["delivery_verified"])
        self.assertEqual(result.payload["delivery_verification"]["reason"], "matched_weflow_outgoing_file")
        self.assertEqual([item["method"] for item in requests], ["GET", "POST", "GET"])
        self.assertEqual(requests[1]["body"]["filepath"], str(target.resolve()))

    def test_wechat_native_file_baseline_failure_blocks_late_reverification(self) -> None:
        routes = {
            ("GET", "/api/v1/messages"): (503, {"error": "baseline unavailable"}),
            ("POST", "/send_file_msg"): (200, {"ret": 0, "retmsg": "accepted_unverified_file_native"}),
        }
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp, _json_server(routes) as (base_url, requests):
            target = Path(tmp) / "same.csv"
            target.write_text("file", encoding="utf-8")
            backend = WeChatNativeHttpSendBackend(
                base_url=base_url,
                verify_base_url=base_url,
                verify_token_env="WEFLOW_TEST_TOKEN",
                verify_timeout_seconds=0,
                file_verify_timeout_seconds=0.2,
            )
            with mock.patch.dict(os.environ, {"WEFLOW_TEST_TOKEN": "secret"}, clear=False):
                result = backend.send_file(SAFE_WECHAT_RECEIVER, str(target))
                late = backend.verify_accepted_bridge_record(
                    {
                        "bridge_id": "bridge:file:baseline-failed",
                        "receiver": SAFE_WECHAT_RECEIVER,
                        "kind": "file",
                        "path": str(target),
                        "name": target.name,
                    },
                    {
                        "bridge_id": "bridge:file:baseline-failed",
                        "status": "accepted",
                        "reason": result.reason,
                        "payload": result.payload,
                    },
                )

        self.assertTrue(result.ok)
        self.assertFalse(result.delivery_verified)
        self.assertEqual(result.payload["delivery_verification"]["baseline_status"], "failed")
        self.assertIsNone(late)
        self.assertEqual([item["method"] for item in requests], ["GET", "POST"])

    def test_wechat_native_backend_late_verifies_accepted_file_without_resend(self) -> None:
        now = int(time.time())
        routes = {
            ("GET", "/api/v1/messages"): (
                200,
                {
                    "success": True,
                    "messages": [
                        {
                            "localId": 32,
                            "serverId": "srv-file-late",
                            "createTime": now,
                            "sortSeq": now * 1000,
                            "isSend": 1,
                            "content": (
                                "<msg><appmsg><title>late.csv</title><type>6</type>"
                                "<appattach><totallen>9</totallen></appattach></appmsg></msg>"
                            ),
                            "rawContent": (
                                "<msg><appmsg><title>late.csv</title><type>6</type>"
                                "<appattach><totallen>9</totallen></appattach></appmsg></msg>"
                            ),
                            "messageKey": "message-key-file-late",
                        }
                    ],
                },
            )
        }
        record = {
            "bridge_id": "bridge:wxid_a:file",
            "conversation_id": "wxid_a",
            "receiver": "wxid_a",
            "kind": "file",
            "path": "C:\\tmp\\late.csv",
            "name": "late.csv",
            "created_at": "2026-07-09T13:03:33+00:00",
        }
        ack = {
            "bridge_id": "bridge:wxid_a:file",
            "status": "accepted",
            "reason": "wechat_native_http_send_file_accepted_unverified",
            "created_at": "2026-07-09T13:03:56+00:00",
            "payload": {
                "backend": "wechat_native_http",
                "operation": "wechat_native_http_send_file",
                "response": {"wxid": "wxid_a", "ret": 0},
                "delivery_verified": False,
                "accepted_unverified": True,
                "delivery_verification": {
                    "verified": False,
                    "receiver": "wxid_a",
                    "file_name": "late.csv",
                    "file_size": 9,
                    "before": {"max_local_id": 31, "max_sort_seq": (now - 30) * 1000, "message_keys": []},
                },
            },
        }
        with _json_server(routes) as (base_url, requests):
            backend = WeChatNativeHttpSendBackend(
                base_url=base_url,
                verify_base_url=base_url,
                verify_token_env="WEFLOW_TEST_TOKEN",
                file_verify_timeout_seconds=45.0,
            )
            with mock.patch.dict(os.environ, {"WEFLOW_TEST_TOKEN": "secret"}, clear=False):
                result = backend.verify_accepted_bridge_record(record, ack)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.ok)
        self.assertTrue(result.delivery_verified)
        self.assertEqual(result.reason, "wechat_native_http_send_file_verified_late")
        self.assertEqual(result.external_message_id, "srv-file-late")
        self.assertEqual([item["method"] for item in requests], ["GET"])

    def test_wechat_native_backend_does_not_late_verify_ambiguous_duplicate_file(self) -> None:
        now = int(time.time())
        duplicated = {
            "createTime": now,
            "sortSeq": now * 1000,
            "isSend": 1,
            "content": (
                "<msg><appmsg><title>same.csv</title><type>6</type>"
                "<appattach><totallen>9</totallen></appattach></appmsg></msg>"
            ),
            "rawContent": (
                "<msg><appmsg><title>same.csv</title><type>6</type>"
                "<appattach><totallen>9</totallen></appattach></appmsg></msg>"
            ),
        }
        routes = {
            ("GET", "/api/v1/messages"): (
                200,
                {
                    "success": True,
                    "messages": [
                        {**duplicated, "localId": 42, "serverId": "srv-file-same-a", "messageKey": "same-a"},
                        {**duplicated, "localId": 43, "serverId": "srv-file-same-b", "messageKey": "same-b"},
                    ],
                },
            )
        }
        record = {
            "bridge_id": "bridge:wxid_a:file",
            "conversation_id": "wxid_a",
            "receiver": "wxid_a",
            "kind": "file",
            "path": "C:\\tmp\\same.csv",
            "name": "same.csv",
            "created_at": "2026-07-09T13:03:33+00:00",
        }
        ack = {
            "bridge_id": "bridge:wxid_a:file",
            "status": "accepted",
            "reason": "wechat_native_http_send_file_accepted_unverified",
            "created_at": "2026-07-09T13:03:56+00:00",
            "payload": {
                "backend": "wechat_native_http",
                "operation": "wechat_native_http_send_file",
                "response": {"wxid": "wxid_a", "ret": 0},
                "delivery_verified": False,
                "accepted_unverified": True,
                "delivery_verification": {
                    "verified": False,
                    "receiver": "wxid_a",
                    "file_name": "same.csv",
                    "file_size": 9,
                    "before": {"max_local_id": 31, "max_sort_seq": (now - 30) * 1000, "message_keys": []},
                },
            },
        }
        with _json_server(routes) as (base_url, requests):
            backend = WeChatNativeHttpSendBackend(
                base_url=base_url,
                verify_base_url=base_url,
                verify_token_env="WEFLOW_TEST_TOKEN",
                file_verify_timeout_seconds=45.0,
            )
            with mock.patch.dict(os.environ, {"WEFLOW_TEST_TOKEN": "secret"}, clear=False):
                result = backend.verify_accepted_bridge_record(record, ack)

        self.assertIsNone(result)
        self.assertEqual([item["method"] for item in requests], ["GET"])

    def test_wechat_native_backend_posts_image_and_file_to_custom_endpoints(self) -> None:
        routes = {
            ("POST", "/custom-image"): (200, {"ret": 0, "retmsg": "success"}),
            ("POST", "/custom-file"): (200, {"ret": 0, "retmsg": "success"}),
        }
        with _json_server(routes) as (base_url, requests):
            backend = WeChatNativeHttpSendBackend(
                base_url=base_url,
                image_path="/custom-image",
                file_path="/custom-file",
                verify_timeout_seconds=0,
                file_verify_timeout_seconds=0,
            )

            image = backend.send_file(SAFE_WECHAT_RECEIVER, "C:\\tmp\\a.png")
            document = backend.send_file(SAFE_WECHAT_RECEIVER, "C:\\tmp\\a.pptx")

        self.assertTrue(image.ok)
        self.assertTrue(document.ok)
        self.assertFalse(image.delivery_verified)
        self.assertFalse(document.delivery_verified)
        self.assertEqual(requests[0]["path"], "/custom-image")
        self.assertEqual(requests[0]["body"], {"wxidorgid": SAFE_WECHAT_RECEIVER, "path": "C:\\tmp\\a.png"})
        self.assertEqual(requests[1]["path"], "/custom-file")
        self.assertEqual(requests[1]["body"], {"wxid": SAFE_WECHAT_RECEIVER, "filepath": "C:\\tmp\\a.pptx", "stage": "send"})

    def test_wechat_native_backend_maps_ret_failure_to_failed_outcome(self) -> None:
        with _json_server({("POST", "/SendTextMsg"): (200, {"ret": 1, "retmsg": "not login"})}) as (base_url, _requests):
            backend = WeChatNativeHttpSendBackend(base_url=base_url, verify_timeout_seconds=0)

            result = backend.send_text(SAFE_WECHAT_RECEIVER, "hello")

        self.assertFalse(result.ok)
        self.assertIn("not login", result.reason)

    def test_wechat_native_backend_rejects_non_local_endpoint(self) -> None:
        backend = WeChatNativeHttpSendBackend(base_url="http://example.com:30001", verify_timeout_seconds=0)

        result = backend.send_text(SAFE_WECHAT_RECEIVER, "hello")

        self.assertFalse(result.ok)
        self.assertIn("localhost", result.reason)

    def test_build_send_backend_passes_configured_wechat_native_http(self) -> None:
        backend = build_send_backend(
            BotConfig(
                send_backend="wechat_native_http",
                wechat_native_base_url="http://127.0.0.1:30001",
                wechat_native_send_text_path="/custom-text",
                wechat_native_send_image_path="/custom-image",
                wechat_native_send_file_path="/custom-file",
                wechat_native_status_path="/custom-status",
                wechat_native_timeout_seconds=4.5,
                wechat_native_verify_timeout_seconds=1.5,
                wechat_native_file_verify_timeout_seconds=12.5,
                weflow_base_url="http://127.0.0.1:5039",
                weflow_token_env="WEFLOW_VERIFY_TOKEN",
            )
        )

        self.assertIsInstance(backend, WeChatNativeHttpSendBackend)
        self.assertEqual(backend.text_path, "/custom-text")
        self.assertEqual(backend.image_path, "/custom-image")
        self.assertEqual(backend.file_path, "/custom-file")
        self.assertEqual(backend.status_path, "/custom-status")
        self.assertEqual(backend.timeout_seconds, 4.5)
        self.assertEqual(backend.verify_timeout_seconds, 1.5)
        self.assertEqual(backend.file_verify_timeout_seconds, 12.5)
        self.assertEqual(backend.verify_base_url, "http://127.0.0.1:5039")
        self.assertEqual(backend.verify_token_env, "WEFLOW_VERIFY_TOKEN")


if __name__ == "__main__":
    unittest.main()

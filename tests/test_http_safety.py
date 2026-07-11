from __future__ import annotations

import socket
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.error import URLError
from urllib.request import Request

from app.personal_wechat_bot.tools.web.http_safety import (
    HttpResponseLimitError,
    LocalHttpUrlError,
    PublicHttpUrlError,
    _PinnedHTTPSConnection,
    _PublicOnlyRedirectHandler,
    _ResolvedEndpoint,
    _SameAuthorityRedirectHandler,
    _open_pinned_socket,
    guarded_urlopen,
    guarded_local_urlopen,
    guarded_same_authority_urlopen,
    read_response_with_deadline,
    validate_public_http_url,
)


@contextmanager
def _recording_server(handler_type: type[BaseHTTPRequestHandler]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _ChunkedResponse:
    def __init__(self, chunks: list[bytes], *, delay_seconds: float = 0.0) -> None:
        self.headers: dict[str, str] = {}
        self.chunks = list(chunks)
        self.delay_seconds = delay_seconds

    def read1(self, _size: int) -> bytes:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return self.chunks.pop(0) if self.chunks else b""


class _DeadlineEofSocket:
    def __init__(self) -> None:
        self.closed = threading.Event()

    def settimeout(self, _timeout: float) -> None:
        return None

    def shutdown(self, _how: int) -> None:
        self.closed.set()


class _DeadlineEofResponse:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self._sock = _DeadlineEofSocket()

    def read1(self, _size: int) -> bytes:
        self._sock.closed.wait(1.0)
        return b""


class HttpSafetyTest(unittest.TestCase):
    def test_local_open_ignores_environment_proxy(self) -> None:
        seen: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen.append(self.path)
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args) -> None:
                return

        with _recording_server(Handler) as server:
            url = f"http://127.0.0.1:{server.server_port}/direct"
            with mock.patch.dict(
                "os.environ",
                {
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "http_proxy": "http://127.0.0.1:1",
                    "NO_PROXY": "",
                    "no_proxy": "",
                },
                clear=False,
            ):
                with guarded_local_urlopen(url, timeout_seconds=1.0) as response:
                    self.assertEqual(response.read(), b"{}")

        self.assertEqual(seen, ["/direct"])

    def test_local_open_allows_same_authority_redirect_with_authorization(self) -> None:
        seen: list[tuple[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen.append((self.path, self.headers.get("Authorization", "")))
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/finish")
                    self.end_headers()
                    return
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args) -> None:
                return

        with _recording_server(Handler) as server:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/start",
                headers={"Authorization": "Bearer secret"},
            )
            with guarded_local_urlopen(request, timeout_seconds=1.0) as response:
                self.assertEqual(response.read(), b"{}")

        self.assertEqual(seen, [("/start", "Bearer secret"), ("/finish", "Bearer secret")])

    def test_local_open_rejects_cross_authority_redirect_before_token_leaves_origin(self) -> None:
        target_authorizations: list[str] = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                target_authorizations.append(self.headers.get("Authorization", ""))
                self.send_response(200)
                self.end_headers()

            def log_message(self, _format: str, *_args) -> None:
                return

        with _recording_server(TargetHandler) as target:
            target_url = f"http://127.0.0.1:{target.server_port}/stolen"

            class OriginHandler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:
                    self.send_response(302)
                    self.send_header("Location", target_url)
                    self.end_headers()

                def log_message(self, _format: str, *_args) -> None:
                    return

            with _recording_server(OriginHandler) as origin:
                request = Request(
                    f"http://127.0.0.1:{origin.server_port}/start",
                    headers={"Authorization": "Bearer secret"},
                )
                with self.assertRaisesRegex(LocalHttpUrlError, "redirect_authority"):
                    guarded_local_urlopen(request, timeout_seconds=1.0)

        self.assertEqual(target_authorizations, [])

    def test_local_open_rejects_https_redirect_before_dispatch(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", f"https://127.0.0.1:{self.server.server_port}/tls")
                self.end_headers()

            def log_message(self, _format: str, *_args) -> None:
                return

        with _recording_server(Handler) as server:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/start",
                headers={"Authorization": "Bearer secret"},
            )
            with self.assertRaisesRegex(LocalHttpUrlError, "local_http_required"):
                guarded_local_urlopen(request, timeout_seconds=1.0)

    def test_local_open_rejects_localhost_when_dns_contains_non_loopback_address(self) -> None:
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.0.2.10", 80)),
        ]
        with mock.patch("socket.getaddrinfo", return_value=addresses):
            with self.assertRaisesRegex(LocalHttpUrlError, "loopback_endpoint_required"):
                guarded_local_urlopen("http://localhost/resource", timeout_seconds=0.2)

    def test_same_authority_redirect_policy_rejects_https_downgrade(self) -> None:
        handler = _SameAuthorityRedirectHandler(authority=("https", "example.com", 443))
        response = mock.Mock()
        with self.assertRaisesRegex(PublicHttpUrlError, "redirect_authority_not_allowed"):
            handler.redirect_request(
                Request("https://example.com/start", headers={"Authorization": "Bearer secret"}),
                response,
                302,
                "Found",
                {},
                "http://example.com/finish",
            )
        response.close.assert_called_once_with()

    def test_same_authority_open_rejects_preconfigured_proxy_request(self) -> None:
        request = Request("https://example.com/resource")
        request.set_proxy("127.0.0.1:8888", "http")
        with self.assertRaisesRegex(PublicHttpUrlError, "proxy_not_allowed"):
            guarded_same_authority_urlopen(request, timeout_seconds=0.2)

    def test_same_authority_open_ignores_environment_proxy(self) -> None:
        seen: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen.append(self.path)
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args) -> None:
                return

        with _recording_server(Handler) as server:
            url = f"http://127.0.0.1:{server.server_port}/direct"
            with mock.patch.dict(
                "os.environ",
                {
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "http_proxy": "http://127.0.0.1:1",
                    "NO_PROXY": "",
                    "no_proxy": "",
                },
                clear=False,
            ):
                with guarded_same_authority_urlopen(url, timeout_seconds=1.0) as response:
                    self.assertEqual(response.read(), b"{}")

        self.assertEqual(seen, ["/direct"])

    def test_bounded_reader_rejects_declared_and_streamed_oversize_bodies(self) -> None:
        declared = mock.Mock()
        declared.headers = {"Content-Length": "9"}
        with self.assertRaisesRegex(HttpResponseLimitError, "9>8"):
            read_response_with_deadline(declared, max_bytes=8, deadline=time.monotonic() + 1)

        streamed = _ChunkedResponse([b"1234", b"5678", b"9", b""])
        with self.assertRaisesRegex(HttpResponseLimitError, "9>8"):
            read_response_with_deadline(streamed, max_bytes=8, deadline=time.monotonic() + 1)

    def test_bounded_reader_enforces_total_deadline_across_slow_chunks(self) -> None:
        response = _ChunkedResponse([b"a", b"b", b"c"], delay_seconds=0.04)

        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "deadline_exceeded"):
            read_response_with_deadline(
                response,
                max_bytes=100,
                deadline=started + 0.07,
                chunk_bytes=1,
            )

        self.assertLess(time.monotonic() - started, 0.2)

    def test_bounded_reader_treats_deadline_induced_eof_as_timeout(self) -> None:
        response = _DeadlineEofResponse()

        with self.assertRaisesRegex(TimeoutError, "deadline_exceeded"):
            read_response_with_deadline(
                response,
                max_bytes=100,
                deadline=time.monotonic() + 0.04,
            )

    def test_bounded_reader_can_preserve_truncating_callers(self) -> None:
        response = _ChunkedResponse([b"1234", b"5678", b"9"])
        response.headers = {"Content-Length": "9"}

        body = read_response_with_deadline(
            response,
            max_bytes=8,
            deadline=time.monotonic() + 1,
            truncate=True,
        )

        self.assertEqual(body, b"12345678")

    def test_blocks_loopback_private_link_local_and_credentials(self) -> None:
        urls = (
            "http://127.0.0.1/status",
            "http://[::1]/status",
            "http://10.0.0.1/status",
            "http://169.254.169.254/latest/meta-data",
            "http://user:password@example.com/",
        )
        for url in urls:
            with self.subTest(url=url), self.assertRaises(PublicHttpUrlError):
                validate_public_http_url(url)

    def test_blocks_hostname_when_any_resolved_address_is_not_public(self) -> None:
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443)),
        ]
        with mock.patch("socket.getaddrinfo", return_value=addresses):
            with self.assertRaisesRegex(PublicHttpUrlError, "non_public_url"):
                validate_public_http_url("https://example.com/")

    def test_allows_hostname_when_all_resolved_addresses_are_public(self) -> None:
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0)),
        ]
        with mock.patch("socket.getaddrinfo", return_value=addresses):
            validate_public_http_url("https://example.com/")

    def test_redirect_handler_revalidates_target(self) -> None:
        handler = _PublicOnlyRedirectHandler(allow_private_network=False)
        response = mock.Mock()
        with self.assertRaisesRegex(PublicHttpUrlError, "non_public_url"):
            handler.redirect_request(
                Request("https://example.com/start"),
                response,
                302,
                "Found",
                {},
                "http://127.0.0.1/internal",
            )
        response.close.assert_called_once_with()

    def test_malformed_urls_are_reported_as_invalid(self) -> None:
        urls = (
            "http://[::1",
            "http://[]/",
            "http://example.com:not-a-port/",
            "http://example.com:0/",
            "http://example.com:99999/",
        )
        for url in urls:
            with self.subTest(url=url), self.assertRaisesRegex(PublicHttpUrlError, "invalid_url"):
                validate_public_http_url(url)

    def test_guarded_open_connects_only_to_the_validated_address(self) -> None:
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 80))]
        sock = mock.Mock()
        sock.connect.side_effect = OSError("stop after endpoint capture")
        with (
            mock.patch("socket.getaddrinfo", return_value=addresses) as resolver,
            mock.patch("socket.socket", return_value=sock),
            self.assertRaises(URLError),
        ):
            guarded_urlopen("http://rebind.example/resource", timeout_seconds=0.2)

        resolver.assert_called_once()
        sock.connect.assert_called_once_with(("93.184.216.34", 80))

    def test_pinned_https_connection_preserves_original_sni(self) -> None:
        endpoint = _ResolvedEndpoint(
            family=socket.AF_INET,
            socktype=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
            sockaddr=("93.184.216.34", 443),
            address="93.184.216.34",
        )
        raw_socket = mock.Mock()
        tls_socket = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.return_value = tls_socket
        with mock.patch("socket.socket", return_value=raw_socket):
            connection = _PinnedHTTPSConnection(
                "example.com",
                timeout=1.0,
                context=context,
                pinned_endpoints=(endpoint,),
            )
            connection.connect()

        raw_socket.connect.assert_called_once_with(("93.184.216.34", 443))
        context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="example.com")
        self.assertEqual(connection.host, "example.com")

    def test_pinned_endpoints_share_one_connect_deadline(self) -> None:
        endpoints = tuple(
            _ResolvedEndpoint(
                family=socket.AF_INET,
                socktype=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
                sockaddr=(f"93.184.216.{index}", 443),
                address=f"93.184.216.{index}",
            )
            for index in range(1, 4)
        )

        class _SlowSocket:
            def settimeout(self, timeout: float) -> None:
                self.timeout = timeout

            def connect(self, _address: tuple[str, int]) -> None:
                time.sleep(min(0.03, self.timeout))
                raise TimeoutError("blackhole")

            def close(self) -> None:
                return None

        started = time.monotonic()
        with mock.patch("socket.socket", side_effect=lambda *_args: _SlowSocket()):
            with self.assertRaisesRegex(TimeoutError, "deadline"):
                _open_pinned_socket(
                    endpoints,
                    ("example.com", 443),
                    timeout=0.04,
                    deadline=started + 0.04,
                )

        self.assertLess(time.monotonic() - started, 0.075)

    def test_guarded_open_bounds_dns_resolution(self) -> None:
        release = threading.Event()

        def blocked_resolution(*_args, **_kwargs):
            release.wait(1.0)
            return []

        started = time.monotonic()
        try:
            with mock.patch("socket.getaddrinfo", side_effect=blocked_resolution):
                with self.assertRaisesRegex(TimeoutError, "resolution_deadline"):
                    guarded_urlopen("http://slow-dns.example/resource", timeout_seconds=0.2)
        finally:
            release.set()

        self.assertLess(time.monotonic() - started, 0.4)

    def test_preconfigured_proxy_request_is_rejected(self) -> None:
        request = Request("https://example.com/resource")
        request.set_proxy("127.0.0.1:8888", "http")

        with self.assertRaisesRegex(PublicHttpUrlError, "proxy_not_allowed"):
            guarded_urlopen(request, timeout_seconds=0.2)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import http.client
import ipaddress
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import partial
from typing import Any
from urllib.parse import urlparse


class PublicHttpUrlError(ValueError):
    """Raised when a web-tool URL crosses the public-network boundary."""


class LocalHttpUrlError(ValueError):
    """Raised when a local bridge URL crosses its loopback HTTP boundary."""


class HttpResponseLimitError(ValueError):
    """Raised when a web response exceeds its declared or observed byte limit."""


@dataclass(frozen=True)
class _ResolvedEndpoint:
    family: int
    socktype: int
    proto: int
    sockaddr: tuple[Any, ...]
    address: str


_DNS_RESOLUTION_SLOTS = threading.BoundedSemaphore(8)


def validate_public_http_url(url: str, *, allow_private_network: bool = False) -> None:
    _validated_endpoints(url, allow_private_network=allow_private_network)


def validate_local_http_url(url: str) -> None:
    _parse_local_http_target(url)


def validate_http_url(url: str) -> None:
    _parse_http_target(url)


def _parse_http_target(url: str) -> tuple[str, int]:
    try:
        parsed = urlparse(str(url or "").strip())
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
        explicit_port = parsed.port
    except (TypeError, UnicodeError, ValueError) as exc:
        raise PublicHttpUrlError("invalid_url") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise PublicHttpUrlError("invalid_url")
    if explicit_port == 0:
        raise PublicHttpUrlError("invalid_url")
    port = explicit_port if explicit_port is not None else (443 if parsed.scheme == "https" else 80)
    if username is not None or password is not None:
        raise PublicHttpUrlError("url_credentials_not_allowed")

    host = hostname.rstrip(".").lower()
    if not host or any(character.isspace() or ord(character) < 32 for character in host):
        raise PublicHttpUrlError("invalid_url")
    try:
        host.encode("idna")
    except UnicodeError as exc:
        raise PublicHttpUrlError("invalid_url") from exc
    return host, port


def _validated_endpoints(
    url: str,
    *,
    allow_private_network: bool,
    deadline: float | None = None,
) -> tuple[_ResolvedEndpoint, ...]:
    host, port = _parse_http_target(url)
    if host == "localhost" or host.endswith(".localhost"):
        if not allow_private_network:
            raise PublicHttpUrlError("non_public_url")
    endpoints = _resolved_endpoints(host, port, deadline=deadline)
    if not endpoints:
        raise PublicHttpUrlError("non_public_url")
    if not allow_private_network and any(not _is_public_address(endpoint.address) for endpoint in endpoints):
        raise PublicHttpUrlError("non_public_url")
    return endpoints


def guarded_urlopen(
    request: urllib.request.Request | str,
    *,
    timeout_seconds: float,
    allow_private_network: bool = False,
) -> Any:
    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    _parse_http_target(url)
    if isinstance(request, urllib.request.Request) and (
        request.has_proxy() or getattr(request, "_tunnel_host", None) is not None
    ):
        raise PublicHttpUrlError("proxy_not_allowed")
    timeout = max(0.2, float(timeout_seconds))
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _PinnedHTTPHandler(allow_private_network=allow_private_network, deadline=deadline),
        _PinnedHTTPSHandler(allow_private_network=allow_private_network, deadline=deadline),
        _PublicOnlyRedirectHandler(allow_private_network=allow_private_network, deadline=deadline),
    )
    try:
        return opener.open(request, timeout=_deadline_remaining(deadline, error="http_open_deadline_exceeded"))
    except urllib.error.HTTPError as exc:
        exc.close()
        raise


def guarded_local_urlopen(
    request: urllib.request.Request | str,
    *,
    timeout_seconds: float,
    deadline: float | None = None,
) -> Any:
    """Open one loopback-only HTTP request without proxies or unsafe redirects.

    This policy is intentionally separate from ``guarded_urlopen``. Web tools
    may opt into private networks, while local bridge credentials must remain
    on the original loopback authority for every redirect hop.
    """

    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    authority = _parse_local_http_target(url)
    if isinstance(request, urllib.request.Request) and (
        request.has_proxy() or getattr(request, "_tunnel_host", None) is not None
    ):
        raise LocalHttpUrlError("proxy_not_allowed")
    timeout = max(0.2, float(timeout_seconds))
    request_deadline = deadline if deadline is not None else time.monotonic() + timeout
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _LocalOnlyHTTPHandler(deadline=request_deadline),
        _RejectLocalHTTPSHandler(),
        _SameAuthorityLocalRedirectHandler(authority=authority, deadline=request_deadline),
    )
    return opener.open(
        request,
        timeout=_deadline_remaining(request_deadline, error="http_open_deadline_exceeded"),
    )


def guarded_same_authority_urlopen(
    request: urllib.request.Request | str,
    *,
    timeout_seconds: float,
    deadline: float | None = None,
) -> Any:
    """Open HTTP(S) with pinned DNS and same-scheme/authority redirects only."""

    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    authority = _parse_http_authority(url)
    if isinstance(request, urllib.request.Request) and (
        request.has_proxy() or getattr(request, "_tunnel_host", None) is not None
    ):
        raise PublicHttpUrlError("proxy_not_allowed")
    timeout = max(0.2, float(timeout_seconds))
    request_deadline = deadline if deadline is not None else time.monotonic() + timeout
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _PinnedHTTPHandler(allow_private_network=True, deadline=request_deadline),
        _PinnedHTTPSHandler(allow_private_network=True, deadline=request_deadline),
        _SameAuthorityRedirectHandler(authority=authority, deadline=request_deadline),
    )
    return opener.open(
        request,
        timeout=_deadline_remaining(request_deadline, error="http_open_deadline_exceeded"),
    )


def read_response_with_deadline(
    response: Any,
    *,
    max_bytes: int,
    deadline: float,
    chunk_bytes: int = 64 * 1024,
    truncate: bool = False,
) -> bytes:
    """Read a bounded response without allowing a slow drip past ``deadline``."""

    byte_limit = max(0, int(max_bytes))
    content_length = _response_content_length(response)
    if not truncate and content_length is not None and content_length > byte_limit:
        raise HttpResponseLimitError(f"http_response_too_large:{content_length}>{byte_limit}")

    reader = _response_reader(response)
    chunks: list[bytes] = []
    total = 0
    deadline_expired = threading.Event()

    def abort_at_deadline() -> None:
        deadline_expired.set()
        sock = _response_socket(response)
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    timer = threading.Timer(_deadline_remaining(deadline), abort_at_deadline)
    timer.daemon = True
    timer.start()
    try:
        while True:
            remaining = _deadline_remaining(deadline)
            sock = _response_socket(response)
            if sock is not None:
                sock.settimeout(remaining)
            if truncate and total >= byte_limit:
                break
            read_size = min(
                max(1, int(chunk_bytes)),
                max(1, byte_limit - total) if truncate else max(1, byte_limit + 1 - total),
            )
            chunk = reader(read_size)
            if not chunk:
                if deadline_expired.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("http_response_deadline_exceeded")
                break
            if not isinstance(chunk, bytes):
                raise TypeError("http_response_body_not_bytes")
            total += len(chunk)
            if total > byte_limit:
                if truncate:
                    chunks.append(chunk[: len(chunk) - (total - byte_limit)])
                    break
                raise HttpResponseLimitError(f"http_response_too_large:{total}>{byte_limit}")
            chunks.append(chunk)
    except Exception as exc:
        if deadline_expired.is_set() or time.monotonic() >= deadline:
            raise TimeoutError("http_response_deadline_exceeded") from exc
        raise
    finally:
        timer.cancel()
    if deadline_expired.is_set() or time.monotonic() >= deadline:
        raise TimeoutError("http_response_deadline_exceeded")
    return b"".join(chunks)


def _deadline_remaining(deadline: float, *, error: str = "http_response_deadline_exceeded") -> float:
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(error)
    return remaining


def _response_content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    value = headers.get("Content-Length") if headers is not None else None
    try:
        length = int(value)
    except (TypeError, ValueError):
        return None
    return length if length >= 0 else None


def _response_objects(response: Any) -> list[Any]:
    pending = [response]
    objects: list[Any] = []
    seen: set[int] = set()
    while pending and len(objects) < 8:
        current = pending.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        objects.append(current)
        pending.extend((getattr(current, "fp", None), getattr(current, "raw", None)))
    return objects


def _response_reader(response: Any) -> Any:
    for current in _response_objects(response):
        read1 = getattr(current, "read1", None)
        if callable(read1):
            return read1
    read = getattr(response, "read", None)
    if not callable(read):
        raise TypeError("http_response_body_not_readable")
    return read


def _response_socket(response: Any) -> Any | None:
    for current in _response_objects(response):
        sock = getattr(current, "_sock", None)
        if sock is not None and callable(getattr(sock, "settimeout", None)):
            return sock
    return None


class _PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, allow_private_network: bool, deadline: float | None = None) -> None:
        super().__init__()
        self.allow_private_network = allow_private_network
        self.deadline = deadline

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            _validated_endpoints(
                newurl,
                allow_private_network=self.allow_private_network,
                deadline=self.deadline,
            )
        except Exception:
            if fp is not None:
                fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _parse_http_authority(url: str) -> tuple[str, str, int]:
    try:
        scheme = urlparse(str(url or "").strip()).scheme
    except (TypeError, UnicodeError, ValueError) as exc:
        raise PublicHttpUrlError("invalid_url") from exc
    host, port = _parse_http_target(url)
    return scheme, host, port


class _SameAuthorityRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, authority: tuple[str, str, int], deadline: float | None = None) -> None:
        super().__init__()
        self.authority = authority
        self.deadline = deadline

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            current_authority = _parse_http_authority(req.full_url)
            target_authority = _parse_http_authority(newurl)
            if current_authority != self.authority or target_authority != self.authority:
                raise PublicHttpUrlError("redirect_authority_not_allowed")
            if req.has_proxy() or getattr(req, "_tunnel_host", None) is not None:
                raise PublicHttpUrlError("proxy_not_allowed")
            _validated_endpoints(newurl, allow_private_network=True, deadline=self.deadline)
        except Exception:
            if fp is not None:
                fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _parse_local_http_target(url: str) -> tuple[str, int]:
    try:
        parsed = urlparse(str(url or "").strip())
    except (TypeError, UnicodeError, ValueError) as exc:
        raise LocalHttpUrlError("invalid_local_http_url") from exc
    try:
        host, port = _parse_http_target(url)
    except PublicHttpUrlError as exc:
        raise LocalHttpUrlError(str(exc)) from exc
    if parsed.scheme != "http":
        raise LocalHttpUrlError("local_http_required")

    if host == "localhost" or host.endswith(".localhost"):
        return host, port
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError as exc:
        raise LocalHttpUrlError("loopback_host_required") from exc
    if not address.is_loopback:
        raise LocalHttpUrlError("loopback_host_required")
    return str(address), port


def _validated_local_endpoints(
    url: str,
    *,
    deadline: float | None = None,
) -> tuple[_ResolvedEndpoint, ...]:
    host, port = _parse_local_http_target(url)
    endpoints = _resolved_endpoints(host, port, deadline=deadline)
    if not endpoints or any(not _is_loopback_address(endpoint.address) for endpoint in endpoints):
        raise LocalHttpUrlError("loopback_endpoint_required")
    return endpoints


class _SameAuthorityLocalRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, authority: tuple[str, int], deadline: float | None = None) -> None:
        super().__init__()
        self.authority = authority
        self.deadline = deadline

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            current_authority = _parse_local_http_target(req.full_url)
            target_authority = _parse_local_http_target(newurl)
            if current_authority != self.authority or target_authority != self.authority:
                raise LocalHttpUrlError("local_redirect_authority_not_allowed")
            if req.has_proxy() or getattr(req, "_tunnel_host", None) is not None:
                raise LocalHttpUrlError("proxy_not_allowed")
            _validated_local_endpoints(newurl, deadline=self.deadline)
        except Exception:
            if fp is not None:
                fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _LocalOnlyHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, *, deadline: float | None = None) -> None:
        super().__init__()
        self.deadline = deadline

    def http_open(self, req):
        endpoints = _validated_local_endpoints(req.full_url, deadline=self.deadline)
        connection = partial(_PinnedHTTPConnection, pinned_endpoints=endpoints, deadline=self.deadline)
        return self.do_open(connection, req)


class _RejectLocalHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        raise LocalHttpUrlError("local_http_required")


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, *, allow_private_network: bool, deadline: float | None = None) -> None:
        super().__init__()
        self.allow_private_network = allow_private_network
        self.deadline = deadline

    def http_open(self, req):
        endpoints = _validated_endpoints(
            req.full_url,
            allow_private_network=self.allow_private_network,
            deadline=self.deadline,
        )
        connection = partial(_PinnedHTTPConnection, pinned_endpoints=endpoints, deadline=self.deadline)
        return self.do_open(connection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, *, allow_private_network: bool, deadline: float | None = None) -> None:
        super().__init__()
        self.allow_private_network = allow_private_network
        self.deadline = deadline

    def https_open(self, req):
        endpoints = _validated_endpoints(
            req.full_url,
            allow_private_network=self.allow_private_network,
            deadline=self.deadline,
        )
        connection = partial(_PinnedHTTPSConnection, pinned_endpoints=endpoints, deadline=self.deadline)
        return self.do_open(connection, req, context=self._context)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        host,
        port=None,
        *,
        pinned_endpoints: tuple[_ResolvedEndpoint, ...],
        deadline: float | None = None,
        **kwargs,
    ) -> None:
        super().__init__(host, port, **kwargs)
        self._create_connection = partial(_open_pinned_socket, pinned_endpoints, deadline=deadline)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host,
        port=None,
        *,
        pinned_endpoints: tuple[_ResolvedEndpoint, ...],
        deadline: float | None = None,
        **kwargs,
    ) -> None:
        super().__init__(host, port, **kwargs)
        self._create_connection = partial(_open_pinned_socket, pinned_endpoints, deadline=deadline)


def _open_pinned_socket(
    endpoints: tuple[_ResolvedEndpoint, ...],
    _address: tuple[str, int],
    timeout: float | object = socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address: tuple[str, int] | None = None,
    *,
    deadline: float | None = None,
):
    if deadline is None and timeout is not socket._GLOBAL_DEFAULT_TIMEOUT and timeout is not None:
        deadline = time.monotonic() + max(0.0, float(timeout))
    last_error: OSError | None = None
    for endpoint in endpoints:
        endpoint_timeout: float | object | None = timeout
        if deadline is not None:
            endpoint_timeout = _deadline_remaining(deadline, error="http_connect_deadline_exceeded")
        sock = socket.socket(endpoint.family, endpoint.socktype, endpoint.proto)
        try:
            if endpoint_timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(endpoint_timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(endpoint.sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    if last_error is not None:
        raise last_error
    raise OSError("no_validated_connection_endpoint")


def _resolved_endpoints(
    host: str,
    port: int,
    *,
    deadline: float | None = None,
) -> tuple[_ResolvedEndpoint, ...]:
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        try:
            results = _getaddrinfo(host, port, deadline=deadline)
        except (socket.gaierror, UnicodeError) as exc:
            raise OSError(f"url_resolution_failed:{host}") from exc
        endpoints: list[_ResolvedEndpoint] = []
        seen: set[tuple[int, tuple[Any, ...]]] = set()
        for family, socktype, proto, _canonname, sockaddr in results:
            if family not in {socket.AF_INET, socket.AF_INET6} or not sockaddr:
                continue
            normalized_sockaddr = tuple(sockaddr)
            key = (family, normalized_sockaddr)
            if key in seen:
                continue
            seen.add(key)
            endpoints.append(
                _ResolvedEndpoint(
                    family=family,
                    socktype=socktype or socket.SOCK_STREAM,
                    proto=proto,
                    sockaddr=normalized_sockaddr,
                    address=str(sockaddr[0]).split("%", 1)[0],
                )
            )
        return tuple(endpoints)
    if literal.version == 6:
        return (
            _ResolvedEndpoint(
                family=socket.AF_INET6,
                socktype=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
                sockaddr=(str(literal), port, 0, 0),
                address=str(literal),
            ),
        )
    return (
        _ResolvedEndpoint(
            family=socket.AF_INET,
            socktype=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
            sockaddr=(str(literal), port),
            address=str(literal),
        ),
    )


def _getaddrinfo(host: str, port: int, *, deadline: float | None) -> list[tuple[Any, ...]]:
    if deadline is None:
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)

    remaining = _deadline_remaining(deadline, error="url_resolution_deadline_exceeded")
    if not _DNS_RESOLUTION_SLOTS.acquire(timeout=remaining):
        raise TimeoutError("url_resolution_deadline_exceeded")

    completed = threading.Event()
    outcome: dict[str, Any] = {}

    def resolve() -> None:
        try:
            outcome["results"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception as exc:
            outcome["error"] = exc
        finally:
            _DNS_RESOLUTION_SLOTS.release()
            completed.set()

    worker = threading.Thread(target=resolve, name="public-http-dns", daemon=True)
    try:
        worker.start()
    except Exception:
        _DNS_RESOLUTION_SLOTS.release()
        raise
    if not completed.wait(_deadline_remaining(deadline, error="url_resolution_deadline_exceeded")):
        raise TimeoutError("url_resolution_deadline_exceeded")
    if "error" in outcome:
        raise outcome["error"]
    return list(outcome.get("results") or [])


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(address.is_global and not address.is_multicast and not address.is_unspecified)


def _is_loopback_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False

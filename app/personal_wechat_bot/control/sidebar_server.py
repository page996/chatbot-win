from __future__ import annotations

import json
import mimetypes
import threading
import time
from ipaddress import ip_address
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from app.personal_wechat_bot.control.sidebar_api import (
    HistoryResetNotScheduledError,
    ack_sidebar_bridge_item,
    add_api_key,
    append_sidebar_backend_event,
    build_sidebar_bridge_state,
    build_sidebar_runtime_cards,
    build_sidebar_task_manager,
    build_sidebar_weflow_state,
    build_sidebar_wechat_probe,
    build_sidebar_state,
    clear_sidebar_send_audit,
    clear_sidebar_history_data,
    cleanup_sidebar_channels,
    cleanup_file_workspace,
    delete_sidebar_channel,
    get_model_config,
    list_api_keys,
    probe_model_fetch,
    remove_api_key,
    retry_sidebar_bridge_item,
    set_model_config,
    sidebar_native_migration_probe,
    sidebar_storage_migration_status,
    sidebar_agent_start,
    sidebar_agent_stop,
    sidebar_agent_tick,
    sidebar_channel_test_file,
    sidebar_channel_test_reply,
    sidebar_queue_action,
    sidebar_channel_state_action,
    sidebar_diagnostics_export,
    sidebar_runtime_probe,
    sidebar_runtime_card_action,
    sidebar_resource_audit,
    sidebar_history_reset_status,
    sidebar_task_action,
    sidebar_weflow_dependency_status,
    sidebar_weflow_backfill,
    sidebar_weflow_cancel_backfill,
    sidebar_weflow_clear_history,
    sidebar_weflow_discover_sessions,
    sidebar_weflow_health,
    sidebar_weflow_install_deps,
    sidebar_weflow_pull_once,
    sidebar_weflow_start,
    sidebar_weflow_stop,
    update_sidebar_controls,
)
from app.personal_wechat_bot.runtime.history_fence import history_writer_fence_if_owned


STATIC_ROOT = Path(__file__).resolve().parents[1] / "ui" / "sidebar"
_MAX_POST_BODY_BYTES = 1024 * 1024
_POST_BODY_READ_TIMEOUT_SECONDS = 5.0
_POST_BODY_READ_CHUNK_BYTES = 64 * 1024


class _SidebarRequestError(ValueError):
    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status = int(status)
        self.error = str(error)


def run_sidebar_server(data_dir: str | Path = "data", host: str = "127.0.0.1", port: int = 8765) -> None:
    handler = _handler_factory(Path(data_dir))
    server = ThreadingHTTPServer((host, port), handler)
    print(f"sidebar listening on http://{host}:{port}")
    server.serve_forever()


def _handler_factory(data_dir: Path) -> type[BaseHTTPRequestHandler]:
    post_admission_lock = threading.RLock()

    class SidebarHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            host_error = self._host_check()
            if host_error:
                self._json({"status": "error", "error": host_error}, status=403)
                return
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                deferred: list[tuple[dict[str, Any], int]] = []
                self._deferred_json_responses = deferred
                try:
                    with history_writer_fence_if_owned(data_dir, label="sidebar_http_get"):
                        self._dispatch_GET(parsed.path)
                finally:
                    del self._deferred_json_responses
                self._flush_deferred_json_response(deferred)
                return
            self._static(parsed.path)

        def _dispatch_GET(self, path: str) -> None:
            if path == "/api/state":
                self._json(build_sidebar_state(data_dir))
                return
            if path in {"/api/driver-probe", "/api/wechat-probe"}:
                self._json({"status": "error", "error": "method_not_allowed"}, status=405)
                return
            if path == "/api/bridge":
                self._json(build_sidebar_bridge_state(data_dir))
                return
            if path == "/api/runtime-cards":
                self._json(build_sidebar_runtime_cards(data_dir))
                return
            if path == "/api/tasks":
                self._json(build_sidebar_task_manager(data_dir))
                return
            if path == "/api/weflow/status":
                self._json(build_sidebar_weflow_state(data_dir))
                return
            if path == "/api/diagnostics/export":
                self._json(sidebar_diagnostics_export(data_dir, {"persist": False}))
                return
            if path == "/api/storage/status":
                self._json(sidebar_storage_migration_status(data_dir, {"include_sizes": True}))
                return
            if path == "/api/keys":
                self._json(list_api_keys(data_dir))
                return
            if path == "/api/model-config":
                self._json(get_model_config(data_dir))
                return
            self._static(path)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            # CSRF / cross-origin guard. The sidebar UI is served from and calls
            # the same loopback origin, so a legitimate request either carries no
            # Origin (same-origin fetch in some browsers) or an Origin/Referer
            # whose host matches our own Host header. A cross-site page trying to
            # drive these mutating endpoints (add/remove keys, model-config,
            # probe -> key egress) fails this check and is rejected before any
            # handler execution and before any API key is touched.
            csrf_error = self._csrf_check()
            if csrf_error:
                error_payload: dict[str, Any] = {"status": "error", "error": csrf_error}
                if parsed.path == "/api/history/clear":
                    error_payload["history_reset_not_scheduled"] = True
                self._json(error_payload, status=403)
                return
            if self._reject_post_during_history_reset(parsed.path):
                return
            try:
                payload = self._read_json()
                if parsed.path == "/api/history/clear":
                    # HTTP callers always use the shutdown helper. The request
                    # itself must not hold the writer fence because the helper
                    # waits until this server has exited before taking it.
                    clear_payload = dict(payload)
                    clear_payload["shutdown_processes"] = True
                    with post_admission_lock:
                        clear_result = clear_sidebar_history_data(data_dir, clear_payload)
                    self._json(clear_result)
                    return
                if parsed.path.startswith("/api/"):
                    deferred = []
                    self._deferred_json_responses = deferred
                    try:
                        with post_admission_lock:
                            with history_writer_fence_if_owned(data_dir, label="sidebar_http_post"):
                                if not self._reject_post_during_history_reset(parsed.path):
                                    self._dispatch_POST(parsed.path, payload)
                    finally:
                        del self._deferred_json_responses
                    self._flush_deferred_json_response(deferred)
                    return
                self._json({"status": "error", "error": "not_found"}, status=404)
            except _SidebarRequestError as exc:
                error_payload = {"status": "error", "error": exc.error}
                if parsed.path == "/api/history/clear":
                    error_payload["history_reset_not_scheduled"] = True
                self._json(error_payload, status=exc.status)
            except HistoryResetNotScheduledError as exc:
                self._json(
                    {
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "history_reset_not_scheduled": True,
                    },
                    status=400,
                )
            except Exception as exc:
                self._json({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, status=400)

        def _reject_post_during_history_reset(self, path: str) -> bool:
            if path == "/api/history/clear" or not path.startswith("/api/"):
                return False
            reset = sidebar_history_reset_status(data_dir)
            if not bool(reset.get("active") or reset.get("outcome_unknown")):
                return False
            self._json(
                {
                    "status": "blocked",
                    "error": "history_reset_in_progress",
                    "message": "History reset is in progress or cannot be verified; do not retry or continue writing.",
                    "history_reset_in_progress": True,
                    "outcome_unknown": bool(reset.get("outcome_unknown")),
                    "phase": str(reset.get("phase") or ""),
                },
                status=409,
            )
            return True

        def _dispatch_POST(self, path: str, payload: dict[str, Any]) -> None:
                if path == "/api/driver-probe":
                    from app.personal_wechat_bot.control.send_commands import probe_send_controls

                    driver = str(payload.get("driver") or "").strip() or None
                    self._json(probe_send_controls(data_dir, driver=driver))
                    return
                if path == "/api/wechat-probe":
                    self._json(build_sidebar_wechat_probe(data_dir))
                    return
                if path == "/api/controls":
                    self._json(update_sidebar_controls(data_dir, payload))
                    return
                if path == "/api/backend-events":
                    self._json(append_sidebar_backend_event(data_dir, payload))
                    return
                if path == "/api/bridge/ack":
                    self._json(ack_sidebar_bridge_item(data_dir, payload))
                    return
                if path == "/api/bridge/retry":
                    self._json(retry_sidebar_bridge_item(data_dir, payload))
                    return
                if path == "/api/audit/clear":
                    self._json(clear_sidebar_send_audit(data_dir))
                    return
                if path == "/api/weflow/health":
                    self._json(sidebar_weflow_health(data_dir, payload))
                    return
                if path == "/api/weflow/pull-once":
                    self._json(sidebar_weflow_pull_once(data_dir, payload))
                    return
                if path == "/api/weflow/backfill":
                    self._json(sidebar_weflow_backfill(data_dir, payload))
                    return
                if path == "/api/weflow/cancel-backfill":
                    self._json(sidebar_weflow_cancel_backfill(data_dir, payload))
                    return
                if path == "/api/weflow/discover-sessions":
                    self._json(sidebar_weflow_discover_sessions(data_dir, payload))
                    return
                if path == "/api/weflow/clear-history":
                    self._json(sidebar_weflow_clear_history(data_dir, payload))
                    return
                if path == "/api/weflow/start":
                    self._json(sidebar_weflow_start(data_dir, payload))
                    return
                if path == "/api/weflow/stop":
                    self._json(sidebar_weflow_stop(data_dir, payload))
                    return
                if path == "/api/weflow/dependencies":
                    self._json(sidebar_weflow_dependency_status(data_dir))
                    return
                if path == "/api/weflow/install-deps":
                    self._json(sidebar_weflow_install_deps(data_dir, payload))
                    return
                if path == "/api/keys/add":
                    self._json(add_api_key(data_dir, payload))
                    return
                if path == "/api/keys/remove":
                    self._json(remove_api_key(data_dir, payload))
                    return
                if path == "/api/model-config":
                    self._json(set_model_config(data_dir, payload))
                    return
                if path == "/api/model-config/probe":
                    self._json(probe_model_fetch(data_dir, payload))
                    return
                if path == "/api/runtime/probe":
                    self._json(sidebar_runtime_probe(data_dir, payload))
                    return
                if path == "/api/resources/audit":
                    self._json(sidebar_resource_audit(data_dir, payload))
                    return
                if path == "/api/diagnostics/export":
                    self._json(sidebar_diagnostics_export(data_dir, payload))
                    return
                if path == "/api/storage/status":
                    self._json(sidebar_storage_migration_status(data_dir, payload))
                    return
                if path == "/api/native/migration-probe":
                    self._json(sidebar_native_migration_probe(data_dir, payload))
                    return
                if path == "/api/agent/tick":
                    self._json(sidebar_agent_tick(data_dir, payload))
                    return
                if path == "/api/agent/start":
                    self._json(sidebar_agent_start(data_dir, payload))
                    return
                if path == "/api/agent/stop":
                    self._json(sidebar_agent_stop(data_dir, payload))
                    return
                if path == "/api/tasks":
                    self._json(sidebar_task_action(data_dir, payload))
                    return
                if path == "/api/channel-state":
                    self._json(sidebar_channel_state_action(data_dir, payload))
                    return
                if path == "/api/workspace/cleanup":
                    self._json(cleanup_file_workspace(data_dir, payload))
                    return
                parts = [part for part in path.split("/") if part]
                if len(parts) == 3 and parts[:2] == ["api", "runtime-cards"]:
                    _, _, action = parts
                    self._json(sidebar_runtime_card_action(data_dir, action, payload))
                    return
                if path == "/api/channels/cleanup-hidden":
                    self._json(cleanup_sidebar_channels(data_dir, hidden_only=True))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "channels"] and parts[3] == "test-reply":
                    _, _, conversation_id, _ = parts
                    self._json(sidebar_channel_test_reply(data_dir, unquote(conversation_id), payload))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "channels"] and parts[3] == "test-file":
                    _, _, conversation_id, _ = parts
                    self._json(sidebar_channel_test_file(data_dir, unquote(conversation_id), payload))
                    return
                if len(parts) == 4 and parts[:3] == ["api", "channels", "delete"]:
                    _, _, _, conversation_id = parts
                    self._json(delete_sidebar_channel(data_dir, unquote(conversation_id)))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "queue"]:
                    _, _, queue_id, action = parts
                    self._json(sidebar_queue_action(data_dir, action, unquote(queue_id), payload))
                    return
                self._json({"status": "error", "error": "not_found"}, status=404)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _csrf_check(self) -> str:
            """Return an error string if a POST looks cross-origin, else "".

            Defends the mutating endpoints against a malicious page in the user's
            browser (the server binds loopback but any site can POST to it). Two
            layers:

            1. Content-Type must be JSON. A cross-site page can only send
               ``application/x-www-form-urlencoded`` / ``multipart/form-data`` /
               ``text/plain`` without triggering a CORS preflight; requiring JSON
               forces a preflight the browser will block for a disallowed origin.
            2. If an Origin/Referer header is present, its host:port must match
               our own Host header. A forged Origin cannot be set by browser JS.
            """
            content_type = str(self.headers.get("content-type", "")).split(";")[0].strip().lower()
            if content_type != "application/json":
                return "unsupported_content_type"
            host_error = self._host_check()
            if host_error:
                return host_error
            request_host, request_port = self._request_authority()
            origin = str(self.headers.get("origin", "")).strip()
            referer = str(self.headers.get("referer", "")).strip()
            source = origin or referer
            if not source:
                # No Origin/Referer: same-origin fetches may omit Origin, and a
                # non-browser client (curl) is not a CSRF vector. The JSON
                # content-type gate above already blocks the cross-site case.
                return ""
            try:
                parsed_source = urlparse(source)
                source_host = _normalize_hostname(parsed_source.hostname or "")
                source_port = parsed_source.port or (443 if parsed_source.scheme.lower() == "https" else 80)
            except ValueError:
                return "invalid_origin"
            if parsed_source.scheme.lower() not in {"http", "https"} or not source_host:
                return "invalid_origin"
            if source_host == request_host and source_port == request_port:
                return ""
            return "cross_origin_forbidden"

        def _host_check(self) -> str:
            try:
                request_host, request_port = self._request_authority()
            except ValueError:
                return "invalid_host"
            expected_port = int(self.server.server_address[1])
            if request_port != expected_port:
                return "untrusted_host"
            if _is_loopback_hostname(request_host):
                return ""
            allowed_hosts = {
                _normalize_hostname(str(self.server.server_address[0] or "")),
            }
            try:
                allowed_hosts.add(_normalize_hostname(str(self.connection.getsockname()[0] or "")))
            except OSError:
                pass
            return "" if request_host in allowed_hosts else "untrusted_host"

        def _request_authority(self) -> tuple[str, int]:
            raw_host = str(self.headers.get("host", "")).strip()
            if not raw_host:
                raise ValueError("missing host")
            parsed = urlparse(f"//{raw_host}")
            hostname = _normalize_hostname(parsed.hostname or "")
            if not hostname or parsed.username or parsed.password:
                raise ValueError("invalid host")
            port = parsed.port or 80
            return hostname, int(port)

        def _read_json(self) -> dict[str, Any]:
            raw_length = str(self.headers.get("content-length", "0") or "0").strip()
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise _SidebarRequestError(400, "invalid_content_length") from exc
            if length < 0:
                raise _SidebarRequestError(400, "invalid_content_length")
            if length <= 0:
                return {}
            if length > _MAX_POST_BODY_BYTES:
                raise _SidebarRequestError(413, "request_body_too_large")

            deadline = time.monotonic() + _POST_BODY_READ_TIMEOUT_SECONDS
            remaining = length
            chunks: list[bytes] = []
            connection = self.connection
            previous_timeout = connection.gettimeout()
            try:
                while remaining:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        raise _SidebarRequestError(408, "request_body_read_timeout")
                    connection.settimeout(timeout)
                    read = getattr(self.rfile, "read1", self.rfile.read)
                    try:
                        chunk = read(min(remaining, _POST_BODY_READ_CHUNK_BYTES))
                    except TimeoutError as exc:
                        raise _SidebarRequestError(408, "request_body_read_timeout") from exc
                    if not chunk:
                        raise _SidebarRequestError(400, "incomplete_request_body")
                    chunks.append(chunk)
                    remaining -= len(chunk)
            finally:
                connection.settimeout(previous_timeout)

            try:
                raw = b"".join(chunks).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _SidebarRequestError(400, "invalid_utf8") from exc
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise _SidebarRequestError(400, "invalid_json") from exc
            if not isinstance(payload, dict):
                raise _SidebarRequestError(400, "request_body_must_be_object")
            return payload

        def _json(self, payload: dict[str, Any], status: int = 200) -> None:
            deferred = getattr(self, "_deferred_json_responses", None)
            if isinstance(deferred, list):
                deferred.append((payload, int(status)))
                return
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self._send_response_bytes(
                body,
                status=status,
                content_type="application/json; charset=utf-8",
            )

        def _flush_deferred_json_response(self, deferred: list[tuple[dict[str, Any], int]]) -> None:
            if len(deferred) != 1:
                raise RuntimeError(f"API handler produced {len(deferred)} JSON responses")
            payload, status = deferred[0]
            self._json(payload, status=status)

        def _send_response_bytes(self, body: bytes, *, status: int, content_type: str) -> None:
            try:
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("cache-control", "no-store")
                self.send_header("content-security-policy", "frame-ancestors 'none'")
                self.send_header("x-frame-options", "DENY")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionError, TimeoutError):
                # Browsers can cancel a stale poll while headers or the body
                # are being flushed. The request is already abandoned.
                return

        def _static(self, raw_path: str) -> None:
            relative = "index.html" if raw_path in {"", "/"} else raw_path.lstrip("/")
            path = (STATIC_ROOT / relative).resolve()
            if STATIC_ROOT.resolve() not in path.parents and path != STATIC_ROOT.resolve():
                self._json({"status": "error", "error": "forbidden"}, status=403)
                return
            if not path.exists() or not path.is_file():
                self._json({"status": "error", "error": "not_found"}, status=404)
                return
            body = path.read_bytes()
            content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            self._send_response_bytes(body, status=200, content_type=content_type)

    return SidebarHandler


def _normalize_hostname(value: str) -> str:
    host = str(value or "").strip().lower().rstrip(".")
    if not host:
        return ""
    try:
        return ip_address(host).compressed.lower()
    except ValueError:
        return host


def _is_loopback_hostname(value: str) -> bool:
    host = _normalize_hostname(value)
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False

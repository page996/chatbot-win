from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from app.personal_wechat_bot.control.sidebar_api import (
    ack_sidebar_bridge_item,
    add_api_key,
    append_sidebar_backend_event,
    build_sidebar_bridge_state,
    build_sidebar_runtime_cards,
    build_sidebar_weflow_state,
    build_sidebar_wechat_probe,
    build_sidebar_state,
    clear_sidebar_send_audit,
    cleanup_sidebar_channels,
    cleanup_file_workspace,
    delete_sidebar_channel,
    get_model_config,
    list_api_keys,
    probe_model_fetch,
    remove_api_key,
    set_model_config,
    sidebar_queue_action,
    sidebar_runtime_card_action,
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


STATIC_ROOT = Path(__file__).resolve().parents[1] / "ui" / "sidebar"


def run_sidebar_server(data_dir: str | Path = "data", host: str = "127.0.0.1", port: int = 8765) -> None:
    handler = _handler_factory(Path(data_dir))
    server = ThreadingHTTPServer((host, port), handler)
    print(f"sidebar listening on http://{host}:{port}")
    server.serve_forever()


def _handler_factory(data_dir: Path) -> type[BaseHTTPRequestHandler]:
    class SidebarHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/state":
                self._json(build_sidebar_state(data_dir))
                return
            if parsed.path == "/api/driver-probe":
                query = parse_qs(parsed.query)
                from app.personal_wechat_bot.control.send_commands import probe_send_controls

                driver = query.get("driver", [None])[0]
                self._json(probe_send_controls(data_dir, driver=driver))
                return
            if parsed.path == "/api/wechat-probe":
                self._json(build_sidebar_wechat_probe(data_dir))
                return
            if parsed.path == "/api/bridge":
                self._json(build_sidebar_bridge_state(data_dir))
                return
            if parsed.path == "/api/runtime-cards":
                self._json(build_sidebar_runtime_cards(data_dir))
                return
            if parsed.path == "/api/weflow/status":
                self._json(build_sidebar_weflow_state(data_dir))
                return
            if parsed.path == "/api/keys":
                self._json(list_api_keys(data_dir))
                return
            if parsed.path == "/api/model-config":
                self._json(get_model_config(data_dir))
                return
            self._static(parsed.path)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            # CSRF / cross-origin guard. The sidebar UI is served from and calls
            # the same loopback origin, so a legitimate request either carries no
            # Origin (same-origin fetch in some browsers) or an Origin/Referer
            # whose host matches our own Host header. A cross-site page trying to
            # drive these mutating endpoints (add/remove keys, model-config,
            # probe -> key egress) fails this check and is rejected before any
            # handler — and before any API key is touched.
            csrf_error = self._csrf_check()
            if csrf_error:
                self._json({"status": "error", "error": csrf_error}, status=403)
                return
            try:
                payload = self._read_json()
                if parsed.path == "/api/controls":
                    self._json(update_sidebar_controls(data_dir, payload))
                    return
                if parsed.path == "/api/backend-events":
                    self._json(append_sidebar_backend_event(data_dir, payload))
                    return
                if parsed.path == "/api/bridge/ack":
                    self._json(ack_sidebar_bridge_item(data_dir, payload))
                    return
                if parsed.path == "/api/audit/clear":
                    self._json(clear_sidebar_send_audit(data_dir))
                    return
                if parsed.path == "/api/weflow/health":
                    self._json(sidebar_weflow_health(data_dir, payload))
                    return
                if parsed.path == "/api/weflow/pull-once":
                    self._json(sidebar_weflow_pull_once(data_dir, payload))
                    return
                if parsed.path == "/api/weflow/backfill":
                    self._json(sidebar_weflow_backfill(data_dir, payload))
                    return
                if parsed.path == "/api/weflow/cancel-backfill":
                    self._json(sidebar_weflow_cancel_backfill(data_dir, payload))
                    return
                if parsed.path == "/api/weflow/discover-sessions":
                    self._json(sidebar_weflow_discover_sessions(data_dir, payload))
                    return
                if parsed.path == "/api/weflow/clear-history":
                    self._json(sidebar_weflow_clear_history(data_dir, payload))
                    return
                if parsed.path == "/api/weflow/start":
                    self._json(sidebar_weflow_start(data_dir, payload))
                    return
                if parsed.path == "/api/weflow/stop":
                    self._json(sidebar_weflow_stop(data_dir, payload))
                    return
                if parsed.path == "/api/weflow/dependencies":
                    self._json(sidebar_weflow_dependency_status(data_dir))
                    return
                if parsed.path == "/api/weflow/install-deps":
                    self._json(sidebar_weflow_install_deps(data_dir, payload))
                    return
                if parsed.path == "/api/keys/add":
                    self._json(add_api_key(data_dir, payload))
                    return
                if parsed.path == "/api/keys/remove":
                    self._json(remove_api_key(data_dir, payload))
                    return
                if parsed.path == "/api/model-config":
                    self._json(set_model_config(data_dir, payload))
                    return
                if parsed.path == "/api/model-config/probe":
                    self._json(probe_model_fetch(data_dir, payload))
                    return
                if parsed.path == "/api/workspace/cleanup":
                    self._json(cleanup_file_workspace(data_dir, payload))
                    return
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) == 3 and parts[:2] == ["api", "runtime-cards"]:
                    _, _, action = parts
                    self._json(sidebar_runtime_card_action(data_dir, action, payload))
                    return
                if parsed.path == "/api/channels/cleanup-hidden":
                    self._json(cleanup_sidebar_channels(data_dir, hidden_only=True))
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
            except Exception as exc:
                self._json({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, status=400)

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
            host = str(self.headers.get("host", "")).strip().lower()
            origin = str(self.headers.get("origin", "")).strip()
            referer = str(self.headers.get("referer", "")).strip()
            source = origin or referer
            if not source:
                # No Origin/Referer: same-origin fetches may omit Origin, and a
                # non-browser client (curl) is not a CSRF vector. The JSON
                # content-type gate above already blocks the cross-site case.
                return ""
            try:
                source_host = urlparse(source).netloc.strip().lower()
            except ValueError:
                return "invalid_origin"
            if not source_host:
                return "invalid_origin"
            # Compare host:port. If our Host header lacks a port (proxied), fall
            # back to comparing hostname only.
            if source_host == host:
                return ""
            if host and ":" not in host and source_host.split(":")[0] == host:
                return ""
            return "cross_origin_forbidden"

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("content-length", "0"))
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
            self.send_response(200)
            self.send_header("content-type", content_type)
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return SidebarHandler

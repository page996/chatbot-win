from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from app.personal_wechat_bot.control.sidebar_api import (
    build_sidebar_wechat_probe,
    build_sidebar_state,
    sidebar_queue_action,
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
            self._static(parsed.path)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                payload = self._read_json()
                if parsed.path == "/api/controls":
                    self._json(update_sidebar_controls(data_dir, payload))
                    return
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) == 4 and parts[:2] == ["api", "queue"]:
                    _, _, queue_id, action = parts
                    self._json(sidebar_queue_action(data_dir, action, unquote(queue_id), payload))
                    return
                self._json({"status": "error", "error": "not_found"}, status=404)
            except Exception as exc:
                self._json({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, status=400)

        def log_message(self, format: str, *args: Any) -> None:
            return

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

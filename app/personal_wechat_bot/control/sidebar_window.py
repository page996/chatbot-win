from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.control.sidebar_server import _handler_factory
from app.personal_wechat_bot.wechat_driver.windows_readonly import Win32WindowProbe


DEFAULT_WINDOW_WIDTH = 430
DEFAULT_WINDOW_HEIGHT = 760


@dataclass(frozen=True)
class SidebarLaunchResult:
    status: str
    url: str
    host: str
    port: int
    browser: str
    pid: int | None
    geometry: dict[str, int]
    note: str = ""


def run_sidebar_window(data_dir: str | Path = "data", *, poll_interval_ms: int = 2000) -> None:
    _ = poll_interval_ms
    result = launch_sidebar_window(data_dir)
    print(result_as_json(result), flush=True)
    if result.status == "opened_external_browser":
        return
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return


def launch_sidebar_window(
    data_dir: str | Path = "data",
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    width: int = DEFAULT_WINDOW_WIDTH,
    height: int = DEFAULT_WINDOW_HEIGHT,
) -> SidebarLaunchResult:
    server = ThreadingHTTPServer((host, _available_port(host, port)), _handler_factory(Path(data_dir)))
    actual_host, actual_port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://{actual_host}:{actual_port}/?window=1"
    geometry = _sidebar_geometry(width=width, height=height)
    browser, pid = _open_app_window(url, geometry=geometry)
    status = "ok" if pid is not None else "opened_external_browser"
    note = "" if pid is not None else "No supported app-mode browser found; opened the default browser."
    return SidebarLaunchResult(
        status=status,
        url=url,
        host=str(actual_host),
        port=int(actual_port),
        browser=browser,
        pid=pid,
        geometry=geometry,
        note=note,
    )


def result_as_json(result: SidebarLaunchResult) -> str:
    import json

    return json.dumps(asdict(result), ensure_ascii=False, indent=2)


def _available_port(host: str, requested: int) -> int:
    if requested:
        return requested
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _sidebar_geometry(*, width: int, height: int) -> dict[str, int]:
    target = _wechat_anchor()
    if target:
        return {
            "x": max(0, int(target.get("right", 0)) + 8),
            "y": max(0, int(target.get("top", 80))),
            "width": width,
            "height": height,
        }
    return {"x": 80, "y": 80, "width": width, "height": height}


def _wechat_anchor() -> dict[str, int] | None:
    windows = Win32WindowProbe(include_invisible=False).find_wechat_windows()
    if not windows:
        return None
    window = windows[0]
    return {"left": window.left, "top": window.top, "right": window.right, "bottom": window.bottom}


def _open_app_window(url: str, *, geometry: dict[str, int]) -> tuple[str, int | None]:
    if sys.platform != "win32":
        webbrowser.open(url)
        return "default", None
    for browser in _browser_candidates():
        try:
            process = subprocess.Popen(
                [
                    str(browser),
                    f"--app={url}",
                    f"--window-position={geometry['x']},{geometry['y']}",
                    f"--window-size={geometry['width']},{geometry['height']}",
                    "--new-window",
                    "--disable-features=Translate",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            return str(browser), int(process.pid)
        except OSError:
            continue
    webbrowser.open(url)
    return "default", None


def _browser_candidates() -> list[Path]:
    roots = [
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    ]
    local = Path.home() / "AppData" / "Local"
    roots.extend(
        [
            local / "Google/Chrome/Application/chrome.exe",
            local / "Microsoft/Edge/Application/msedge.exe",
        ]
    )
    return [path for path in roots if path.exists()]


def flatten_queue_items(state: dict[str, Any], statuses: tuple[str, ...] = ("pending", "approved", "failed")) -> list[dict[str, Any]]:
    queues = state.get("queues", {})
    if not isinstance(queues, dict):
        return []
    items: list[dict[str, Any]] = []
    for status in statuses:
        queue = queues.get(status, {})
        raw_items = queue.get("items", []) if isinstance(queue, dict) else []
        for item in raw_items if isinstance(raw_items, list) else []:
            if isinstance(item, dict):
                copied = dict(item)
                copied.setdefault("status", status)
                items.append(copied)
    return items


def queue_counts(state: dict[str, Any], statuses: tuple[str, ...] = ("pending", "approved", "failed", "rejected", "sent")) -> dict[str, int]:
    queues = state.get("queues", {})
    counts: dict[str, int] = {}
    for status in statuses:
        queue = queues.get(status, {}) if isinstance(queues, dict) else {}
        if isinstance(queue, dict) and isinstance(queue.get("count"), int):
            counts[status] = int(queue["count"])
        else:
            counts[status] = len(queue.get("items", [])) if isinstance(queue, dict) else 0
    return counts

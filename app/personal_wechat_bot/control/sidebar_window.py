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
from app.personal_wechat_bot.wechat_driver.window_introspection import filter_wechat_chat_windows
from app.personal_wechat_bot.wechat_driver.windows_readonly import Win32WindowProbe


DEFAULT_WINDOW_WIDTH = 430
DEFAULT_WINDOW_HEIGHT = 760
WINDOW_GAP = 8
SIDEBAR_TITLE = "微信 Agent 审计面板"
SIDEBAR_TITLE_TOKENS = (SIDEBAR_TITLE, "WeChat Agent Console", "Agent Console")
BROWSER_PROCESS_NAMES = {"chrome.exe", "msedge.exe"}


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
    result = launch_sidebar_window(data_dir)
    print(result_as_json(result), flush=True)
    if result.status == "opened_external_browser":
        return
    if result.pid is not None:
        tracker = threading.Thread(
            target=_track_sidebar_window,
            args=(result.pid, poll_interval_ms, result.geometry["width"], result.geometry["height"], Path(data_dir)),
            daemon=True,
        )
        tracker.start()
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
    url = f"http://{actual_host}:{actual_port}/?window=1&launch={int(time.time())}"
    geometry = _sidebar_geometry(width=width, height=height, data_dir=data_dir)
    profile_dir = (Path(data_dir) / "runtime" / "sidebar_browser_profile").resolve()
    _close_existing_sidebar_windows()
    browser, pid = _open_app_window(url, geometry=geometry, profile_dir=profile_dir)
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


def _sidebar_geometry(*, width: int, height: int, data_dir: str | Path | None = None) -> dict[str, int]:
    target = _wechat_anchor(data_dir=data_dir)
    if target:
        return _geometry_next_to_anchor(target, width=width, height=height)
    return {"x": 80, "y": 80, "width": width, "height": height}


def _wechat_anchor(data_dir: str | Path | None = None) -> dict[str, int] | None:
    windows = filter_wechat_chat_windows(Win32WindowProbe(include_invisible=False).find_wechat_windows())
    if not windows:
        return None
    window = windows[0]
    return {"left": window.left, "top": window.top, "right": window.right, "bottom": window.bottom}


def _geometry_next_to_anchor(anchor: dict[str, int], *, width: int, height: int) -> dict[str, int]:
    work = _work_area()
    left = int(anchor.get("left", 0))
    top = int(anchor.get("top", 80))
    right = int(anchor.get("right", left + width))
    bottom = int(anchor.get("bottom", top + height))
    usable_height = max(360, min(height, work["bottom"] - work["top"]))
    y = _clamp(top, work["top"], max(work["top"], work["bottom"] - usable_height))
    outside_right = right + WINDOW_GAP
    if outside_right + width <= work["right"]:
        x = outside_right
    else:
        inside_right = right - width - WINDOW_GAP
        x = _clamp(inside_right, work["left"], max(work["left"], work["right"] - width))
    return {"x": x, "y": y, "width": width, "height": usable_height}


def _open_app_window(url: str, *, geometry: dict[str, int], profile_dir: Path | None = None) -> tuple[str, int | None]:
    if sys.platform != "win32":
        webbrowser.open(url)
        return "default", None
    if profile_dir is not None:
        profile_dir.mkdir(parents=True, exist_ok=True)
    for browser in _browser_candidates():
        command = [
            str(browser),
            f"--app={url}",
            f"--window-position={geometry['x']},{geometry['y']}",
            f"--window-size={geometry['width']},{geometry['height']}",
            "--new-window",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
        ]
        if profile_dir is not None:
            command.append(f"--user-data-dir={profile_dir}")
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            return str(browser), int(process.pid)
        except OSError:
            continue
    webbrowser.open(url)
    return "default", None


def _track_sidebar_window(pid: int, poll_interval_ms: int, width: int, height: int, data_dir: Path) -> None:
    if sys.platform != "win32" or pid <= 0:
        return
    interval = max(0.25, poll_interval_ms / 1000)
    last_geometry: dict[str, int] | None = None
    misses = 0
    while True:
        hwnd = _find_sidebar_window(pid)
        if hwnd is None:
            misses += 1
            if misses * interval >= 30:
                return
            time.sleep(interval)
            continue
        misses = 0
        geometry = _sidebar_geometry(width=width, height=height, data_dir=data_dir)
        if geometry != last_geometry:
            _move_window(hwnd, geometry)
            last_geometry = geometry
        time.sleep(interval)


def _window_for_pid(pid: int) -> int | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        if found:
            return False
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if (
            int(window_pid.value) == pid
            and user32.IsWindowVisible(hwnd)
            and _title_matches_sidebar(_window_title(hwnd))
        ):
            found.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(enum_proc, 0)
    return found[0] if found else None


def _find_sidebar_window(preferred_pid: int) -> int | None:
    by_pid = _window_for_pid(preferred_pid)
    if by_pid is not None:
        return by_pid
    return _window_for_sidebar_title()


def _window_for_sidebar_title() -> int | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, int]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not _title_matches_sidebar(title):
            return True
        process_name = _process_name_for_hwnd(hwnd)
        if process_name and process_name.lower() not in BROWSER_PROCESS_NAMES:
            return True
        found.append((_sidebar_title_priority(title), int(hwnd)))
        return True

    user32.EnumWindows(enum_proc, 0)
    return sorted(found, key=lambda item: item[0])[0][1] if found else None


def _close_existing_sidebar_windows() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    user32 = ctypes.windll.user32
    wm_close = 0x0010

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_proc(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not _title_matches_sidebar(title):
            return True
        process_name = _process_name_for_hwnd(hwnd)
        if process_name and process_name.lower() not in BROWSER_PROCESS_NAMES:
            return True
        user32.PostMessageW(hwnd, wm_close, 0, 0)
        return True

    user32.EnumWindows(enum_proc, 0)
    time.sleep(0.5)


def _title_matches_sidebar(title: str) -> bool:
    normalized = str(title or "").strip()
    if not normalized:
        return False
    return any(token in normalized for token in SIDEBAR_TITLE_TOKENS)


def _sidebar_title_priority(title: str) -> int:
    if SIDEBAR_TITLE in str(title or ""):
        return 0
    return 10


def _window_title(hwnd: int) -> str:
    if sys.platform != "win32":
        return ""
    import ctypes

    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _process_name_for_hwnd(hwnd: int) -> str:
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if not process_id.value:
        return ""
    query_limited_information = 0x1000
    vm_read = 0x0010
    handle = kernel32.OpenProcess(query_limited_information | vm_read, False, process_id.value)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(260)
        if psapi.GetModuleBaseNameW(handle, None, buffer, len(buffer)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _move_window(hwnd: int, geometry: dict[str, int]) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    user32 = ctypes.windll.user32
    swp_no_zorder = 0x0004
    swp_no_activate = 0x0010
    return bool(
        user32.SetWindowPos(
            hwnd,
            0,
            int(geometry["x"]),
            int(geometry["y"]),
            int(geometry["width"]),
            int(geometry["height"]),
            swp_no_zorder | swp_no_activate,
        )
    )


def _work_area() -> dict[str, int]:
    if sys.platform != "win32":
        return {"left": 0, "top": 0, "right": 1920, "bottom": 1080}
    import ctypes
    from ctypes import wintypes

    rect = wintypes.RECT()
    spi_getworkarea = 0x0030
    if ctypes.windll.user32.SystemParametersInfoW(spi_getworkarea, 0, ctypes.byref(rect), 0):
        return {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom}
    return {"left": 0, "top": 0, "right": 1920, "bottom": 1080}


def _clamp(value: int, low: int, high: int) -> int:
    if high < low:
        return low
    return max(low, min(value, high))


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


def flatten_queue_items(
    state: dict[str, Any],
    statuses: tuple[str, ...] = ("pending", "approved", "queued_to_bridge", "failed"),
) -> list[dict[str, Any]]:
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


def queue_counts(
    state: dict[str, Any],
    statuses: tuple[str, ...] = ("pending", "approved", "queued_to_bridge", "failed", "rejected", "sent"),
) -> dict[str, int]:
    queues = state.get("queues", {})
    counts: dict[str, int] = {}
    for status in statuses:
        queue = queues.get(status, {}) if isinstance(queues, dict) else {}
        if isinstance(queue, dict) and isinstance(queue.get("count"), int):
            counts[status] = int(queue["count"])
        else:
            counts[status] = len(queue.get("items", [])) if isinstance(queue, dict) else 0
    return counts

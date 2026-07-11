from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field, fields
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from app.personal_wechat_bot.control.sidebar_server import _handler_factory
from app.personal_wechat_bot.control.sidebar_browser_runtime import (
    inspect_sidebar_browser_runtime,
    sidebar_browser_profile_path,
)
from app.personal_wechat_bot.runtime.history_fence import history_writer_lease_if_owned
from app.personal_wechat_bot.runtime.process_lock import process_start_marker
from app.personal_wechat_bot.runtime.windows_job_process import start_windows_job_process
from app.personal_wechat_bot.wechat_driver.window_introspection import filter_wechat_chat_windows
from app.personal_wechat_bot.wechat_driver.windows_readonly import Win32WindowProbe


DEFAULT_WINDOW_WIDTH = 430
DEFAULT_WINDOW_HEIGHT = 760
WINDOW_GAP = 8
SIDEBAR_TITLE = "微信 Agent 审计面板"
SIDEBAR_TITLE_TOKENS = (SIDEBAR_TITLE, "WeChat Agent Console", "Agent Console")
WINDOW_MISSING_GRACE_SECONDS = 30.0


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
    browser_process_start: str = ""
    browser_executable: str = ""
    browser_profile: str = ""
    owned_browser: bool = False
    browser_job_owned: bool = False
    _server: ThreadingHTTPServer | None = field(default=None, repr=False, compare=False)
    _server_thread: threading.Thread | None = field(default=None, repr=False, compare=False)
    _browser_process: Any = field(default=None, repr=False, compare=False)


def run_sidebar_window(
    data_dir: str | Path = "data",
    *,
    poll_interval_ms: int = 2000,
    browser_state_callback: Callable[[dict[str, Any]], None] | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    root = Path(data_dir).resolve()
    profile_dir = sidebar_browser_profile_path(root)
    stopped = stop_event or threading.Event()
    with history_writer_lease_if_owned(
        root,
        label="sidebar_browser_lifecycle",
        metadata={"browser_profile": str(profile_dir)},
    ):
        result = launch_sidebar_window(
            root,
            browser_state_callback=browser_state_callback,
        )
        print(result_as_json(result), flush=True)
        tracker: threading.Thread | None = None
        if result.pid is not None:
            tracker = threading.Thread(
                target=_track_sidebar_window,
                args=(
                    result.pid,
                    poll_interval_ms,
                    result.geometry["width"],
                    result.geometry["height"],
                    root,
                    stopped,
                ),
                daemon=True,
            )
            tracker.start()
        try:
            while not stopped.wait(0.25):
                process = result._browser_process
                if result.owned_browser and process is not None and process.poll() is not None:
                    stopped.set()
        except KeyboardInterrupt:
            stopped.set()
        finally:
            stopped.set()
            _shutdown_sidebar_launch(result)
            if tracker is not None:
                tracker.join(timeout=max(1.0, poll_interval_ms / 1000 + 0.5))


def launch_sidebar_window(
    data_dir: str | Path = "data",
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    width: int = DEFAULT_WINDOW_WIDTH,
    height: int = DEFAULT_WINDOW_HEIGHT,
    browser_state_callback: Callable[[dict[str, Any]], None] | None = None,
) -> SidebarLaunchResult:
    server = ThreadingHTTPServer((host, _available_port(host, port)), _handler_factory(Path(data_dir)))
    actual_host, actual_port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process: Any = None
    try:
        url = f"http://{actual_host}:{actual_port}/?window=1&launch={int(time.time())}"
        geometry = _sidebar_geometry(width=width, height=height, data_dir=data_dir)
        profile_dir = sidebar_browser_profile_path(data_dir)
        existing_browser = inspect_sidebar_browser_runtime(data_dir)
        if not bool(existing_browser.get("inventory_verified")) or not bool(existing_browser.get("verified")):
            raise RuntimeError("sidebar browser profile preflight is unsafe or unavailable")
        if existing_browser.get("blockers") or existing_browser.get("browser_process_tree"):
            raise RuntimeError("sidebar browser profile is already active")
        browser, process = _open_app_window(url, geometry=geometry, profile_dir=profile_dir)
        pid = int(process.pid) if process is not None else None
        process_start = process_start_marker(pid) if pid is not None else ""
        if process is not None and not process_start:
            _terminate_browser_process(process)
            raise RuntimeError("sidebar browser process identity is unavailable")
        status = "ok" if pid is not None else "opened_external_browser"
        note = "" if pid is not None else "No supported app-mode browser found; opened the default browser."
        result = SidebarLaunchResult(
            status=status,
            url=url,
            host=str(actual_host),
            port=int(actual_port),
            browser=browser,
            pid=pid,
            geometry=geometry,
            note=note,
            browser_process_start=process_start,
            browser_executable=str(Path(browser).resolve()) if process is not None else "",
            browser_profile=str(profile_dir),
            owned_browser=process is not None,
            browser_job_owned=process is not None,
            _server=server,
            _server_thread=thread,
            _browser_process=process,
        )
        if browser_state_callback is not None:
            browser_state_callback(_browser_state_payload(result))
        return result
    except BaseException:
        if process is not None:
            _terminate_browser_process(process)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3.0)
        raise


def result_as_json(result: SidebarLaunchResult) -> str:
    import json

    payload = {
        item.name: getattr(result, item.name)
        for item in fields(result)
        if not item.name.startswith("_")
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


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


def _open_app_window(
    url: str,
    *,
    geometry: dict[str, int],
    profile_dir: Path | None = None,
) -> tuple[str, Any]:
    if sys.platform != "win32":
        webbrowser.open(url)
        return "default", None
    if profile_dir is not None:
        _prepare_private_browser_profile(profile_dir)
    for browser in _browser_candidates():
        command = [
            str(browser),
            f"--app={url}",
            f"--window-position={geometry['x']},{geometry['y']}",
            f"--window-size={geometry['width']},{geometry['height']}",
            "--new-window",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--disable-features=Translate",
        ]
        if profile_dir is not None:
            command.append(f"--user-data-dir={profile_dir}")
        try:
            process = start_windows_job_process(command)
            return str(browser.resolve()), process
        except OSError:
            continue
    webbrowser.open(url)
    return "default", None


def _prepare_private_browser_profile(profile_dir: Path) -> None:
    profile_dir = Path(os.path.abspath(profile_dir))
    runtime_dir = profile_dir.parent
    runtime_stat = _safe_lstat(runtime_dir)
    if runtime_stat is None:
        runtime_dir.mkdir(parents=False, exist_ok=False)
        runtime_stat = _safe_lstat(runtime_dir)
    if runtime_stat is None or _is_reparse_point(runtime_stat) or not stat.S_ISDIR(runtime_stat.st_mode):
        raise RuntimeError("unsafe sidebar browser runtime directory")
    profile_stat = _safe_lstat(profile_dir)
    if profile_stat is None:
        profile_dir.mkdir(parents=False, exist_ok=False)
        profile_stat = _safe_lstat(profile_dir)
    if profile_stat is None or _is_reparse_point(profile_stat) or not stat.S_ISDIR(profile_stat.st_mode):
        raise RuntimeError("unsafe sidebar browser profile directory")
    runtime_recheck = _safe_lstat(runtime_dir)
    if runtime_recheck is None or _is_reparse_point(runtime_recheck) or not stat.S_ISDIR(runtime_recheck.st_mode):
        raise RuntimeError("unsafe sidebar browser runtime directory")


def _safe_lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_reparse_point(path_stat: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(path_stat.st_mode)
        or int(getattr(path_stat, "st_file_attributes", 0) or 0)
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    )


def _track_sidebar_window(
    pid: int,
    poll_interval_ms: int,
    width: int,
    height: int,
    data_dir: Path,
    stop_event: threading.Event,
) -> None:
    if sys.platform != "win32" or pid <= 0:
        return
    interval = max(0.25, poll_interval_ms / 1000)
    last_geometry: dict[str, int] | None = None
    misses = 0
    while not stop_event.is_set():
        hwnd = _find_sidebar_window(pid)
        if hwnd is None:
            misses += 1
            if misses * interval >= WINDOW_MISSING_GRACE_SECONDS:
                stop_event.set()
                return
            stop_event.wait(interval)
            continue
        misses = 0
        geometry = _sidebar_geometry(width=width, height=height, data_dir=data_dir)
        if geometry != last_geometry:
            _move_window(hwnd, geometry)
            last_geometry = geometry
        stop_event.wait(interval)


def _browser_state_payload(result: SidebarLaunchResult) -> dict[str, Any]:
    descendants: list[dict[str, Any]] = []
    if result.owned_browser:
        runtime = inspect_sidebar_browser_runtime(Path(result.browser_profile).parents[1])
        if not bool(runtime.get("inventory_verified")):
            raise RuntimeError("sidebar browser descendant inventory is unavailable")
        tree = [item for item in runtime.get("browser_process_tree", []) if isinstance(item, dict)]
        if not any(int(item.get("pid") or 0) == int(result.pid or 0) for item in tree):
            raise RuntimeError("sidebar browser root is missing from descendant inventory")
        if any(not bool(item.get("identity_verified")) for item in tree):
            raise RuntimeError("sidebar browser descendant identity is unavailable")
        descendants = [
            {
                "pid": int(item.get("pid") or 0),
                "parent_pid": int(item.get("parent_pid") or 0),
                "process_start": str(item.get("process_start") or ""),
                "executable": str(item.get("image") or ""),
                "root_pid": int(item.get("root_pid") or 0),
            }
            for item in tree
        ]
    return {
        "browser_status": result.status,
        "browser_pid": result.pid,
        "browser_process_start": result.browser_process_start,
        "browser_executable": result.browser_executable,
        "browser_profile": result.browser_profile,
        "browser_owned": result.owned_browser,
        "browser_job_owned": result.browser_job_owned,
        "browser_descendants": descendants,
        "browser_url": result.url,
        "browser_state_updated_at_epoch": time.time(),
    }


def _shutdown_sidebar_launch(result: SidebarLaunchResult) -> None:
    deferred: BaseException | None = None
    process = result._browser_process
    if result.owned_browser and process is not None:
        try:
            hwnd = _find_sidebar_window(int(result.pid or 0))
            if hwnd is not None:
                _post_close_window(hwnd)
            _wait_or_terminate_browser_process(process)
            _wait_for_browser_runtime_shutdown(Path(result.browser_profile).parents[1])
        except BaseException as exc:
            deferred = exc
    server = result._server
    if server is not None:
        try:
            server.shutdown()
        except BaseException as exc:
            if deferred is None:
                deferred = exc
        try:
            server.server_close()
        except BaseException as exc:
            if deferred is None:
                deferred = exc
    thread = result._server_thread
    if thread is not None:
        thread.join(timeout=3.0)
        if thread.is_alive() and deferred is None:
            deferred = RuntimeError("sidebar HTTP server did not stop")
    if deferred is not None:
        raise deferred


def _wait_or_terminate_browser_process(process: Any) -> None:
    try:
        process.wait(timeout=3.0)
        _close_browser_process_handles(process)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    _terminate_browser_process(process)


def _terminate_browser_process(process: Any) -> None:
    if process.poll() is not None:
        try:
            process.wait(timeout=0.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
        _close_browser_process_handles(process)
        return
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=3.0)
        _close_browser_process_handles(process)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=3.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _close_browser_process_handles(process)
        raise RuntimeError("sidebar browser process did not stop") from exc
    _close_browser_process_handles(process)


def _close_browser_process_handles(process: Any) -> None:
    close = getattr(process, "close", None)
    if callable(close):
        close()


def _wait_for_browser_runtime_shutdown(data_dir: Path) -> None:
    last_runtime: dict[str, Any] = {}
    for attempt in range(5):
        if attempt:
            time.sleep(0.25)
        last_runtime = inspect_sidebar_browser_runtime(data_dir)
        if not bool(last_runtime.get("inventory_verified")):
            raise RuntimeError("sidebar browser shutdown inventory is unavailable")
        if not last_runtime.get("browser_process_tree") and not last_runtime.get("profile_processes"):
            return
    raise RuntimeError(
        "sidebar browser processes remain after shutdown: "
        f"{[int(item.get('pid') or 0) for item in last_runtime.get('browser_process_tree', []) if isinstance(item, dict)]}"
    )


def _post_close_window(hwnd: int) -> None:
    if sys.platform != "win32":
        return
    import ctypes

    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)


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
    return _window_for_pid(preferred_pid)


def _close_existing_sidebar_windows(browser_pids: tuple[int, ...] = ()) -> None:
    if sys.platform != "win32":
        return
    closed = False
    for pid in sorted({int(value) for value in browser_pids if int(value) > 0}):
        hwnd = _window_for_pid(pid)
        if hwnd is None:
            continue
        _post_close_window(hwnd)
        closed = True
    if closed:
        time.sleep(0.5)


def _title_matches_sidebar(title: str) -> bool:
    normalized = str(title or "").strip()
    if not normalized:
        return False
    return any(token in normalized for token in SIDEBAR_TITLE_TOKENS)


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
    statuses: tuple[str, ...] = ("pending", "approved", "queued_to_bridge", "accepted", "failed", "rejected", "sent"),
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

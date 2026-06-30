from __future__ import annotations

import ctypes
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ctypes import wintypes

from app.personal_wechat_bot.wechat_driver.windows_readonly import (
    Win32WindowProbe,
    WindowInfo,
    foreground_window_info,
)


VENDOR_UI_PATH = Path(__file__).resolve().parents[3] / "vendor" / "windows-ui"
WECHAT_PROCESS_NAMES = {"wechat.exe", "weixin.exe", "wechatappex.exe"}
KNOWN_WECHAT_CLASS_TOKENS = (
    "wechat",
    "weixin",
    "mmuisdk",
)
REJECTED_PROCESS_NAMES = {
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "powershell.exe",
    "windowsterminal.exe",
    "cmd.exe",
    "python.exe",
    "pythonw.exe",
}
REJECTED_CLASS_TOKENS = (
    "chrome_widgetwin",
    "consolewindowclass",
    "cabinetwclass",
    "applicationframewindow",
    "notepad",
)
WECHAT_TITLE_TOKENS = ("微信", "wechat", "寰俊")
IGNORED_WINDOW_TOKENS = (
    "tray",
    "tooltip",
    "popup",
    "notification",
    "wxtrayiconmessagewindow",
    "wechat agent",
    "send queue",
)


@dataclass(frozen=True)
class ChildWindowInfo:
    hwnd: int
    title: str
    class_name: str
    width: int
    height: int
    left: int
    top: int
    right: int
    bottom: int
    visible: bool


@dataclass(frozen=True)
class AutomationControlInfo:
    name: str
    control_type: str
    automation_id: str
    class_name: str
    bounding_rect: dict[str, int]
    depth: int
    looks_like_chat: bool = False
    looks_like_input: bool = False


def build_wechat_window_probe(
    *,
    max_children: int = 200,
    max_controls: int = 300,
    max_depth: int = 8,
) -> dict[str, Any]:
    raw_windows = Win32WindowProbe(include_invisible=False).find_wechat_windows()
    windows = filter_wechat_chat_windows(raw_windows)
    foreground = foreground_window_info()
    targets = [_window_payload(item, max_children=max_children, max_controls=max_controls, max_depth=max_depth) for item in windows]
    active = _active_target(targets, foreground)
    dependency = _uia_dependency_status()
    return {
        "status": "ok" if windows else "not_found",
        "strategy": "win32_hwnd_plus_ui_automation",
        "developer_tools_note": (
            "WeChat DevTools targets Mini Program/WebView debugging; native PC WeChat chat HWND/control "
            "discovery uses Win32 window handles plus UI Automation instead."
        ),
        "foreground": foreground,
        "active": active,
        "windows": targets,
        "ignored_windows": [asdict(item) for item in raw_windows if item not in windows],
        "ui_automation": dependency,
    }


def _window_payload(window: WindowInfo, *, max_children: int, max_controls: int, max_depth: int) -> dict[str, Any]:
    children = _child_windows(window.hwnd, max_children=max_children)
    controls = _automation_controls(window.hwnd, max_controls=max_controls, max_depth=max_depth)
    return {
        **asdict(window),
        "children": [asdict(item) for item in children],
        "child_count": len(children),
        "automation_controls": [asdict(item) for item in controls],
        "automation_control_count": len(controls),
        "chat_candidates": [asdict(item) for item in controls if item.looks_like_chat or item.looks_like_input][:20],
    }


def _active_target(targets: list[dict[str, Any]], foreground: dict[str, Any]) -> dict[str, Any]:
    foreground_hwnd = int(foreground.get("hwnd", 0) or 0)
    for target in targets:
        if int(target.get("hwnd", 0) or 0) == foreground_hwnd:
            return {"status": "matched_foreground", "hwnd": foreground_hwnd, "title": target.get("title", "")}
    process = str(foreground.get("process_name", "")).lower()
    if process in {"wechat.exe", "weixin.exe", "wechatappex.exe"}:
        return {"status": "foreground_wechat_child_or_popup", "hwnd": foreground_hwnd, "title": foreground.get("title", "")}
    return {"status": "not_wechat_foreground", "hwnd": foreground_hwnd, "title": foreground.get("title", "")}


def _is_candidate_chat_window(window: WindowInfo) -> bool:
    if not getattr(window, "visible", True):
        return False
    if window.width < 500 or window.height < 300:
        return False
    if window.left <= -10000 or window.top <= -10000:
        return False
    title_and_class = f"{window.title} {window.class_name}".lower()
    if any(token in title_and_class for token in IGNORED_WINDOW_TOKENS):
        return False
    if any(token in title_and_class for token in REJECTED_CLASS_TOKENS):
        return False
    process = Path(window.process_name).name.lower()
    if process in REJECTED_PROCESS_NAMES:
        return False
    if process and process not in WECHAT_PROCESS_NAMES:
        return False
    if process in WECHAT_PROCESS_NAMES:
        return True
    if any(token in title_and_class for token in KNOWN_WECHAT_CLASS_TOKENS):
        return True
    title = window.title.lower()
    if any(token in window.title or token in title for token in WECHAT_TITLE_TOKENS):
        return True
    return False


def filter_wechat_chat_windows(windows: list[WindowInfo]) -> list[WindowInfo]:
    return [item for item in windows if _is_candidate_chat_window(item)]


def _child_windows(hwnd: int, *, max_children: int) -> list[ChildWindowInfo]:
    if sys.platform != "win32" or not hwnd:
        return []
    user32 = ctypes.windll.user32
    children: list[ChildWindowInfo] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_proc(child_hwnd: int, _lparam: int) -> bool:
        if len(children) >= max_children:
            return False
        rect = _window_rect(child_hwnd)
        children.append(
            ChildWindowInfo(
                hwnd=int(child_hwnd),
                title=_window_title(child_hwnd),
                class_name=_window_class_name(child_hwnd),
                width=rect.right - rect.left,
                height=rect.bottom - rect.top,
                left=rect.left,
                top=rect.top,
                right=rect.right,
                bottom=rect.bottom,
                visible=bool(user32.IsWindowVisible(child_hwnd)),
            )
        )
        return True

    user32.EnumChildWindows(hwnd, enum_proc, 0)
    return children


def _automation_controls(hwnd: int, *, max_controls: int, max_depth: int) -> list[AutomationControlInfo]:
    if sys.platform != "win32" or not hwnd:
        return []
    if not _ensure_comtypes_importable():
        return []
    try:
        import comtypes.client  # type: ignore[import-not-found]
    except Exception:
        return []
    try:
        automation = comtypes.client.CreateObject("{FF48DBA4-60EF-4201-AA87-54103EEF594E}", interface=None)
        root = automation.ElementFromHandle(hwnd)
        walker = automation.RawViewWalker
    except Exception:
        return []

    controls: list[AutomationControlInfo] = []
    _walk_automation_tree(root, walker, controls, depth=0, max_depth=max_depth, max_controls=max_controls)
    return controls


def _walk_automation_tree(element: Any, walker: Any, controls: list[AutomationControlInfo], *, depth: int, max_depth: int, max_controls: int) -> None:
    if element is None or depth > max_depth or len(controls) >= max_controls:
        return
    info = _control_info(element, depth)
    if info is not None:
        controls.append(info)
    try:
        child = walker.GetFirstChildElement(element)
    except Exception:
        child = None
    while child is not None and len(controls) < max_controls:
        _walk_automation_tree(child, walker, controls, depth=depth + 1, max_depth=max_depth, max_controls=max_controls)
        try:
            child = walker.GetNextSiblingElement(child)
        except Exception:
            break


def _control_info(element: Any, depth: int) -> AutomationControlInfo | None:
    try:
        name = str(element.CurrentName or "").strip()
        control_type = _control_type_name(int(element.CurrentControlType or 0))
        automation_id = str(element.CurrentAutomationId or "").strip()
        class_name = str(element.CurrentClassName or "").strip()
        rect = element.CurrentBoundingRectangle
    except Exception:
        return None
    if not name and not automation_id and not class_name:
        return None
    bounds = {
        "left": int(getattr(rect, "left", 0)),
        "top": int(getattr(rect, "top", 0)),
        "right": int(getattr(rect, "right", 0)),
        "bottom": int(getattr(rect, "bottom", 0)),
    }
    text = " ".join([name, automation_id, class_name, control_type]).lower()
    return AutomationControlInfo(
        name=name,
        control_type=control_type,
        automation_id=automation_id,
        class_name=class_name,
        bounding_rect=bounds,
        depth=depth,
        looks_like_chat=_looks_like_chat_control(text),
        looks_like_input=_looks_like_input_control(text),
    )


def _looks_like_chat_control(text: str) -> bool:
    return any(token in text for token in ["message", "chat", "conversation", "消息", "聊天", "会话"])


def _looks_like_input_control(text: str) -> bool:
    return any(token in text for token in ["edit", "input", "text", "输入", "发送", "按住说话"])


def _control_type_name(control_type: int) -> str:
    names = {
        50004: "button",
        50005: "calendar",
        50006: "checkbox",
        50008: "combobox",
        50010: "edit",
        50020: "pane",
        50026: "text",
        50030: "window",
        50033: "tree",
        50034: "treeitem",
        50036: "group",
    }
    return names.get(control_type, str(control_type) if control_type else "")


def _uia_dependency_status() -> dict[str, Any]:
    if sys.platform != "win32":
        return {"available": False, "reason": "not_windows"}
    if _ensure_comtypes_importable():
        return {"available": True, "provider": "comtypes", "vendor_path": str(VENDOR_UI_PATH)}
    return {
        "available": False,
        "reason": "uia_dependency_missing",
        "install": "python -m pip install --target vendor\\windows-ui comtypes",
        "vendor_path": str(VENDOR_UI_PATH),
    }


def _ensure_comtypes_importable() -> bool:
    try:
        import comtypes  # noqa: F401  # type: ignore[import-not-found]

        return True
    except Exception:
        pass
    if VENDOR_UI_PATH.exists() and str(VENDOR_UI_PATH) not in sys.path:
        sys.path.insert(0, str(VENDOR_UI_PATH))
    try:
        import comtypes  # noqa: F401  # type: ignore[import-not-found]

        return True
    except Exception:
        return False


def _window_title(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _window_class_name(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetClassNameW(hwnd, buffer, 512)
    return buffer.value


def _window_rect(hwnd: int) -> wintypes.RECT:
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect

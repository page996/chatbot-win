from __future__ import annotations

import ctypes
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.personal_wechat_bot.domain.models import SendResult
from app.personal_wechat_bot.wechat_driver.windows_readonly import (
    Win32WindowProbe,
    find_wechat_processes,
    foreground_window_info,
)
from app.personal_wechat_bot.wechat_driver.window_introspection import filter_wechat_chat_windows


WINDOWS_GUARDED_SEND_DRIVER = "windows_guarded"
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_RETURN = 0x0D
VK_V = 0x56


@dataclass(frozen=True)
class WindowsGuardedSendProbe:
    driver: str
    implemented: bool
    send_enabled: bool
    health: str
    foreground: dict[str, Any]
    windows: list[dict[str, Any]]
    processes: list[dict[str, Any]]
    blockers: list[str]


class WindowsGuardedSendDriver:
    """Guarded Windows WeChat sender.

    The driver never searches for or focuses a chat. It only pastes into the
    current foreground window after that window looks like WeChat and its title
    matches the registered conversation title.
    """

    def __init__(
        self,
        *,
        send_enabled: bool,
        data_dir: str | Path = "data",
        window_probe: Win32WindowProbe | None = None,
        input_controller: "WindowsInputController | None" = None,
        foreground_provider: Callable[[], dict[str, Any]] = foreground_window_info,
        process_provider: Callable[[], list[dict[str, Any]]] = find_wechat_processes,
        require_title_match: bool = True,
    ):
        self.send_enabled = send_enabled
        self.data_dir = Path(data_dir)
        self.window_probe = window_probe or Win32WindowProbe()
        self.input_controller = input_controller or WindowsInputController()
        self.foreground_provider = foreground_provider
        self.process_provider = process_provider
        self.require_title_match = require_title_match

    def health_check(self) -> bool:
        return bool(self.window_probe.find_wechat_windows())

    def probe(self) -> WindowsGuardedSendProbe:
        windows = [
            {"hwnd": item.hwnd, "title": item.title}
            for item in filter_wechat_chat_windows(self.window_probe.find_wechat_windows())
        ]
        processes = self.process_provider()
        foreground = self.foreground_provider()
        blockers: list[str] = []
        if not self.send_enabled:
            blockers.append("send_enabled_false")
        if not windows:
            blockers.append("wechat_window_not_found")
        if not _foreground_looks_like_wechat(foreground):
            blockers.append("foreground_not_wechat")
        return WindowsGuardedSendProbe(
            driver=WINDOWS_GUARDED_SEND_DRIVER,
            implemented=True,
            send_enabled=self.send_enabled,
            health="blocked" if blockers else "ready",
            foreground=foreground,
            windows=windows,
            processes=processes,
            blockers=blockers,
        )

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        blockers = self._send_blockers(conversation_id)
        if blockers:
            return SendResult(
                message_id="windows-guarded-send",
                conversation_id=conversation_id,
                status="failed",
                reason=";".join(blockers),
            )
        try:
            self.input_controller.paste_and_enter(text)
        except Exception as exc:
            return SendResult(
                message_id="windows-guarded-send",
                conversation_id=conversation_id,
                status="failed",
                reason=f"windows_guarded_send_error:{type(exc).__name__}",
            )
        return SendResult(
            message_id="windows-guarded-send",
            conversation_id=conversation_id,
            status="sent",
            reason="windows_guarded_paste_enter",
        )

    def _send_blockers(self, conversation_id: str) -> list[str]:
        blockers = list(self.probe().blockers)
        title = self._conversation_title(conversation_id)
        if not title:
            blockers.append("conversation_channel_not_found")
            return blockers
        foreground = self.foreground_provider()
        if self.require_title_match and not _foreground_matches_conversation(foreground, title, conversation_id):
            blockers.append("foreground_conversation_mismatch")
        return blockers

    def _conversation_title(self, conversation_id: str) -> str:
        path = self.data_dir / "conversation_channels" / conversation_id / "channel.json"
        if not path.exists():
            return ""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("chat_title", "")).strip()


class WindowsInputController:
    def paste_and_enter(self, text: str) -> None:
        if sys.platform != "win32":
            raise RuntimeError("windows_input_controller_requires_win32")
        previous = _get_clipboard_text()
        _set_clipboard_text(text)
        try:
            time.sleep(0.05)
            _hotkey_ctrl_v()
            time.sleep(0.08)
            _press_enter()
            time.sleep(0.05)
        finally:
            if previous is not None:
                _set_clipboard_text(previous)


def _get_clipboard_text() -> str | None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _set_clipboard_text(text: str) -> None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not user32.OpenClipboard(None):
        raise RuntimeError("open_clipboard_failed")
    try:
        if not user32.EmptyClipboard():
            raise RuntimeError("empty_clipboard_failed")
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            raise RuntimeError("global_alloc_failed")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise RuntimeError("global_lock_failed")
        try:
            ctypes.memmove(pointer, data, size)
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise RuntimeError("set_clipboard_data_failed")
    finally:
        user32.CloseClipboard()


def _hotkey_ctrl_v() -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def _press_enter() -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_RETURN, 0, 0, 0)
    user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)


def _foreground_looks_like_wechat(foreground: dict[str, Any]) -> bool:
    process = Path(str(foreground.get("process_name", ""))).name.lower()
    class_name = str(foreground.get("class_name", "")).lower()
    if process in {
        "chrome.exe",
        "msedge.exe",
        "firefox.exe",
        "powershell.exe",
        "windowsterminal.exe",
        "cmd.exe",
        "python.exe",
        "pythonw.exe",
    }:
        return False
    if any(token in class_name for token in ["chrome_widgetwin", "consolewindowclass", "applicationframewindow"]):
        return False
    if process in {"wechat.exe", "weixin.exe", "wechatappex.exe"}:
        return True
    title = str(foreground.get("title", "")).lower()
    return "wechat" in title or "\u5fae\u4fe1" in title


def _foreground_matches_conversation(foreground: dict[str, Any], chat_title: str, conversation_id: str) -> bool:
    title = str(foreground.get("title", ""))
    if not title:
        return False
    lowered = title.lower()
    return chat_title.lower() in lowered or conversation_id.lower() in lowered

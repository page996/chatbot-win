from __future__ import annotations

import ctypes
import hashlib
from pathlib import Path
import sys
from dataclasses import dataclass
from typing import Callable

from app.personal_wechat_bot.domain.models import RawWeChatMessage, SendResult, utc_now_iso
from app.personal_wechat_bot.wechat_driver.snapshot_provider import SnapshotProvider
from ctypes import wintypes


WindowTextProvider = Callable[[], str]


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    width: int = 0
    height: int = 0
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0
    process_id: int = 0
    process_name: str = ""
    class_name: str = ""
    visible: bool = True


class Win32WindowProbe:
    def __init__(self, title_keywords: list[str] | None = None, *, include_invisible: bool = False):
        self.title_keywords = title_keywords or ["\u5fae\u4fe1", "WeChat"]
        self.include_invisible = include_invisible

    def find_wechat_windows(self) -> list[WindowInfo]:
        if sys.platform != "win32":
            return []
        user32 = ctypes.windll.user32
        windows: list[WindowInfo] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def enum_proc(hwnd: int, _lparam: int) -> bool:
            visible = bool(user32.IsWindowVisible(hwnd))
            if not visible and not self.include_invisible:
                return True
            title = _window_title(hwnd).strip()
            class_name = _window_class_name(hwnd)
            process_id = _window_process_id(hwnd)
            process_name = _process_name(process_id)
            rect = _window_rect(hwnd)
            if _looks_like_wechat_window(title, process_name, self.title_keywords):
                windows.append(
                    WindowInfo(
                        hwnd=int(hwnd),
                        title=title,
                        width=rect.right - rect.left,
                        height=rect.bottom - rect.top,
                        left=rect.left,
                        top=rect.top,
                        right=rect.right,
                        bottom=rect.bottom,
                        process_id=process_id,
                        process_name=process_name,
                        class_name=class_name,
                        visible=visible,
                    )
                )
            return True

        user32.EnumWindows(enum_proc, 0)
        return sorted(windows, key=lambda item: (item.visible, item.width * item.height), reverse=True)


def find_wechat_processes() -> list[dict[str, object]]:
    if sys.platform != "win32":
        return []
    windows = Win32WindowProbe(include_invisible=True).find_wechat_windows()
    by_pid: dict[int, dict[str, object]] = {}
    for window in windows:
        if not window.process_id:
            continue
        existing = by_pid.setdefault(
            window.process_id,
            {
                "ProcessName": Path(window.process_name).stem,
                "Id": window.process_id,
                "MainWindowTitle": "",
                "MainWindowHandle": 0,
            },
        )
        if window.visible and window.title and not existing["MainWindowTitle"]:
            existing["MainWindowTitle"] = window.title
            existing["MainWindowHandle"] = window.hwnd
    return sorted(by_pid.values(), key=lambda item: str(item.get("ProcessName", "")))


def foreground_window_info() -> dict[str, object]:
    if sys.platform != "win32":
        return {}
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return {}
    pid = _window_process_id(hwnd)
    rect = _window_rect(hwnd)
    return {
        "hwnd": int(hwnd),
        "title": _window_title(hwnd),
        "process_id": pid,
        "process_name": _process_name(pid),
        "class_name": _window_class_name(hwnd),
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "left": rect.left,
        "top": rect.top,
        "right": rect.right,
        "bottom": rect.bottom,
        "width": rect.right - rect.left,
        "height": rect.bottom - rect.top,
    }


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


def _window_process_id(hwnd: int) -> int:
    user32 = ctypes.windll.user32
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _window_rect(hwnd: int) -> wintypes.RECT:
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect


def _process_name(pid: int) -> str:
    if not pid or sys.platform != "win32":
        return ""
    kernel32 = ctypes.windll.kernel32
    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).name
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _looks_like_wechat_window(title: str, process_name: str, title_keywords: list[str]) -> bool:
    process = process_name.lower()
    if process in {"wechat.exe", "weixin.exe", "wechatappex.exe"}:
        return True
    lowered_title = title.lower()
    chinese_wechat = "\u5fae\u4fe1"
    if chinese_wechat in title:
        return True
    return any(keyword.lower() in lowered_title for keyword in title_keywords if keyword == chinese_wechat)


class SnapshotMessageParser:
    """Parse a conservative line-based snapshot format.

    Supported lines:
    [private] Chat Title | Sender | wxid_optional | message text
    [group] Group Title | Sender | wxid_optional | message text
    """

    def parse(self, snapshot: str, observed_at: str | None = None) -> list[RawWeChatMessage]:
        timestamp = observed_at or utc_now_iso()
        messages: list[RawWeChatMessage] = []
        for index, line in enumerate(snapshot.splitlines()):
            parsed = self._parse_line(line.strip(), index=index, observed_at=timestamp)
            if parsed is not None:
                messages.append(parsed)
        return messages

    def _parse_line(self, line: str, index: int, observed_at: str) -> RawWeChatMessage | None:
        if not line or not (line.startswith("[private]") or line.startswith("[group]")):
            return None
        prefix, rest = line.split("]", 1)
        is_group = prefix == "[group"
        parts = [part.strip() for part in rest.strip().split("|", 3)]
        if len(parts) != 4:
            return None
        chat_title, sender_name, sender_wechat_id, text = parts
        if not chat_title or not sender_name or not text:
            return None
        context_only = False
        if text.startswith("[OCR_CONTEXT]"):
            context_only = True
            text = text.removeprefix("[OCR_CONTEXT]").strip()
            if not text:
                return None
        raw_id = _snapshot_raw_id(line, observed_at, index)
        return RawWeChatMessage(
            raw_id=raw_id,
            chat_title=chat_title,
            sender_name=sender_name,
            sender_wechat_id=sender_wechat_id or None,
            text=text,
            is_group=is_group,
            observed_at=observed_at,
            driver_meta={
                "source": "windows_snapshot",
                "line_index": index,
                "context_only": context_only,
                "ocr_fallback": True,
                "attachments": _ocr_context_attachments(text) if context_only else [],
            },
        )


class WindowsWeChatReadOnlyDriver:
    def __init__(
        self,
        text_provider: WindowTextProvider | None = None,
        snapshot_provider: SnapshotProvider | None = None,
        window_probe: Win32WindowProbe | None = None,
        parser: SnapshotMessageParser | None = None,
    ):
        self.text_provider = text_provider
        self.snapshot_provider = snapshot_provider
        self.window_probe = window_probe or Win32WindowProbe()
        self.parser = parser or SnapshotMessageParser()
        self._seen_snapshot_keys: set[str] = set()

    def health_check(self) -> bool:
        if self.text_provider is not None or self.snapshot_provider is not None:
            return True
        return bool(self.window_probe.find_wechat_windows())

    def read_new_messages(self) -> list[RawWeChatMessage]:
        snapshot = self._read_snapshot()
        if not snapshot:
            return []
        messages = []
        for message in self.parser.parse(snapshot):
            snapshot_key = _message_snapshot_key(message)
            if snapshot_key in self._seen_snapshot_keys:
                continue
            self._seen_snapshot_keys.add(snapshot_key)
            messages.append(message)
        return messages

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        return SendResult(
            message_id="windows-readonly-send",
            conversation_id=conversation_id,
            status="failed",
            reason="windows_readonly_driver_never_sends",
        )

    def _read_snapshot(self) -> str:
        if self.snapshot_provider is not None:
            return self.snapshot_provider.read_text()
        if self.text_provider is not None:
            return self.text_provider()
        return ""


def _snapshot_raw_id(line: str, observed_at: str, index: int) -> str:
    digest = hashlib.sha256(f"{observed_at}:{index}:{line}".encode("utf-8")).hexdigest()
    return digest[:24]


def _message_snapshot_key(message: RawWeChatMessage) -> str:
    digest = hashlib.sha256(
        "\n".join(
            [
                message.chat_title,
                message.sender_name,
                message.sender_wechat_id or "",
                message.text,
                "group" if message.is_group else "private",
            ]
        ).encode("utf-8")
    ).hexdigest()
    return digest[:24]


def _ocr_context_attachments(text: str) -> list[dict[str, object]]:
    if not text.startswith("[OCR附件卡片]"):
        return []
    payload = text.removeprefix("[OCR附件卡片]").strip()
    name = payload.split(" kind=", 1)[0].strip()
    kind = ""
    size = ""
    if " kind=" in payload:
        rest = payload.split(" kind=", 1)[1]
        kind = rest.split(" size=", 1)[0].strip()
        if " size=" in rest:
            size = rest.split(" size=", 1)[1].strip()
    return [
        {
            "status": "ocr_card_only",
            "name": name,
            "kind": kind or "file",
            "size": size,
            "source": "ocr_file_card",
            "note": "OCR saw a WeChat file card; real file path is not available from frontend OCR.",
        }
    ]

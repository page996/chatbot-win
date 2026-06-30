from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.normalizer.normalizer import conversation_id_for
from app.personal_wechat_bot.wechat_driver.window_introspection import filter_wechat_chat_windows
from app.personal_wechat_bot.wechat_driver.windows_readonly import (
    Win32WindowProbe,
    WindowInfo,
    foreground_window_info,
)


ForegroundProvider = Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class WeChatWindowBinding:
    conversation_id: str
    conversation_type: str
    chat_title: str
    hwnd: int
    title: str
    process_id: int
    process_name: str
    class_name: str
    width: int
    height: int
    left: int
    top: int
    right: int
    bottom: int
    bound_at: str
    last_seen_at: str
    status: str = "active"


class WeChatWindowBindingStore:
    def __init__(
        self,
        data_dir: str | Path,
        *,
        window_probe: Win32WindowProbe | None = None,
        foreground_provider: ForegroundProvider = foreground_window_info,
    ):
        self.root = Path(data_dir)
        self.path = self.root / "window_bindings.json"
        self.window_probe = window_probe or Win32WindowProbe(include_invisible=False)
        self.foreground_provider = foreground_provider

    def bind_foreground(
        self,
        *,
        chat_title: str,
        conversation_type: str = "private",
        conversation_id: str = "",
    ) -> dict[str, Any]:
        foreground = self.foreground_provider()
        window = _window_from_foreground(foreground)
        blockers = _binding_blockers(window)
        if blockers:
            return {"status": "blocked", "blockers": blockers, "foreground": foreground}
        resolved_id = conversation_id or conversation_id_for(conversation_type, chat_title)
        now = utc_now_iso()
        binding = WeChatWindowBinding(
            conversation_id=resolved_id,
            conversation_type=conversation_type,
            chat_title=chat_title,
            hwnd=window.hwnd,
            title=window.title,
            process_id=window.process_id,
            process_name=window.process_name,
            class_name=window.class_name,
            width=window.width,
            height=window.height,
            left=window.left,
            top=window.top,
            right=window.right,
            bottom=window.bottom,
            bound_at=now,
            last_seen_at=now,
        )
        payload = self._read()
        bindings = [
            item
            for item in payload.get("bindings", [])
            if isinstance(item, dict) and item.get("conversation_id") != resolved_id
        ]
        bindings.append(asdict(binding))
        self._write({"bindings": sorted(bindings, key=lambda item: str(item.get("chat_title", ""))), "updated_at": now})
        return {"status": "ok", "binding": asdict(binding)}

    def resolve_window(self, conversation_id: str) -> WindowInfo | None:
        result = self.resolve_status(conversation_id)
        window = result.get("window")
        return window if isinstance(window, WindowInfo) else None

    def resolve_status(self, conversation_id: str) -> dict[str, Any]:
        binding = self.get_binding(conversation_id)
        if binding is None:
            return {"status": "not_bound", "conversation_id": conversation_id, "window": None}
        windows = filter_wechat_chat_windows(self.window_probe.find_wechat_windows())
        exact = next((item for item in windows if item.hwnd == binding.hwnd), None)
        if exact is not None:
            self._touch(binding, exact)
            return {"status": "ok", "conversation_id": conversation_id, "binding": asdict(binding), "window": exact}
        fallback = _same_physical_window(binding, windows)
        if fallback is not None:
            self._touch(binding, fallback)
            return {"status": "ok", "conversation_id": conversation_id, "binding": asdict(binding), "window": fallback}
        self._mark_stale(binding)
        return {
            "status": "stale",
            "conversation_id": conversation_id,
            "binding": asdict(binding),
            "window": None,
            "window_candidates": [asdict(item) for item in windows],
        }

    def get_binding(self, conversation_id: str) -> WeChatWindowBinding | None:
        for item in self._read().get("bindings", []):
            if not isinstance(item, dict) or item.get("conversation_id") != conversation_id:
                continue
            return _binding_from_payload(item)
        return None

    def list_bindings(self) -> list[dict[str, Any]]:
        bindings = [item for item in self._read().get("bindings", []) if isinstance(item, dict)]
        return sorted(bindings, key=lambda item: str(item.get("last_seen_at", "")), reverse=True)

    def _touch(self, binding: WeChatWindowBinding, window: WindowInfo) -> None:
        payload = self._read()
        updated: list[dict[str, Any]] = []
        now = utc_now_iso()
        for item in payload.get("bindings", []):
            if not isinstance(item, dict):
                continue
            if item.get("conversation_id") != binding.conversation_id:
                updated.append(item)
                continue
            updated.append(
                {
                    **item,
                    "hwnd": window.hwnd,
                    "title": window.title,
                    "process_id": window.process_id,
                    "process_name": window.process_name,
                    "class_name": window.class_name,
                    "width": window.width,
                    "height": window.height,
                    "left": window.left,
                    "top": window.top,
                    "right": window.right,
                    "bottom": window.bottom,
                    "last_seen_at": now,
                    "status": "active",
                }
            )
        self._write({"bindings": updated, "updated_at": now})

    def _mark_stale(self, binding: WeChatWindowBinding) -> None:
        payload = self._read()
        updated: list[dict[str, Any]] = []
        now = utc_now_iso()
        for item in payload.get("bindings", []):
            if not isinstance(item, dict):
                continue
            if item.get("conversation_id") == binding.conversation_id:
                updated.append({**item, "status": "stale", "updated_at": now})
            else:
                updated.append(item)
        self._write({"bindings": updated, "updated_at": now})

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"bindings": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"bindings": []}
        return payload if isinstance(payload, dict) else {"bindings": []}

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def _window_from_foreground(foreground: dict[str, Any]) -> WindowInfo:
    return WindowInfo(
        hwnd=int(foreground.get("hwnd", 0) or 0),
        title=str(foreground.get("title", "")),
        width=int(foreground.get("width", 0) or 0),
        height=int(foreground.get("height", 0) or 0),
        left=int(foreground.get("left", 0) or 0),
        top=int(foreground.get("top", 0) or 0),
        right=int(foreground.get("right", 0) or 0),
        bottom=int(foreground.get("bottom", 0) or 0),
        process_id=int(foreground.get("process_id", 0) or 0),
        process_name=str(foreground.get("process_name", "")),
        class_name=str(foreground.get("class_name", "")),
        visible=bool(foreground.get("visible", True)),
    )


def _binding_blockers(window: WindowInfo) -> list[str]:
    blockers: list[str] = []
    if not window.hwnd:
        blockers.append("foreground_window_missing")
    if not filter_wechat_chat_windows([window]):
        blockers.append("foreground_not_wechat_chat_window")
    return blockers


def _same_physical_window(binding: WeChatWindowBinding, windows: list[WindowInfo]) -> WindowInfo | None:
    for window in windows:
        if binding.process_id and window.process_id == binding.process_id and window.title == binding.title:
            return window
    for window in windows:
        if window.title == binding.title and window.process_name.lower() == binding.process_name.lower():
            return window
    return None


def _binding_from_payload(payload: dict[str, Any]) -> WeChatWindowBinding:
    return WeChatWindowBinding(
        conversation_id=str(payload.get("conversation_id", "")),
        conversation_type=str(payload.get("conversation_type", "private")),
        chat_title=str(payload.get("chat_title", "")),
        hwnd=int(payload.get("hwnd", 0) or 0),
        title=str(payload.get("title", "")),
        process_id=int(payload.get("process_id", 0) or 0),
        process_name=str(payload.get("process_name", "")),
        class_name=str(payload.get("class_name", "")),
        width=int(payload.get("width", 0) or 0),
        height=int(payload.get("height", 0) or 0),
        left=int(payload.get("left", 0) or 0),
        top=int(payload.get("top", 0) or 0),
        right=int(payload.get("right", 0) or 0),
        bottom=int(payload.get("bottom", 0) or 0),
        bound_at=str(payload.get("bound_at", "")),
        last_seen_at=str(payload.get("last_seen_at", "")),
        status=str(payload.get("status", "active")),
    )

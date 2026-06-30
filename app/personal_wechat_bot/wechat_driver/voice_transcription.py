from __future__ import annotations

import ctypes
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from app.personal_wechat_bot.wechat_driver.window_binding import WeChatWindowBindingStore
from app.personal_wechat_bot.wechat_driver.windows_guarded_send import _get_clipboard_text
from app.personal_wechat_bot.wechat_driver.windows_readonly import WindowInfo


KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_C = 0x43
VK_T = 0x54
VK_APPS = 0x5D
VK_RETURN = 0x0D


@dataclass(frozen=True)
class WeChatVoiceTranscriptionResult:
    status: str
    text: str = ""
    source: str = "wechat_builtin_voice_to_text"
    conversation_id: str = ""
    chat_title: str = ""
    method: str = ""
    error: str = ""
    blockers: list[str] | None = None


class WeChatVoiceTranscriptionBridge:
    """Trigger WeChat's own voice-to-text path for a manually bound chat.

    The bridge intentionally avoids OCR. It only acts on a known window binding
    and reads the resulting text from the clipboard after a configurable action
    sequence. The user or an upstream monitor still owns selecting the voice
    bubble to transcribe.
    """

    def __init__(
        self,
        data_dir: str | Path = "data",
        *,
        binding_store: WeChatWindowBindingStore | None = None,
        input_controller: "WeChatVoiceInputController | None" = None,
        config_path: str | Path | None = None,
        window_resolver: Callable[[str], WindowInfo | None] | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.binding_store = binding_store or WeChatWindowBindingStore(self.data_dir)
        self.input_controller = input_controller or WeChatVoiceInputController()
        self.config_path = Path(config_path) if config_path else self.data_dir / "voice_transcription.json"
        self.window_resolver = window_resolver or self.binding_store.resolve_window

    def health(self) -> dict[str, Any]:
        config = self._config()
        return {
            "status": "ok" if sys.platform == "win32" else "blocked",
            "backend": "wechat_builtin_voice_to_text",
            "available": sys.platform == "win32",
            "action_sequence": config.get("action_sequence", []),
            "requires": [
                "manually bound WeChat chat window",
                "target voice bubble selected or focused by upstream monitor",
                "WeChat built-in voice-to-text available in client UI",
            ],
        }

    def transcribe_selected_voice(self, conversation_id: str) -> WeChatVoiceTranscriptionResult:
        binding = self.binding_store.get_binding(conversation_id)
        if binding is None:
            return WeChatVoiceTranscriptionResult(
                status="blocked",
                conversation_id=conversation_id,
                error="wechat_window_not_bound",
                blockers=["wechat_window_not_bound"],
            )
        window = self.window_resolver(conversation_id)
        if window is None:
            return WeChatVoiceTranscriptionResult(
                status="blocked",
                conversation_id=conversation_id,
                chat_title=binding.chat_title,
                error="bound_wechat_window_not_found",
                blockers=["bound_wechat_window_not_found"],
            )
        config = self._config()
        before = self.input_controller.clipboard_text()
        try:
            self.input_controller.run_sequence(window.hwnd, config.get("action_sequence", []))
            time.sleep(float(config.get("settle_seconds", 0.8)))
            if config.get("copy_after_action", True):
                self.input_controller.copy_selection()
                time.sleep(float(config.get("copy_settle_seconds", 0.2)))
            after = self.input_controller.clipboard_text()
        except Exception as exc:
            return WeChatVoiceTranscriptionResult(
                status="failed",
                conversation_id=conversation_id,
                chat_title=binding.chat_title,
                method="configured_wechat_ui_sequence",
                error=f"{type(exc).__name__}: {exc}",
            )
        text = _clean_transcript(after, before)
        if not text:
            return WeChatVoiceTranscriptionResult(
                status="blocked",
                conversation_id=conversation_id,
                chat_title=binding.chat_title,
                method="configured_wechat_ui_sequence",
                error="wechat_builtin_transcript_not_observed",
                blockers=["wechat_builtin_transcript_not_observed"],
            )
        return WeChatVoiceTranscriptionResult(
            status="transcribed",
            text=text,
            conversation_id=conversation_id,
            chat_title=binding.chat_title,
            method="configured_wechat_ui_sequence",
        )

    def _config(self) -> dict[str, Any]:
        default = {
            "action_sequence": [
                {"type": "hotkey", "keys": ["apps"]},
                {"type": "sleep", "seconds": 0.2},
                {"type": "hotkey", "keys": ["t"]},
            ],
            "copy_after_action": True,
            "settle_seconds": 0.8,
            "copy_settle_seconds": 0.2,
        }
        if not self.config_path.exists():
            return default
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
        if not isinstance(payload, dict):
            return default
        return {**default, **payload}


class WeChatVoiceInputController:
    def run_sequence(self, hwnd: int, sequence: list[dict[str, Any]]) -> None:
        if sys.platform != "win32":
            raise RuntimeError("wechat_voice_bridge_requires_win32")
        if not hwnd:
            raise RuntimeError("missing_wechat_hwnd")
        ctypes.windll.user32.SetForegroundWindow(int(hwnd))
        time.sleep(0.1)
        for step in sequence:
            if not isinstance(step, dict):
                continue
            step_type = str(step.get("type", "")).strip().lower()
            if step_type == "sleep":
                time.sleep(float(step.get("seconds", 0.1)))
            elif step_type == "hotkey":
                keys = [str(item).strip().lower() for item in step.get("keys", []) if str(item).strip()]
                _hotkey(keys)
            elif step_type == "key":
                key = str(step.get("key", "")).strip().lower()
                _press_key(_vk(key))
            elif step_type == "context-menu":
                _press_key(VK_APPS)
            elif step_type == "enter":
                _press_key(VK_RETURN)

    def copy_selection(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("wechat_voice_bridge_requires_win32")
        _hotkey(["ctrl", "c"])

    def clipboard_text(self) -> str:
        return _get_clipboard_text() or ""


def result_payload(result: WeChatVoiceTranscriptionResult) -> dict[str, Any]:
    return {key: value for key, value in asdict(result).items() if value not in (None, "", [])}


def _clean_transcript(after: str, before: str) -> str:
    text = after.strip()
    if not text or text == before.strip():
        return ""
    for prefix in ["转文字结果", "语音转文字", "以下为转文字结果"]:
        if text.startswith(prefix):
            text = text.removeprefix(prefix).strip(" ：:\n\t")
    return text


def _hotkey(keys: list[str]) -> None:
    vks = [_vk(key) for key in keys]
    for vk in vks:
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)


def _press_key(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)


def _vk(key: str) -> int:
    lookup = {
        "ctrl": VK_CONTROL,
        "control": VK_CONTROL,
        "shift": VK_SHIFT,
        "alt": VK_MENU,
        "menu": VK_MENU,
        "apps": VK_APPS,
        "context": VK_APPS,
        "enter": VK_RETURN,
        "return": VK_RETURN,
        "c": VK_C,
        "t": VK_T,
    }
    if key in lookup:
        return lookup[key]
    if len(key) == 1:
        return ord(key.upper())
    raise ValueError(f"unsupported key: {key}")

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.normalizer.normalizer import conversation_id_for
from app.personal_wechat_bot.wechat_driver.window_binding import WeChatWindowBindingStore
from app.personal_wechat_bot.wechat_driver.windows_readonly import WindowInfo, Win32WindowProbe


class WeChatWindowBindingStoreTest(unittest.TestCase):
    def test_bind_foreground_persists_and_resolves_matching_hwnd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatWindowBindingStore(
                Path(tmp),
                window_probe=_Probe([_window(10)]),
                foreground_provider=lambda: _foreground(10),
            )

            result = store.bind_foreground(chat_title="PAGE")
            resolved = store.resolve_window(conversation_id_for("private", "PAGE"))

            self.assertEqual(result["status"], "ok")
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.hwnd, 10)
            self.assertTrue((Path(tmp) / "window_bindings.json").exists())

    def test_bind_foreground_rejects_non_wechat_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatWindowBindingStore(
                Path(tmp),
                foreground_provider=lambda: {
                    **_foreground(20),
                    "title": "WeChat Agent Send Queue - Google Chrome",
                    "process_name": "chrome.exe",
                },
            )

            result = store.bind_foreground(chat_title="PAGE")

            self.assertEqual(result["status"], "blocked")
            self.assertIn("foreground_not_wechat_chat_window", result["blockers"])

    def test_resolve_falls_back_to_same_process_and_title_if_hwnd_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatWindowBindingStore(
                Path(tmp),
                window_probe=_Probe([_window(31, process_id=100)]),
                foreground_provider=lambda: _foreground(30, process_id=100),
            )
            store.bind_foreground(chat_title="PAGE")

            resolved = store.resolve_window(conversation_id_for("private", "PAGE"))

            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.hwnd, 31)
            self.assertEqual(store.list_bindings()[0]["hwnd"], 31)

    def test_resolve_status_marks_binding_stale_when_window_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatWindowBindingStore(
                Path(tmp),
                window_probe=_Probe([_window(40)]),
                foreground_provider=lambda: _foreground(40),
            )
            store.bind_foreground(chat_title="PAGE")
            store.window_probe = _Probe([])

            status = store.resolve_status(conversation_id_for("private", "PAGE"))

            self.assertEqual(status["status"], "stale")
            self.assertIsNone(status["window"])
            self.assertEqual(store.resolve_window(conversation_id_for("private", "PAGE")), None)
            self.assertEqual(store.list_bindings()[0]["status"], "stale")


def _window(hwnd: int, *, process_id: int = 100) -> WindowInfo:
    return WindowInfo(
        hwnd=hwnd,
        title="微信",
        width=1000,
        height=700,
        left=100,
        top=100,
        right=1100,
        bottom=800,
        process_id=process_id,
        process_name="Weixin.exe",
        class_name="WeChatMainWndForPC",
        visible=True,
    )


def _foreground(hwnd: int, *, process_id: int = 100) -> dict[str, object]:
    return _window(hwnd, process_id=process_id).__dict__


class _Probe(Win32WindowProbe):
    def __init__(self, windows: list[WindowInfo]):
        self.windows = windows

    def find_wechat_windows(self) -> list[WindowInfo]:
        return self.windows


if __name__ == "__main__":
    unittest.main()

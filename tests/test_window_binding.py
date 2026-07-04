from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.wechat_driver.window_binding import WeChatWindowBindingStore
from app.personal_wechat_bot.wechat_driver.windows_readonly import WindowInfo, Win32WindowProbe


class WeChatWindowBindingStoreTest(unittest.TestCase):
    """Foreground binding creation was removed (wcf delivers by wxid/roomid).

    The store is now read-only: it reads legacy ``window_bindings.json`` entries
    and reconciles their liveness. These tests seed bindings directly on disk.
    """

    def test_resolves_matching_hwnd_from_seeded_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _seed_binding(Path(tmp), "private:PAGE", hwnd=10)
            store = WeChatWindowBindingStore(Path(tmp), window_probe=_Probe([_window(10)]))

            resolved = store.resolve_window("private:PAGE")

            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.hwnd, 10)

    def test_resolve_falls_back_to_same_process_and_title_if_hwnd_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _seed_binding(Path(tmp), "private:PAGE", hwnd=30, process_id=100)
            store = WeChatWindowBindingStore(Path(tmp), window_probe=_Probe([_window(31, process_id=100)]))

            resolved = store.resolve_window("private:PAGE")

            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.hwnd, 31)
            self.assertEqual(store.list_bindings()[0]["hwnd"], 31)

    def test_resolve_status_marks_binding_stale_when_window_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _seed_binding(Path(tmp), "private:PAGE", hwnd=40)
            store = WeChatWindowBindingStore(Path(tmp), window_probe=_Probe([]))

            status = store.resolve_status("private:PAGE")

            self.assertEqual(status["status"], "stale")
            self.assertIsNone(status["window"])
            self.assertEqual(store.resolve_window("private:PAGE"), None)
            self.assertEqual(store.list_bindings()[0]["status"], "stale")

    def test_get_binding_returns_none_for_unknown_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatWindowBindingStore(Path(tmp), window_probe=_Probe([]))
            self.assertIsNone(store.get_binding("private:unknown"))


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


def _seed_binding(data_dir: Path, conversation_id: str, *, hwnd: int, process_id: int = 100) -> None:
    now = "2026-06-30T00:00:00+00:00"
    payload = {
        "bindings": [
            {
                "conversation_id": conversation_id,
                "conversation_type": "private",
                "chat_title": "PAGE",
                "hwnd": hwnd,
                "title": "微信",
                "process_id": process_id,
                "process_name": "Weixin.exe",
                "class_name": "WeChatMainWndForPC",
                "width": 1000,
                "height": 700,
                "left": 100,
                "top": 100,
                "right": 1100,
                "bottom": 800,
                "bound_at": now,
                "last_seen_at": now,
                "status": "active",
            }
        ],
        "updated_at": now,
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "window_bindings.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class _Probe(Win32WindowProbe):
    def __init__(self, windows: list[WindowInfo]):
        self.windows = windows

    def find_wechat_windows(self) -> list[WindowInfo]:
        return self.windows


if __name__ == "__main__":
    unittest.main()

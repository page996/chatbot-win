from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.wechat_driver.windows_guarded_send import WindowsGuardedSendDriver
from app.personal_wechat_bot.wechat_driver.windows_readonly import Win32WindowProbe, WindowInfo


class WindowsGuardedSendDriverTest(unittest.TestCase):
    def test_send_blocks_when_send_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_channel(data_dir, "conv-1", "PAGE")
            controller = _InputController()
            driver = WindowsGuardedSendDriver(
                send_enabled=False,
                data_dir=data_dir,
                window_probe=_Probe([_wechat_window(1, "WeChat - PAGE")]),
                input_controller=controller,
                foreground_provider=lambda: {"title": "WeChat - PAGE", "process_name": "WeChat.exe"},
                process_provider=lambda: [],
            )

            result = driver.send_message("conv-1", "hello")

            self.assertEqual(result.status, "failed")
            self.assertIn("send_enabled_false", result.reason)
            self.assertEqual(controller.sent, [])

    def test_send_blocks_when_foreground_conversation_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_channel(data_dir, "conv-1", "PAGE")
            controller = _InputController()
            driver = WindowsGuardedSendDriver(
                send_enabled=True,
                data_dir=data_dir,
                window_probe=_Probe([_wechat_window(1, "WeChat - OTHER")]),
                input_controller=controller,
                foreground_provider=lambda: {"title": "WeChat - OTHER", "process_name": "WeChat.exe"},
                process_provider=lambda: [],
            )

            result = driver.send_message("conv-1", "hello")

            self.assertEqual(result.status, "failed")
            self.assertIn("foreground_conversation_mismatch", result.reason)
            self.assertEqual(controller.sent, [])

    def test_send_pastes_and_enters_when_guards_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_channel(data_dir, "conv-1", "PAGE")
            controller = _InputController()
            driver = WindowsGuardedSendDriver(
                send_enabled=True,
                data_dir=data_dir,
                window_probe=_Probe([_wechat_window(1, "WeChat - PAGE")]),
                input_controller=controller,
                foreground_provider=lambda: {"title": "WeChat - PAGE", "process_name": "WeChat.exe"},
                process_provider=lambda: [],
            )

            result = driver.send_message("conv-1", "hello")

            self.assertEqual(result.status, "sent")
            self.assertEqual(result.reason, "windows_guarded_paste_enter")
            self.assertEqual(controller.sent, ["hello"])

    def test_send_accepts_chinese_wechat_window_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_channel(data_dir, "conv-1", "PAGE")
            controller = _InputController()
            driver = WindowsGuardedSendDriver(
                send_enabled=True,
                data_dir=data_dir,
                window_probe=_Probe([_wechat_window(1, "\u5fae\u4fe1 - PAGE")]),
                input_controller=controller,
                foreground_provider=lambda: {"title": "\u5fae\u4fe1 - PAGE", "process_name": "WeChat.exe"},
                process_provider=lambda: [],
            )

            result = driver.send_message("conv-1", "hello")

            self.assertEqual(result.status, "sent")
            self.assertEqual(controller.sent, ["hello"])

    def test_send_accepts_manually_bound_foreground_hwnd_when_title_is_generic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            _write_channel(data_dir, "conv-1", "PAGE")
            _write_binding(data_dir, "conv-1", hwnd=10, title="微信")
            controller = _InputController()
            driver = WindowsGuardedSendDriver(
                send_enabled=True,
                data_dir=data_dir,
                window_probe=_Probe([_wechat_window(10, "微信")]),
                input_controller=controller,
                foreground_provider=lambda: {
                    "hwnd": 10,
                    "title": "微信",
                    "process_name": "Weixin.exe",
                    "class_name": "Qt51514QWindowIcon",
                },
                process_provider=lambda: [],
            )

            result = driver.send_message("conv-1", "hello")

            self.assertEqual(result.status, "sent")
            self.assertEqual(controller.sent, ["hello"])

    def test_send_blocks_when_channel_is_missing(self) -> None:
        controller = _InputController()
        driver = WindowsGuardedSendDriver(
            send_enabled=True,
            data_dir="missing-data",
            window_probe=_Probe([_wechat_window(1, "WeChat - PAGE")]),
            input_controller=controller,
            foreground_provider=lambda: {"title": "WeChat - PAGE", "process_name": "WeChat.exe"},
            process_provider=lambda: [],
        )

        result = driver.send_message("conv-1", "hello")

        self.assertEqual(result.status, "failed")
        self.assertIn("conversation_channel_not_found", result.reason)
        self.assertEqual(controller.sent, [])

    def test_probe_rejects_chrome_sidebar_with_wechat_title(self) -> None:
        driver = WindowsGuardedSendDriver(
            send_enabled=True,
            data_dir="missing-data",
            window_probe=_Probe([_wechat_window(1, "微信")]),
            input_controller=_InputController(),
            foreground_provider=lambda: {
                "title": "WeChat Agent Send Queue - Google Chrome",
                "process_name": "chrome.exe",
                "class_name": "Chrome_WidgetWin_1",
            },
            process_provider=lambda: [],
        )

        probe = driver.probe()

        self.assertEqual(probe.health, "blocked")
        self.assertIn("foreground_not_wechat", probe.blockers)


def _write_channel(data_dir: Path, conversation_id: str, chat_title: str) -> None:
    path = data_dir / "conversation_channels" / conversation_id / "channel.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"conversation_id": conversation_id, "chat_title": chat_title}), encoding="utf-8")


def _write_binding(data_dir: Path, conversation_id: str, *, hwnd: int, title: str) -> None:
    path = data_dir / "window_bindings.json"
    path.write_text(
        json.dumps(
            {
                "bindings": [
                    {
                        "conversation_id": conversation_id,
                        "conversation_type": "private",
                        "chat_title": "PAGE",
                        "hwnd": hwnd,
                        "title": title,
                        "process_id": 100,
                        "process_name": "Weixin.exe",
                        "class_name": "Qt51514QWindowIcon",
                        "width": 1000,
                        "height": 700,
                        "left": 100,
                        "top": 100,
                        "right": 1100,
                        "bottom": 800,
                        "bound_at": "2026-07-01T00:00:00+00:00",
                        "last_seen_at": "2026-07-01T00:00:00+00:00",
                        "status": "active",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


class _Probe(Win32WindowProbe):
    def __init__(self, windows: list[WindowInfo]):
        self.windows = windows

    def find_wechat_windows(self) -> list[WindowInfo]:
        return self.windows


def _wechat_window(hwnd: int, title: str) -> WindowInfo:
    return WindowInfo(
        hwnd=hwnd,
        title=title,
        width=1000,
        height=700,
        left=100,
        top=100,
        right=1100,
        bottom=800,
        process_name="WeChat.exe",
        class_name="WeChatMainWndForPC",
    )


class _InputController:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def paste_and_enter(self, text: str) -> None:
        self.sent.append(text)


if __name__ == "__main__":
    unittest.main()

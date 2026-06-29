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
                window_probe=_Probe([WindowInfo(1, "WeChat - PAGE")]),
                input_controller=controller,
                foreground_provider=lambda: {"title": "WeChat - PAGE"},
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
                window_probe=_Probe([WindowInfo(1, "WeChat - OTHER")]),
                input_controller=controller,
                foreground_provider=lambda: {"title": "WeChat - OTHER"},
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
                window_probe=_Probe([WindowInfo(1, "WeChat - PAGE")]),
                input_controller=controller,
                foreground_provider=lambda: {"title": "WeChat - PAGE"},
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
                window_probe=_Probe([WindowInfo(1, "\u5fae\u4fe1 - PAGE")]),
                input_controller=controller,
                foreground_provider=lambda: {"title": "\u5fae\u4fe1 - PAGE"},
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
            window_probe=_Probe([WindowInfo(1, "WeChat - PAGE")]),
            input_controller=controller,
            foreground_provider=lambda: {"title": "WeChat - PAGE"},
            process_provider=lambda: [],
        )

        result = driver.send_message("conv-1", "hello")

        self.assertEqual(result.status, "failed")
        self.assertIn("conversation_channel_not_found", result.reason)
        self.assertEqual(controller.sent, [])


def _write_channel(data_dir: Path, conversation_id: str, chat_title: str) -> None:
    path = data_dir / "conversation_channels" / conversation_id / "channel.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"conversation_id": conversation_id, "chat_title": chat_title}), encoding="utf-8")


class _Probe(Win32WindowProbe):
    def __init__(self, windows: list[WindowInfo]):
        self.windows = windows

    def find_wechat_windows(self) -> list[WindowInfo]:
        return self.windows


class _InputController:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def paste_and_enter(self, text: str) -> None:
        self.sent.append(text)


if __name__ == "__main__":
    unittest.main()

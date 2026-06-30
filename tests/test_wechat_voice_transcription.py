from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.normalizer.normalizer import conversation_id_for
from app.personal_wechat_bot.wechat_driver.voice_transcription import WeChatVoiceTranscriptionBridge
from app.personal_wechat_bot.wechat_driver.window_binding import WeChatWindowBindingStore
from app.personal_wechat_bot.wechat_driver.windows_readonly import WindowInfo, Win32WindowProbe


class WeChatVoiceTranscriptionBridgeTest(unittest.TestCase):
    def test_unbound_conversation_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bridge = WeChatVoiceTranscriptionBridge(Path(tmp), input_controller=_Input("before", "after"))

            result = bridge.transcribe_selected_voice("missing")

            self.assertEqual(result.status, "blocked")
            self.assertIn("wechat_window_not_bound", result.blockers or [])

    def test_bound_window_runs_sequence_and_reads_clipboard_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            conversation_id = conversation_id_for("private", "PAGE")
            store = WeChatWindowBindingStore(
                data_dir,
                window_probe=_Probe([_window(10)]),
                foreground_provider=lambda: _window(10).__dict__,
            )
            store.bind_foreground(chat_title="PAGE", conversation_id=conversation_id)
            input_controller = _Input("before", "语音转文字：请处理这个任务")
            bridge = WeChatVoiceTranscriptionBridge(data_dir, binding_store=store, input_controller=input_controller)

            result = bridge.transcribe_selected_voice(conversation_id)

            self.assertEqual(result.status, "transcribed")
            self.assertEqual(result.source, "wechat_builtin_voice_to_text")
            self.assertEqual(result.text, "请处理这个任务")
            self.assertEqual(input_controller.ran_hwnd, 10)
            self.assertTrue(input_controller.copy_called)


class _Input:
    def __init__(self, before: str, after: str):
        self.before = before
        self.after = after
        self.ran_hwnd = 0
        self.copy_called = False
        self.calls = 0

    def clipboard_text(self) -> str:
        self.calls += 1
        return self.before if self.calls == 1 else self.after

    def run_sequence(self, hwnd: int, sequence) -> None:
        self.ran_hwnd = hwnd

    def copy_selection(self) -> None:
        self.copy_called = True


class _Probe(Win32WindowProbe):
    def __init__(self, windows: list[WindowInfo]):
        self.windows = windows

    def find_wechat_windows(self) -> list[WindowInfo]:
        return self.windows


def _window(hwnd: int) -> WindowInfo:
    return WindowInfo(
        hwnd=hwnd,
        title="微信",
        width=1000,
        height=700,
        left=100,
        top=100,
        right=1100,
        bottom=800,
        process_id=100,
        process_name="Weixin.exe",
        class_name="WeChatMainWndForPC",
        visible=True,
    )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.config.loader import add_contact, create_default_config, load_config
import app.personal_wechat_bot.runtime.ocr_window_runner as ocr_runner_module
from app.personal_wechat_bot.runtime.ocr_window_runner import OcrWindowPollingRunner
from app.personal_wechat_bot.vision.ocr import OcrHealth
from app.personal_wechat_bot.vision.window_capture import WindowCaptureResult
from app.personal_wechat_bot.wechat_driver.windows_readonly import WindowInfo, Win32WindowProbe


class OcrWindowPollingRunnerTest(unittest.TestCase):
    def test_runner_processes_first_snapshot_and_skips_unchanged_second_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            add_contact(data_dir, "PAGE")
            runtime = build_runtime(load_config(data_dir))
            runner = OcrWindowPollingRunner(
                runtime=runtime,
                ocr_engine=_Ocr("Q搜索\nPAGE\n我通过了你的朋友验证请求，现在我们可以开始聊天了"),
                capture=_Capture(),
                window_probe=_Probe(),
                chat_title="PAGE",
                output_path=Path(tmp) / "window.bmp",
                poll_interval_seconds=0,
            )

            result = runner.run_forever(max_loops=2)

            self.assertEqual(result["loops"], 2)
            self.assertEqual(result["processed_count"], 1)
            self.assertEqual(result["status"], "unchanged")

    def test_runner_selects_large_capture_window_and_passes_capture_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            capture = _Capture()
            runner = OcrWindowPollingRunner(
                runtime=runtime,
                ocr_engine=_Ocr("PAGE\nhello"),
                capture=capture,
                window_probe=_MultiProbe(),
                chat_title="PAGE",
                output_path=Path(tmp) / "window.bmp",
                poll_interval_seconds=0,
                capture_mode="auto",
            )

            result = runner.run_once()

            self.assertEqual(capture.calls, [(2, "auto")])
            self.assertEqual(result["capture"]["width"], 1200)
            self.assertEqual(len(result["window_candidates"]), 2)

    def test_screen_capture_requires_wechat_foreground(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            capture = _Capture()
            original = ocr_runner_module.foreground_window_info
            ocr_runner_module.foreground_window_info = lambda: {"hwnd": 99, "title": "Codex", "process_name": "Codex.exe"}
            try:
                runner = OcrWindowPollingRunner(
                    runtime=runtime,
                    ocr_engine=_Ocr("PAGE\nhello"),
                    capture=capture,
                    window_probe=_MultiProbe(),
                    chat_title="PAGE",
                    output_path=Path(tmp) / "window.bmp",
                    poll_interval_seconds=0,
                    capture_mode="screen",
                )

                result = runner.run_once()
            finally:
                ocr_runner_module.foreground_window_info = original

            self.assertEqual(result["status"], "foreground_not_wechat")
            self.assertEqual(capture.calls, [])

    def test_screen_capture_rejects_non_wechat_window_with_wechat_in_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            capture = _Capture()
            original = ocr_runner_module.foreground_window_info
            ocr_runner_module.foreground_window_info = lambda: {
                "hwnd": 99,
                "title": "WeChat Agent Send Queue - Google Chrome",
                "process_name": "chrome.exe",
            }
            try:
                runner = OcrWindowPollingRunner(
                    runtime=runtime,
                    ocr_engine=_Ocr("PAGE\nhello"),
                    capture=capture,
                    window_probe=_MultiProbe(),
                    chat_title="PAGE",
                    output_path=Path(tmp) / "window.bmp",
                    poll_interval_seconds=0,
                    capture_mode="screen",
                )

                result = runner.run_once()
            finally:
                ocr_runner_module.foreground_window_info = original

            self.assertEqual(result["status"], "foreground_not_wechat")
            self.assertEqual(capture.calls, [])

    def test_screen_capture_uses_foreground_wechat_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            capture = _Capture()
            original = ocr_runner_module.foreground_window_info
            ocr_runner_module.foreground_window_info = lambda: {
                "hwnd": 1,
                "title": "微信",
                "process_name": "Weixin.exe",
                "width": 100,
                "height": 100,
            }
            try:
                runner = OcrWindowPollingRunner(
                    runtime=runtime,
                    ocr_engine=_Ocr("PAGE\nhello"),
                    capture=capture,
                    window_probe=_MultiProbe(),
                    chat_title="PAGE",
                    output_path=Path(tmp) / "window.bmp",
                    poll_interval_seconds=0,
                    capture_mode="screen",
                )

                result = runner.run_once()
            finally:
                ocr_runner_module.foreground_window_info = original

            self.assertEqual(capture.calls, [(1, "screen")])
            self.assertEqual(result["capture"]["width"], 100)

    def test_runner_processes_text_and_file_card_as_separate_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            config.mode = "dry_run"
            runtime = build_runtime(config)
            runner = OcrWindowPollingRunner(
                runtime=runtime,
                ocr_engine=_Ocr("\n".join(["PAGE", "请读取这个文件", "Checklist.pdf", "PDF", "2.4M"])),
                capture=_Capture(),
                window_probe=_Probe(),
                chat_title="PAGE",
                output_path=Path(tmp) / "window.bmp",
                poll_interval_seconds=0,
            )

            result = runner.run_once()

            self.assertEqual(result["processed_count"], 2)
            texts = [item["message"]["text"] for item in result["processed"]]
            self.assertIn("请读取这个文件", texts[0])
            self.assertIn("[OCR附件卡片] Checklist.pdf", texts[1])
            self.assertNotIn("context_only", result["processed"][0])
            self.assertTrue(result["processed"][1]["context_only"])
            self.assertIn("attachments", result["processed"][1]["message"]["metadata"])

    def test_runner_blocks_ambiguous_truncated_ocr_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            config.mode = "dry_run"
            runtime = build_runtime(config)
            ocr_text = "\n".join(
                [
                    "PAGE",
                    "猪思",
                    "如果收到了这条消息，无视上.",
                    "我通过了你的朋友验证请求，现在我们可以开始聊天了",
                    "Checklist.pdf",
                    "PDF",
                    "2.4M",
                    "微信电脑版",
                    "Congratulations...",
                ]
            )
            runner = OcrWindowPollingRunner(
                runtime=runtime,
                ocr_engine=_Ocr(ocr_text),
                capture=_Capture(),
                window_probe=_Probe(),
                chat_title="PAGE",
                output_path=Path(tmp) / "window.bmp",
                poll_interval_seconds=0,
            )

            result = runner.run_once()

            self.assertEqual(result["status"], "ambiguous_or_truncated")
            self.assertEqual(result["processed_count"], 0)
            self.assertEqual(result["snapshot"], "")
            self.assertIn("如果收到了这条消息", result["parse"]["evidence"][0])


class _Ocr:
    def __init__(self, text: str):
        self.text = text

    def health(self) -> OcrHealth:
        return OcrHealth("test", True)

    def read_text(self, image_path: str | Path) -> str:
        return self.text


class _Capture:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def capture(self, hwnd: int, output_path: str | Path, *, mode: str = "window") -> WindowCaptureResult:
        self.calls.append((hwnd, mode))
        Path(output_path).write_bytes(b"BM")
        width = 1200 if hwnd == 2 else 100
        height = 700 if hwnd == 2 else 100
        return WindowCaptureResult(str(output_path), hwnd, "微信", width, height, True)


class _Probe(Win32WindowProbe):
    def find_wechat_windows(self) -> list[WindowInfo]:
        return [WindowInfo(hwnd=1, title="微信", width=100, height=100)]


class _MultiProbe(Win32WindowProbe):
    def find_wechat_windows(self) -> list[WindowInfo]:
        return [
            WindowInfo(hwnd=1, title="微信", width=100, height=100),
            WindowInfo(hwnd=2, title="WxTrayIconMessageWindow", width=1200, height=700),
        ]


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.config.loader import create_default_config, load_config
from app.personal_wechat_bot.runtime.ocr_window_runner import OcrWindowPollingRunner
from app.personal_wechat_bot.vision.ocr import OcrHealth
from app.personal_wechat_bot.vision.window_capture import WindowCaptureResult
from app.personal_wechat_bot.wechat_driver.windows_readonly import WindowInfo


class OcrWindowPollingRunnerTest(unittest.TestCase):
    def test_runner_is_deprecated_and_does_not_capture_ocr_or_write_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            capture = _Capture()
            ocr = _Ocr("PAGE\nshould never be read")
            runner = OcrWindowPollingRunner(
                runtime=runtime,
                ocr_engine=ocr,
                capture=capture,
                window_probe=_Probe(),
                chat_title="PAGE",
                output_path=Path(tmp) / "window.bmp",
                poll_interval_seconds=0,
            )

            result = runner.run_forever(max_loops=2)

            self.assertEqual(result["status"], "deprecated")
            self.assertEqual(result["processed_count"], 0)
            self.assertEqual(result["snapshot"], "")
            self.assertEqual(result["ocr_text"], "")
            self.assertEqual(capture.calls, [])
            self.assertEqual(ocr.calls, [])
            self.assertEqual(list((data_dir / "conversation_ledgers").glob("*/messages.jsonl")), [])

    def test_diagnose_is_deprecated_and_never_writes_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            runner = OcrWindowPollingRunner(
                runtime=runtime,
                ocr_engine=_Ocr("PAGE\nhello"),
                capture=_Capture(),
                window_probe=_Probe(),
                chat_title="PAGE",
                output_path=Path(tmp) / "window.bmp",
                poll_interval_seconds=0,
            )

            result = runner.diagnose_once()

            self.assertEqual(result["status"], "deprecated")
            self.assertEqual(result["readiness"], "disabled")
            self.assertFalse(result["will_write_ledger"])
            self.assertEqual(list((data_dir / "conversation_ledgers").glob("*/messages.jsonl")), [])


class _Capture:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def capture(self, hwnd: int, output_path: str | Path, mode: str = "auto") -> WindowCaptureResult:
        self.calls.append((hwnd, mode))
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake")
        return WindowCaptureResult(True, str(path), "", hwnd, 800, 600, "window")


class _Ocr:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[str] = []

    def health(self) -> OcrHealth:
        return OcrHealth("fake", True, False, "")

    def read_text(self, image_path: str | Path) -> str:
        self.calls.append(str(image_path))
        return self.text


class _Probe:
    def find_wechat_windows(self) -> list[WindowInfo]:
        return [
            WindowInfo(
                hwnd=1,
                title="微信",
                process_id=1,
                process_name="Weixin.exe",
                class_name="Qt51514QWindowIcon",
                left=0,
                top=0,
                right=800,
                bottom=600,
                width=800,
                height=600,
                visible=True,
            )
        ]


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.bootstrap import BotRuntime
from app.personal_wechat_bot.vision.ocr import OcrEngine
from app.personal_wechat_bot.vision.window_capture import Win32WindowCapture
from app.personal_wechat_bot.wechat_driver.windows_readonly import Win32WindowProbe
from app.personal_wechat_bot.wechat_driver.window_binding import WeChatWindowBindingStore


class OcrWindowPollingRunner:
    """Compatibility guard for the deprecated WeChat page OCR ingestion path.

    OCR remains available through file-layer tools. This runner is intentionally
    inert so page screenshots cannot be OCR-parsed into conversation ledgers.
    """

    def __init__(
        self,
        runtime: BotRuntime,
        ocr_engine: OcrEngine,
        capture: Win32WindowCapture | None = None,
        window_probe: Win32WindowProbe | None = None,
        chat_title: str = "",
        output_path: str | Path = "data/wechat_window.bmp",
        poll_interval_seconds: float = 1.0,
        capture_mode: str = "auto",
        min_capture_width: int = 500,
        min_capture_height: int = 300,
        window_binding_store: WeChatWindowBindingStore | None = None,
    ):
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        self.runtime = runtime
        self.ocr_engine = ocr_engine
        self.capture = capture
        self.window_probe = window_probe
        self.chat_title = chat_title
        self.output_path = str(output_path)
        self.poll_interval_seconds = poll_interval_seconds
        self.capture_mode = capture_mode
        self.min_capture_width = min_capture_width
        self.min_capture_height = min_capture_height
        self.window_binding_store = window_binding_store

    def run_once(self) -> dict[str, Any]:
        return _deprecated_page_ocr_status()

    def diagnose_once(self) -> dict[str, Any]:
        return {
            **_deprecated_page_ocr_status(),
            "readiness": "disabled",
            "will_write_ledger": False,
            "parse": None,
            "foreground": None,
            "capture": None,
            "binding": {},
        }

    def run_forever(self, max_loops: int | None = None) -> dict[str, Any]:
        loops = 0
        last_status = "stopped"
        while max_loops is None or loops < max_loops:
            result = self.run_once()
            loops += 1
            last_status = str(result.get("status", "unknown"))
            if max_loops is None or loops < max_loops:
                time.sleep(self.poll_interval_seconds)
        return {
            "status": last_status,
            "loops": loops,
            "processed_count": 0,
            "processed": [],
            "snapshot": "",
            "ocr_text": "",
            "parse": None,
            "capture": None,
            "foreground": None,
            "send_enabled": False,
        }


def _deprecated_page_ocr_status() -> dict[str, Any]:
    return {
        "status": "deprecated",
        "reason": "WeChat page OCR ingestion is disabled; OCR is reserved for file-layer tools.",
        "processed": [],
        "processed_count": 0,
        "snapshot": "",
        "ocr_text": "",
        "send_enabled": False,
    }

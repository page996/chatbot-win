from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.bootstrap import BotRuntime
from app.personal_wechat_bot.runtime.polling_runner import PollingRunner
from app.personal_wechat_bot.vision.ocr import OcrEngine
from app.personal_wechat_bot.vision.window_capture import Win32WindowCapture, WindowCaptureResult
from app.personal_wechat_bot.wechat_driver.windows_readonly import (
    WindowsWeChatReadOnlyDriver,
    Win32WindowProbe,
    foreground_window_info,
)
from app.personal_wechat_bot.wechat_driver.ocr_snapshot_parser import parse_ocr_snapshot


class OcrWindowPollingRunner:
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
    ):
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        self.runtime = runtime
        self.ocr_engine = ocr_engine
        self.capture = capture or Win32WindowCapture()
        self.window_probe = window_probe or Win32WindowProbe(include_invisible=True)
        self.chat_title = chat_title
        self.output_path = str(output_path)
        self.poll_interval_seconds = poll_interval_seconds
        self.capture_mode = capture_mode
        self.min_capture_width = min_capture_width
        self.min_capture_height = min_capture_height
        self._last_snapshot = ""

    def run_once(self) -> dict[str, Any]:
        windows = self.window_probe.find_wechat_windows()
        if not windows:
            return {"status": "not_found", "processed": [], "processed_count": 0, "send_enabled": False}
        foreground = foreground_window_info()
        target = self._select_capture_window(windows, foreground=foreground)
        if target is None:
            return {
                "status": "foreground_not_wechat",
                "foreground": foreground,
                "window_candidates": [asdict(item) for item in windows],
                "processed": [],
                "processed_count": 0,
                "send_enabled": False,
            }
        capture_result = self.capture.capture(target.hwnd, self.output_path, mode=self.capture_mode)
        if not capture_result.ok:
            return {
                "status": "capture_failed",
                "foreground": foreground,
                "capture": asdict(capture_result),
                "window_candidates": [asdict(item) for item in windows],
                "processed": [],
                "processed_count": 0,
                "send_enabled": False,
            }
        ocr_text = self.ocr_engine.read_text(capture_result.path)
        parse_result = parse_ocr_snapshot(ocr_text, preferred_chat_title=self.chat_title)
        parse_status = parse_result.status if parse_result is not None else "empty"
        snapshots = parse_result.to_snapshots() if parse_result is not None else []
        snapshot = "\n".join(snapshots)
        if not snapshots:
            status = parse_status if parse_status != "ok" else "empty"
            return {
                "status": status,
                "foreground": foreground,
                "capture": asdict(capture_result),
                "window_candidates": [asdict(item) for item in windows],
                "ocr_text": ocr_text,
                "snapshot": "",
                "parse": _parse_debug_payload(parse_result),
                "processed": [],
                "processed_count": 0,
                "send_enabled": False,
            }
        if snapshot == self._last_snapshot:
            return {
                "status": "unchanged",
                "foreground": foreground,
                "capture": asdict(capture_result),
                "window_candidates": [asdict(item) for item in windows],
                "ocr_text": ocr_text,
                "snapshot": snapshot,
                "parse": _parse_debug_payload(parse_result),
                "processed": [],
                "processed_count": 0,
                "send_enabled": False,
            }
        self._last_snapshot = snapshot
        driver = WindowsWeChatReadOnlyDriver(text_provider=lambda: snapshot)
        result = PollingRunner(self.runtime, driver, poll_interval_seconds=0).run_forever(max_loops=1)
        result["status"] = "ok"
        result["snapshot"] = snapshot
        result["ocr_text"] = ocr_text
        result["parse"] = _parse_debug_payload(parse_result)
        result["foreground"] = foreground
        result["capture"] = asdict(capture_result)
        result["window_candidates"] = [asdict(item) for item in windows]
        result["send_enabled"] = False
        return result

    def _select_capture_window(self, windows: list[Any], *, foreground: dict[str, Any]) -> Any | None:
        if self.capture_mode == "screen":
            if not _foreground_looks_like_wechat_for_capture(foreground):
                return None
            foreground_hwnd = int(foreground.get("hwnd", 0) or 0)
            if not foreground_hwnd:
                return None
            for item in windows:
                if item.hwnd == foreground_hwnd:
                    return item
            return _window_item_from_foreground(foreground)
        suitable = [
            item
            for item in windows
            if item.width >= self.min_capture_width and item.height >= self.min_capture_height
        ]
        candidates = suitable or windows
        return max(candidates, key=lambda item: item.width * item.height)

    def run_forever(self, max_loops: int | None = None) -> dict[str, Any]:
        loops = 0
        processed_count = 0
        processed: list[dict[str, Any]] = []
        last_status = "stopped"
        last_snapshot = ""
        last_ocr_text = ""
        last_capture: dict[str, Any] | None = None
        last_foreground: dict[str, Any] | None = None
        last_parse: dict[str, Any] | None = None
        while max_loops is None or loops < max_loops:
            result = self.run_once()
            loops += 1
            last_status = str(result.get("status", "unknown"))
            last_snapshot = str(result.get("snapshot", last_snapshot))
            last_ocr_text = str(result.get("ocr_text", last_ocr_text))
            capture_payload = result.get("capture")
            if isinstance(capture_payload, dict):
                last_capture = capture_payload
            foreground_payload = result.get("foreground")
            if isinstance(foreground_payload, dict):
                last_foreground = foreground_payload
            parse_payload = result.get("parse")
            if isinstance(parse_payload, dict):
                last_parse = parse_payload
            batch = list(result.get("processed", []))
            processed.extend(batch)
            processed_count += len(batch)
            if max_loops is None or loops < max_loops:
                time.sleep(self.poll_interval_seconds)
        return {
            "status": last_status,
            "loops": loops,
            "processed_count": processed_count,
            "processed": processed,
            "snapshot": last_snapshot,
            "ocr_text": last_ocr_text,
            "parse": last_parse,
            "capture": last_capture,
            "foreground": last_foreground,
            "send_enabled": False,
        }


def _window_item_from_foreground(foreground: dict[str, Any]) -> Any:
    class _ForegroundWindow:
        hwnd = int(foreground.get("hwnd", 0) or 0)
        title = str(foreground.get("title", ""))
        width = int(foreground.get("width", 0) or 0)
        height = int(foreground.get("height", 0) or 0)
        left = int(foreground.get("left", 0) or 0)
        top = int(foreground.get("top", 0) or 0)
        right = int(foreground.get("right", 0) or 0)
        bottom = int(foreground.get("bottom", 0) or 0)
        process_id = int(foreground.get("process_id", 0) or 0)
        process_name = str(foreground.get("process_name", ""))
        class_name = str(foreground.get("class_name", ""))
        visible = bool(foreground.get("visible", True))

    return _ForegroundWindow()


def _parse_debug_payload(parse_result: Any | None) -> dict[str, Any] | None:
    if parse_result is None:
        return None
    return {
        "status": parse_result.status,
        "reason": parse_result.reason,
        "message": parse_result.message,
        "attachments": list(parse_result.attachments),
        "evidence": list(parse_result.evidence),
    }


def _foreground_looks_like_wechat_for_capture(foreground: dict[str, Any]) -> bool:
    process = str(foreground.get("process_name", "")).lower()
    if process in {"wechat.exe", "weixin.exe", "wechatappex.exe"}:
        return True
    return False

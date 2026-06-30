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
from app.personal_wechat_bot.wechat_driver.window_binding import WeChatWindowBindingStore
from app.personal_wechat_bot.wechat_driver.window_introspection import filter_wechat_chat_windows


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
        window_binding_store: WeChatWindowBindingStore | None = None,
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
        self.window_binding_store = window_binding_store
        self._last_snapshot = ""
        self._last_binding_status: dict[str, Any] = {}

    def run_once(self) -> dict[str, Any]:
        parsed = self._capture_and_parse()
        if parsed.get("status") not in {"ok", "unchanged"}:
            return parsed
        snapshots = list(parsed.get("snapshots", []))
        snapshot = str(parsed.get("snapshot", ""))
        if not snapshots:
            parse_payload = parsed.get("parse") if isinstance(parsed.get("parse"), dict) else {}
            parse_status = str(parse_payload.get("status", "empty"))
            status = parse_status if parse_status != "ok" else "empty"
            return {
                "status": status,
                "foreground": parsed.get("foreground"),
                "capture": parsed.get("capture"),
                "window_candidates": parsed.get("window_candidates", []),
                "raw_window_candidates": parsed.get("raw_window_candidates", []),
                "ocr_text": parsed.get("ocr_text", ""),
                "snapshot": "",
                "parse": parsed.get("parse"),
                "processed": [],
                "processed_count": 0,
                "send_enabled": False,
            }
        if snapshot == self._last_snapshot:
            return {
                "status": "unchanged",
                "foreground": parsed.get("foreground"),
                "capture": parsed.get("capture"),
                "window_candidates": parsed.get("window_candidates", []),
                "raw_window_candidates": parsed.get("raw_window_candidates", []),
                "ocr_text": parsed.get("ocr_text", ""),
                "snapshot": snapshot,
                "parse": parsed.get("parse"),
                "processed": [],
                "processed_count": 0,
                "send_enabled": False,
            }
        self._last_snapshot = snapshot
        driver = WindowsWeChatReadOnlyDriver(text_provider=lambda: snapshot)
        result = PollingRunner(self.runtime, driver, poll_interval_seconds=0).run_forever(max_loops=1)
        result["status"] = "ok"
        result["snapshot"] = snapshot
        result["ocr_text"] = parsed.get("ocr_text", "")
        result["parse"] = parsed.get("parse")
        result["foreground"] = parsed.get("foreground")
        result["capture"] = parsed.get("capture")
        result["window_candidates"] = parsed.get("window_candidates", [])
        result["raw_window_candidates"] = parsed.get("raw_window_candidates", [])
        result["send_enabled"] = False
        return result

    def diagnose_once(self) -> dict[str, Any]:
        result = self._capture_and_parse()
        return {
            "status": result.get("status", "unknown"),
            "readiness": _diagnose_readiness(result),
            "will_write_ledger": bool(result.get("snapshots")),
            "processed_count": 0,
            "parse": result.get("parse"),
            "foreground": result.get("foreground"),
            "capture": result.get("capture"),
            "binding": result.get("binding", _binding_debug_payload(self._last_binding_status)),
            "snapshot": result.get("snapshot", ""),
            "ocr_text": result.get("ocr_text", ""),
            "send_enabled": False,
        }

    def _capture_and_parse(self) -> dict[str, Any]:
        raw_windows = self.window_probe.find_wechat_windows()
        windows = filter_wechat_chat_windows(raw_windows)
        foreground = foreground_window_info()
        self._last_binding_status = {}
        if not windows:
            if self.capture_mode == "screen":
                target = self._select_capture_window([], foreground=foreground)
                if self._last_binding_status.get("status") == "stale":
                    return self._bound_window_unavailable(foreground, raw_windows)
                if target is None:
                    return _empty_capture_status("foreground_not_wechat", foreground, raw_windows)
            else:
                target = None
        else:
            target = self._select_capture_window(windows, foreground=foreground)
        if target is None:
            if self._last_binding_status.get("status") == "stale":
                return self._bound_window_unavailable(foreground, raw_windows, windows)
            if self.capture_mode == "screen":
                return _empty_capture_status("foreground_not_wechat", foreground, raw_windows, windows)
            return _empty_capture_status("not_found", foreground, raw_windows, windows)
        if self.capture_mode == "screen" and not _foreground_looks_like_wechat_for_capture(foreground):
            return _empty_capture_status("foreground_not_wechat", foreground, raw_windows, windows)
        capture_result = self.capture.capture(target.hwnd, self.output_path, mode=self.capture_mode)
        if not capture_result.ok:
            return {
                "status": "capture_failed",
                "foreground": foreground,
                "capture": asdict(capture_result),
                "window_candidates": [asdict(item) for item in windows],
                "raw_window_candidates": [asdict(item) for item in raw_windows],
                "processed": [],
                "processed_count": 0,
                "send_enabled": False,
            }
        ocr_text = self.ocr_engine.read_text(capture_result.path)
        parse_result = parse_ocr_snapshot(ocr_text, preferred_chat_title=self.chat_title)
        parse_status = parse_result.status if parse_result is not None else "empty"
        snapshots = parse_result.to_snapshots() if parse_result is not None else []
        snapshot = "\n".join(snapshots)
        return {
            "status": "ok" if snapshots else (parse_status if parse_status != "ok" else "empty"),
            "foreground": foreground,
            "capture": asdict(capture_result),
            "window_candidates": [asdict(item) for item in windows],
            "raw_window_candidates": [asdict(item) for item in raw_windows],
            "ocr_text": ocr_text,
            "snapshot": snapshot,
            "snapshots": snapshots,
            "parse": _parse_debug_payload(parse_result),
            "processed": [],
            "processed_count": 0,
            "send_enabled": False,
        }

    def _select_capture_window(self, windows: list[Any], *, foreground: dict[str, Any]) -> Any | None:
        bound = self._bound_window()
        if bound is not None:
            return bound
        if self._last_binding_status.get("status") == "stale":
            return None
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

    def _bound_window(self) -> Any | None:
        if self.window_binding_store is None or not self.chat_title:
            return None
        from app.personal_wechat_bot.normalizer.normalizer import conversation_id_for

        private_id = conversation_id_for("private", self.chat_title)
        group_id = conversation_id_for("group", self.chat_title)
        private_status = self.window_binding_store.resolve_status(private_id)
        if private_status.get("status") == "ok":
            self._last_binding_status = private_status
            return private_status.get("window")
        group_status = self.window_binding_store.resolve_status(group_id)
        if group_status.get("status") == "ok":
            self._last_binding_status = group_status
            return group_status.get("window")
        stale = private_status if private_status.get("status") == "stale" else group_status
        if stale.get("status") == "stale":
            self._last_binding_status = stale
        return None

    def _bound_window_unavailable(
        self,
        foreground: dict[str, Any],
        raw_windows: list[Any],
        windows: list[Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "bound_window_unavailable",
            "reason": "bound WeChat chat window is no longer visible; re-bind the target chat before OCR polling writes to the ledger",
            "binding": _binding_debug_payload(self._last_binding_status),
            "foreground": foreground,
            "window_candidates": [asdict(item) for item in (windows or [])],
            "raw_window_candidates": [asdict(item) for item in raw_windows],
            "processed": [],
            "processed_count": 0,
            "send_enabled": False,
        }

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


def _empty_capture_status(
    status: str,
    foreground: dict[str, Any],
    raw_windows: list[Any],
    windows: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "foreground": foreground,
        "window_candidates": [asdict(item) for item in (windows or [])],
        "raw_window_candidates": [asdict(item) for item in raw_windows],
        "processed": [],
        "processed_count": 0,
        "send_enabled": False,
    }


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


def _binding_debug_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    result = {key: value for key, value in payload.items() if key != "window"}
    window = payload.get("window")
    if window is not None and hasattr(window, "__dict__"):
        result["window"] = asdict(window)
    return result


def _diagnose_readiness(result: dict[str, Any]) -> str:
    status = str(result.get("status", "unknown"))
    if result.get("snapshots"):
        return "ready_to_process"
    if status in {"foreground_not_wechat", "not_found", "capture_failed", "bound_window_unavailable"}:
        return "blocked_by_window"
    if status in {"ambiguous_or_truncated", "chat_title_not_visible", "empty"}:
        return "blocked_by_ocr_parse"
    return "blocked"


def _foreground_looks_like_wechat_for_capture(foreground: dict[str, Any]) -> bool:
    process = str(foreground.get("process_name", "")).lower()
    if process in {"wechat.exe", "weixin.exe", "wechatappex.exe"}:
        return True
    return False

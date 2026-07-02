from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.runtime.polling_runner import PollingRunner
from app.personal_wechat_bot.wechat_driver.hook_events import HookEventJsonlImporter, HookImportResult


class HookMessagePullRunner:
    """Continuously bridge hook-captured WeChat events into the local queue."""

    def __init__(
        self,
        importer: HookEventJsonlImporter,
        polling_runner: PollingRunner,
        *,
        hook_event_file: str | Path,
        backend_event_file: str | Path,
    ):
        self.importer = importer
        self.polling_runner = polling_runner
        self.hook_event_file = Path(hook_event_file)
        self.backend_event_file = Path(backend_event_file)
        self.backend_event_file.parent.mkdir(parents=True, exist_ok=True)
        self.backend_event_file.touch(exist_ok=True)

    def run_once(self) -> dict[str, Any]:
        imported = self.importer.import_new()
        poll_result = self.polling_runner.run_once()
        processed = poll_result.get("processed", [])
        processed_count = len(processed) if isinstance(processed, list) else 0
        status = _tick_status(imported, poll_result, processed_count)
        return {
            "status": status,
            "hook_event_file": str(self.hook_event_file),
            "backend_event_file": str(self.backend_event_file),
            "import": _import_payload(imported),
            "queue": self.queue_status(imported),
            "processed_count": processed_count,
            "processed": processed if isinstance(processed, list) else [],
            "poll": {
                key: value
                for key, value in poll_result.items()
                if key not in {"processed"}
            },
            "send_enabled": False,
        }

    def run_forever(self, max_loops: int | None = None) -> dict[str, Any]:
        loops = 0
        imported_count = 0
        processed_count = 0
        processed: list[dict[str, Any]] = []
        last_tick: dict[str, Any] | None = None
        while max_loops is None or loops < max_loops:
            tick = self.run_once()
            loops += 1
            imported_count += int(tick.get("import", {}).get("appended_count", 0) or 0)
            processed_count += int(tick.get("processed_count", 0) or 0)
            processed.extend([item for item in tick.get("processed", []) if isinstance(item, dict)])
            last_tick = tick
            if max_loops is None or loops < max_loops:
                time.sleep(self.polling_runner.poll_interval_seconds)
        return {
            "status": "stopped",
            "loops": loops,
            "last_status": str(last_tick.get("status", "unknown")) if last_tick else "not_started",
            "hook_event_file": str(self.hook_event_file),
            "backend_event_file": str(self.backend_event_file),
            "imported_count": imported_count,
            "processed_count": processed_count,
            "queue": last_tick.get("queue", {}) if last_tick else self.queue_status(None),
            "last_import": last_tick.get("import", {}) if last_tick else {},
            "last_poll": last_tick.get("poll", {}) if last_tick else {},
            "processed": processed,
            "send_enabled": False,
        }

    def queue_status(self, imported: HookImportResult | None) -> dict[str, Any]:
        source_offset = int(imported.source_offset) if imported is not None else _state_offset(self.importer.state_path, self.hook_event_file)
        backend_event_count = (
            int(imported.backend_event_count)
            if imported is not None and imported.backend_event_count
            else _jsonl_line_count(self.backend_event_file)
        )
        seen_event_count = len(getattr(self.polling_runner.driver, "_seen_event_ids", set()))
        seen_message_count = len(getattr(self.polling_runner.driver, "_seen_message_raw_ids", set()))
        return {
            "source_exists": self.hook_event_file.exists(),
            "source_size": self.hook_event_file.stat().st_size if self.hook_event_file.exists() else 0,
            "source_offset": source_offset,
            "backend_event_count": backend_event_count,
            "backend_driver_seen_event_count": seen_event_count,
            "backend_driver_seen_message_count": seen_message_count,
            "estimated_backend_events_unread": max(0, backend_event_count - seen_event_count),
        }


def _tick_status(imported: HookImportResult, poll_result: dict[str, Any], processed_count: int) -> str:
    poll_status = str(poll_result.get("status") or "unknown")
    if imported.status == "missing_source" and processed_count == 0:
        return "waiting_for_hook_source"
    if imported.error_count:
        return "partial_error"
    if poll_status != "ok":
        return poll_status
    return "ok"


def _import_payload(result: HookImportResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "source_path": result.source_path,
        "backend_event_path": result.backend_event_path,
        "scanned_count": result.scanned_count,
        "appended_count": result.appended_count,
        "skipped_count": result.skipped_count,
        "error_count": result.error_count,
        "appended_raw_ids": list(result.appended_raw_ids),
        "errors": list(result.errors),
        "source_offset": result.source_offset,
        "backend_event_count": result.backend_event_count,
    }


def _state_offset(state_path: Path, source_path: Path) -> int:
    if not state_path.exists():
        return 0
    try:
        import json

        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        return int(payload.get(str(source_path), 0) or 0)
    except Exception:
        return 0


def _jsonl_line_count(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
    except OSError:
        return 0
    return count

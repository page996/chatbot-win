from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque


DEFAULT_MAX_RECENT_TICKS = 50
DEFAULT_STALL_THRESHOLD_SECONDS = 120.0
DEFAULT_SLOW_TICK_SECONDS = 20.0


@dataclass(frozen=True)
class WeflowTickRecord:
    """One background pull tick, kept for stability observation."""

    tick_index: int
    at: float
    duration_seconds: float
    status: str
    source_status: str = ""
    session_count: int = 0
    scanned_count: int = 0
    appended_count: int = 0
    imported_count: int = 0
    processed_count: int = 0
    error_count: int = 0
    slow: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick_index": self.tick_index,
            "at": round(self.at, 3),
            "duration_seconds": round(self.duration_seconds, 3),
            "status": self.status,
            "source_status": self.source_status,
            "session_count": self.session_count,
            "scanned_count": self.scanned_count,
            "appended_count": self.appended_count,
            "imported_count": self.imported_count,
            "processed_count": self.processed_count,
            "error_count": self.error_count,
            "slow": self.slow,
            "error": self.error,
        }


@dataclass
class WeflowWorkerMetrics:
    """Accumulate background WeFlow pull activity for the sidebar.

    The worker thread calls :meth:`record_tick` / :meth:`record_error` once per
    loop; the sidebar HTTP thread calls :meth:`snapshot`. Both are cheap and
    hold no locks themselves, so the caller must guard shared access (the
    sidebar guards with ``_WEFLOW_LOCK``).
    """

    started_at: float = field(default_factory=time.time)
    max_recent_ticks: int = DEFAULT_MAX_RECENT_TICKS
    slow_tick_seconds: float = DEFAULT_SLOW_TICK_SECONDS
    loops: int = 0
    error_ticks: int = 0
    slow_ticks: int = 0
    totals: dict[str, int] = field(
        default_factory=lambda: {
            "scanned": 0,
            "appended": 0,
            "imported": 0,
            "processed": 0,
            "errors": 0,
        }
    )
    last_status: str = ""
    last_error: str = ""
    last_tick_at: float = 0.0
    last_success_at: float = 0.0
    last_progress_at: float = 0.0
    max_tick_duration_seconds: float = 0.0
    recent: Deque[WeflowTickRecord] = field(default_factory=deque)

    def record_tick(self, result: dict[str, Any], duration_seconds: float, *, now: float | None = None) -> WeflowTickRecord:
        now = time.time() if now is None else now
        duration = max(0.0, float(duration_seconds))
        status = str(result.get("status") or "unknown")
        source = result.get("source") if isinstance(result.get("source"), dict) else {}
        pull = result.get("pull") if isinstance(result.get("pull"), dict) else {}
        import_payload = pull.get("import") if isinstance(pull.get("import"), dict) else {}
        source_status = str(source.get("status") or "")
        scanned = _int(source.get("scanned_count"))
        appended = _int(source.get("appended_count"))
        imported = _int(import_payload.get("appended_count"))
        processed = _int(pull.get("processed_count"))
        error_count = len(source.get("errors", [])) if isinstance(source.get("errors"), list) else 0
        error_count += _int(import_payload.get("error_count"))
        slow = duration >= self.slow_tick_seconds
        record = WeflowTickRecord(
            tick_index=self.loops + 1,
            at=now,
            duration_seconds=duration,
            status=status,
            source_status=source_status,
            session_count=_int(source.get("session_count")),
            scanned_count=scanned,
            appended_count=appended,
            imported_count=imported,
            processed_count=processed,
            error_count=error_count,
            slow=slow,
        )
        self._append(record, now=now)
        self.totals["scanned"] += scanned
        self.totals["appended"] += appended
        self.totals["imported"] += imported
        self.totals["processed"] += processed
        self.totals["errors"] += error_count
        if error_count:
            self.error_ticks += 1
        if status in {"ok"} and source_status in {"", "ok"}:
            self.last_success_at = now
        if appended or processed or imported:
            self.last_progress_at = now
        return record

    def record_error(self, error: str, duration_seconds: float, *, now: float | None = None) -> WeflowTickRecord:
        now = time.time() if now is None else now
        duration = max(0.0, float(duration_seconds))
        slow = duration >= self.slow_tick_seconds
        record = WeflowTickRecord(
            tick_index=self.loops + 1,
            at=now,
            duration_seconds=duration,
            status="error",
            source_status="error",
            error_count=1,
            slow=slow,
            error=str(error),
        )
        self._append(record, now=now)
        self.totals["errors"] += 1
        self.error_ticks += 1
        self.last_error = str(error)
        return record

    def _append(self, record: WeflowTickRecord, *, now: float) -> None:
        self.loops += 1
        self.last_status = record.status
        self.last_tick_at = now
        if record.error:
            self.last_error = record.error
        if record.slow:
            self.slow_ticks += 1
        self.max_tick_duration_seconds = max(self.max_tick_duration_seconds, record.duration_seconds)
        self.recent.append(record)
        while len(self.recent) > max(1, self.max_recent_ticks):
            self.recent.popleft()

    def snapshot(
        self,
        *,
        running: bool,
        now: float | None = None,
        stall_threshold_seconds: float = DEFAULT_STALL_THRESHOLD_SECONDS,
        recent_limit: int = 10,
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        seconds_since_tick = (now - self.last_tick_at) if self.last_tick_at else None
        seconds_since_success = (now - self.last_success_at) if self.last_success_at else None
        seconds_since_progress = (now - self.last_progress_at) if self.last_progress_at else None
        # For stall detection, a worker that has *never* succeeded is measured
        # from when it started, so a puller that errors from the first tick is
        # still flagged once it has been running past the threshold.
        success_reference = self.last_success_at or self.started_at
        seconds_without_success = (now - success_reference) if success_reference else None
        stalled = bool(
            running
            and self.last_tick_at
            and seconds_without_success is not None
            and seconds_without_success > stall_threshold_seconds
        )
        recent = list(self.recent)[-max(0, recent_limit):]
        return {
            "running": running,
            "started_at": round(self.started_at, 3),
            "uptime_seconds": round(now - self.started_at, 3) if self.started_at else 0.0,
            "loops": self.loops,
            "error_ticks": self.error_ticks,
            "slow_ticks": self.slow_ticks,
            "totals": dict(self.totals),
            "last_status": self.last_status,
            "last_error": self.last_error,
            "last_tick_at": round(self.last_tick_at, 3) if self.last_tick_at else 0.0,
            "last_success_at": round(self.last_success_at, 3) if self.last_success_at else 0.0,
            "last_progress_at": round(self.last_progress_at, 3) if self.last_progress_at else 0.0,
            "seconds_since_tick": round(seconds_since_tick, 3) if seconds_since_tick is not None else None,
            "seconds_since_success": round(seconds_since_success, 3) if seconds_since_success is not None else None,
            "seconds_since_progress": round(seconds_since_progress, 3) if seconds_since_progress is not None else None,
            "max_tick_duration_seconds": round(self.max_tick_duration_seconds, 3),
            "stalled": stalled,
            "stall_threshold_seconds": stall_threshold_seconds,
            "recent_ticks": [record.to_dict() for record in recent],
        }


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

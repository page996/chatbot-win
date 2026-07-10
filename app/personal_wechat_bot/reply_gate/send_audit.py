from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.logging.jsonl_rotation import append_line_with_rotation_unlocked, jsonl_operation_lock
from app.personal_wechat_bot.memory.sqlite_utils import connect


SCHEMA_VERSION = 1


class SendAuditLog:
    """Append-only send audit with a SQLite read/index authority.

    The JSONL file remains the forensic evidence stream. SQLite mirrors it for
    fast, WAL-protected reads and compact sidebar projections, and imports old
    JSONL records on first use.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = self.path.with_suffix(".sqlite")

    def append(
        self,
        action: str,
        *,
        queue_id: str = "",
        status: str = "",
        reason: str = "",
        reviewer: str = "",
        note: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "timestamp": utc_now_iso(),
            "action": action,
            "queue_id": queue_id,
            "status": status,
            "reason": reason,
            "reviewer": reviewer,
            "note": note,
            "payload": payload or {},
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._audit_lock():
            self._ensure_schema_unlocked()
            append_line_with_rotation_unlocked(self.path, line)
            self._append_index_unlocked(record)
        return record

    def list_recent(
        self,
        *,
        limit: int = 20,
        status: str | None = None,
        include_resolved: bool = False,
        compact_transitions: bool = False,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        records = self._annotate_resolved_failures(self._read_all())
        if status:
            records = [item for item in records if item.get("status") == status]
        if not include_resolved:
            records = [
                item
                for item in records
                if not (item.get("action") == "ledger_sync_failed" and item.get("resolved"))
            ]
        if compact_transitions:
            records = _compact_audit_transitions(records)
        return records[-limit:]

    def has_unresolved_ledger_sync_failure(self, *, queue_id: str, status: str | None = None) -> bool:
        queue_id = str(queue_id or "").strip()
        if not queue_id:
            return False
        for item in self._annotate_resolved_failures(self._read_all()):
            if item.get("action") != "ledger_sync_failed":
                continue
            if item.get("resolved"):
                continue
            if str(item.get("queue_id", "")) != queue_id:
                continue
            if status is not None and str(item.get("status", "")) != str(status):
                continue
            return True
        return False

    def clear(self) -> int:
        with self._audit_lock():
            records = self._read_all_unlocked()
            self.path.write_text("", encoding="utf-8")
            self._clear_index_unlocked()
        return len(records)

    def _read_all(self) -> list[dict[str, Any]]:
        with self._audit_lock():
            return self._read_all_unlocked()

    def _read_all_unlocked(self) -> list[dict[str, Any]]:
        self._ensure_schema_unlocked()
        records: list[dict[str, Any]] = []
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT record_json
                FROM send_audit_events
                ORDER BY id ASC
                """
            ).fetchall()
        for row in rows:
            try:
                item = json.loads(str(row["record_json"] or "{}"))
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records

    def _read_legacy_jsonl_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
        return records

    def _audit_lock(self):
        return jsonl_operation_lock(self.path, timeout_seconds=15.0)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = connect(self.db_path, busy_timeout_ms=30000)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _append_index_unlocked(self, record: dict[str, Any]) -> None:
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO send_audit_events(
                  timestamp, action, queue_id, status, reason, reviewer, note, record_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.get("timestamp") or utc_now_iso()),
                    str(record.get("action") or ""),
                    str(record.get("queue_id") or ""),
                    str(record.get("status") or ""),
                    str(record.get("reason") or ""),
                    str(record.get("reviewer") or ""),
                    str(record.get("note") or ""),
                    _json_dumps(record),
                ),
            )

    def _clear_index_unlocked(self) -> None:
        self._ensure_schema_unlocked()
        with self._connection() as db:
            db.execute("DELETE FROM send_audit_events")
            db.execute("INSERT OR REPLACE INTO send_audit_meta(key, value) VALUES ('legacy_jsonl_imported', '1')")

    def _ensure_schema_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = connect(self.db_path, busy_timeout_ms=30000)
        db.row_factory = sqlite3.Row
        try:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS send_audit_meta(
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS send_audit_events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  timestamp TEXT NOT NULL,
                  action TEXT NOT NULL,
                  queue_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  reviewer TEXT NOT NULL,
                  note TEXT NOT NULL,
                  record_json TEXT NOT NULL
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_send_audit_events_status ON send_audit_events(status)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_send_audit_events_queue ON send_audit_events(queue_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_send_audit_events_timestamp ON send_audit_events(timestamp)")
            db.execute(
                "INSERT OR REPLACE INTO send_audit_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            count = db.execute("SELECT COUNT(*) FROM send_audit_events").fetchone()[0]
            imported = db.execute("SELECT value FROM send_audit_meta WHERE key = 'legacy_jsonl_imported'").fetchone()
            db.commit()
        finally:
            db.close()
        if int(count or 0) == 0 and (imported is None or str(imported["value"]) != "1"):
            legacy_records = self._read_legacy_jsonl_unlocked()
            with self._connection() as writable:
                for item in legacy_records:
                    if not isinstance(item, dict):
                        continue
                    writable.execute(
                        """
                        INSERT INTO send_audit_events(
                          timestamp, action, queue_id, status, reason, reviewer, note, record_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(item.get("timestamp") or utc_now_iso()),
                            str(item.get("action") or ""),
                            str(item.get("queue_id") or ""),
                            str(item.get("status") or ""),
                            str(item.get("reason") or ""),
                            str(item.get("reviewer") or ""),
                            str(item.get("note") or ""),
                            _json_dumps(item),
                        ),
                    )
                writable.execute(
                    "INSERT OR REPLACE INTO send_audit_meta(key, value) VALUES ('legacy_jsonl_imported', '1')"
                )

    def _annotate_resolved_failures(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        annotated = [dict(item) for item in records]
        for index, item in enumerate(annotated):
            if item.get("action") != "ledger_sync_failed":
                item.setdefault("resolved", False)
                _annotate_audit_severity(item)
                continue
            queue_id = str(item.get("queue_id", "") or "").strip()
            item["resolved"] = False
            if not queue_id:
                _annotate_audit_severity(item)
                continue
            for later in annotated[index + 1 :]:
                if str(later.get("queue_id", "") or "").strip() != queue_id:
                    continue
                if not _audit_event_resolves_ledger_failure(item, later):
                    continue
                item["resolved"] = True
                item["resolved_by"] = str(later.get("action", "") or "")
                item["resolved_at"] = str(later.get("timestamp", "") or "")
                break
            _annotate_audit_severity(item)
        return annotated


def _annotate_audit_severity(item: dict[str, Any]) -> None:
    """Add UI-facing semantics without changing the append-only audit record.

    Older sidebar code only had ``resolved`` and had to infer meaning from an
    action/status pair. That made normal queue transitions like approve/remove
    look too much like unresolved failures in compact history views.
    """

    action = str(item.get("action", "") or "")
    status = str(item.get("status", "") or "")
    resolved = bool(item.get("resolved"))
    if action == "ledger_sync_failed":
        item["audit_state"] = "resolved_error" if resolved else "open_error"
        item["severity"] = "resolved" if resolved else "error"
        item["problem"] = not resolved
        return
    if action == "confirm_send_attempt" and status == "failed":
        item["audit_state"] = "open_error"
        item["severity"] = "error"
        item["problem"] = True
        return
    if action == "confirm_send_blocked":
        item["audit_state"] = "blocked"
        item["severity"] = "warning"
        item["problem"] = False
        return
    if action == "bridge_ack_sync":
        if status == "failed":
            item["audit_state"] = "open_error"
            item["severity"] = "error"
            item["problem"] = True
        elif status == "accepted":
            item["audit_state"] = "accepted_unverified"
            item["severity"] = "warning"
            item["problem"] = False
        else:
            item["audit_state"] = "history"
            item["severity"] = "info"
            item["problem"] = False
        return
    item["audit_state"] = "history"
    item["severity"] = "info"
    item["problem"] = False


def _audit_event_resolves_ledger_failure(failed: dict[str, Any], later: dict[str, Any]) -> bool:
    action = str(later.get("action", "") or "")
    if action == "ledger_sync_recovered":
        return True
    if action in {"confirm_remove", "confirm_reject"}:
        return True
    if action != "confirm_send_attempt":
        return False
    failed_status = str(failed.get("status", "") or "")
    later_status = str(later.get("status", "") or "")
    if later_status == "sent":
        return True
    return failed_status in {"pending", "approved"} and later_status in {"queued_to_bridge", "failed"}


def _compact_audit_transitions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep unresolved errors plus the latest visible event per queue item.

    The audit file is append-only and intentionally records every transition:
    approved -> queued -> removed, ledger sync failure -> recovery, etc. That is
    useful evidence, but the sidebar's compact status panel reads as if old
    transition rows are still current. This projection keeps the file intact
    while showing one effective state per queue id by default.
    """

    latest_by_queue: dict[str, int] = {}
    keep_indexes: set[int] = set()
    for index, item in enumerate(records):
        queue_id = str(item.get("queue_id", "") or "").strip()
        action = str(item.get("action", "") or "")
        if action == "ledger_sync_failed" and not item.get("resolved"):
            keep_indexes.add(index)
            continue
        if not queue_id:
            keep_indexes.add(index)
            continue
        latest_by_queue[queue_id] = index
    keep_indexes.update(latest_by_queue.values())
    return [item for index, item in enumerate(records) if index in keep_indexes]


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

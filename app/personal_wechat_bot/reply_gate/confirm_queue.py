from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import asdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from app.personal_wechat_bot.domain.models import ReplyCandidate, utc_now_iso
from app.personal_wechat_bot.memory.sqlite_utils import connect
from app.personal_wechat_bot.runtime.process_lock import blocking_process_lock


SCHEMA_VERSION = 1

_ALLOWED_TRANSITIONS = {
    "pending": {"approved", "rejected"},
    "approved": {"rejected", "queued_to_bridge", "sent", "accepted", "failed"},
    "queued_to_bridge": {"sent", "accepted", "failed"},
    "accepted": {"sent", "failed"},
    # A durable bridge "sent" ack is stronger than a previous local failure
    # marker: the message is on the wire, so reconciliation must be able to
    # repair the queue state.
    "failed": {"sent", "accepted"},
}


class ConfirmQueue:
    """SQLite-backed authority for the human confirmation queue.

    ``confirm_queue.jsonl`` remains a compatibility projection for older
    diagnostics and operator inspection, but the state machine now reads and
    writes ``confirm_queue.sqlite``. On first use, any legacy JSONL records are
    imported into SQLite before normal operations continue.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = self.path.with_suffix(".sqlite")

    def enqueue(self, reply: ReplyCandidate) -> str:
        queue_id = f"{reply.message_id}:{reply.created_at}"
        record = {
            "queue_id": queue_id,
            "status": "pending",
            "created_at": utc_now_iso(),
            "reply": asdict(reply),
        }
        with self._queue_lock():
            self._ensure_schema_unlocked()
            self._append_unlocked(record)
            self._write_projection_unlocked(self._read_all_unlocked())
        return queue_id

    def list_pending(self) -> list[dict[str, Any]]:
        return self.list_by_status("pending")

    def list_by_status(self, status: str) -> list[dict[str, Any]]:
        return [item for item in self._read_all() if item.get("status") == status]

    def get(self, queue_id: str) -> dict[str, Any] | None:
        for item in self._read_all():
            if item.get("queue_id") == queue_id:
                return item
        return None

    def find_by_bridge_id(self, bridge_id: str) -> dict[str, Any] | None:
        bridge_id = str(bridge_id or "").strip()
        if not bridge_id:
            return None
        for item in reversed(self._read_all()):
            if _queue_item_references_bridge(item, bridge_id):
                return item
        return None

    def approve(self, queue_id: str, *, reviewer: str = "local_user", note: str = "") -> dict[str, Any]:
        return self._transition(queue_id, "approved", reviewer=reviewer, note=note)

    def reject(self, queue_id: str, *, reviewer: str = "local_user", note: str = "") -> dict[str, Any]:
        return self._transition(queue_id, "rejected", reviewer=reviewer, note=note)

    def mark_send_result(
        self,
        queue_id: str,
        status: str,
        reason: str,
        *,
        reviewer: str = "local_user",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"queued_to_bridge", "sent", "accepted", "failed"}:
            raise ValueError("status must be queued_to_bridge, sent, accepted, or failed")
        return self._transition(queue_id, status, reviewer=reviewer, note=reason, extra=extra)

    def requeue_bridge_result(
        self,
        old_bridge_id: str,
        new_bridge_id: str,
        reason: str,
        *,
        reviewer: str = "local_user",
    ) -> dict[str, Any] | None:
        old_bridge_id = str(old_bridge_id or "").strip()
        new_bridge_id = str(new_bridge_id or "").strip()
        if not old_bridge_id or not new_bridge_id:
            return None
        with self._queue_lock():
            records = self._read_all_unlocked()
            changed: dict[str, Any] | None = None
            for item in reversed(records):
                note = str(item.get("note", ""))
                if old_bridge_id not in note:
                    continue
                if str(item.get("status") or "") in {"sent", "rejected"} and "dry_run_not_delivered" not in note:
                    return None
                item["status"] = "queued_to_bridge"
                item["reviewed_at"] = utc_now_iso()
                item["reviewer"] = reviewer
                item["note"] = reason or f"retry_to_non_foreground_bridge:{new_bridge_id}"
                item["retry_of"] = old_bridge_id
                changed = item
                break
            if changed is None:
                return None
            self._write_all_unlocked(records)
            return changed

    def update_referencing_bridge(
        self,
        bridge_id: str,
        updater: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
        """Atomically update the newest queue item that references a bridge id.

        Bridge text/file acks can arrive concurrently. The callback runs while
        the queue file lock is held so multipart ack maps are merged from the
        latest on-disk state instead of a stale pre-lock snapshot.
        """

        bridge_id = str(bridge_id or "").strip()
        if not bridge_id:
            return None, None, False
        with self._queue_lock():
            records = self._read_all_unlocked()
            for index in range(len(records) - 1, -1, -1):
                item = records[index]
                if not _queue_item_references_bridge(item, bridge_id):
                    continue
                original = deepcopy(item)
                updated = updater(deepcopy(item))
                if updated is None:
                    return original, None, False
                if not isinstance(updated, dict):
                    raise ValueError("confirm queue updater must return a dict or None")
                _validate_transition(str(item.get("status", "")), str(updated.get("status", "")))
                if updated == item:
                    return original, updated, False
                records[index] = updated
                self._write_all_unlocked(records)
                return original, updated, True
        return None, None, False

    def remove(self, queue_id: str) -> dict[str, Any]:
        with self._queue_lock():
            records = self._read_all_unlocked()
            kept: list[dict[str, Any]] = []
            removed: dict[str, Any] | None = None
            for item in records:
                if item.get("queue_id") == queue_id and removed is None:
                    removed = item
                    continue
                kept.append(item)
            if removed is None:
                raise KeyError(f"queue_id not found: {queue_id}")
            self._write_all_unlocked(kept)
            return removed

    def _transition(
        self,
        queue_id: str,
        status: str,
        *,
        reviewer: str,
        note: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._queue_lock():
            records = self._read_all_unlocked()
            changed: dict[str, Any] | None = None
            for item in records:
                if item.get("queue_id") != queue_id:
                    continue
                current_status = str(item.get("status", ""))
                _validate_transition(current_status, status)
                item["status"] = status
                item["reviewed_at"] = utc_now_iso()
                item["reviewer"] = reviewer
                item["note"] = note
                if extra:
                    for key, value in extra.items():
                        item[str(key)] = value
                changed = item
                break
            if changed is None:
                raise KeyError(f"queue_id not found: {queue_id}")
            self._write_all_unlocked(records)
            return changed

    def _queue_lock(self):
        return blocking_process_lock(
            self.path.with_suffix(self.path.suffix + ".lock"),
            label="confirm_queue_rw",
            stale_after_seconds=30.0,
            wait_timeout_seconds=15.0,
        )

    def _append_unlocked(self, record: dict[str, Any]) -> None:
        self._ensure_schema_unlocked()
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO confirm_queue_items(queue_id, status, created_at, updated_at, record_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(record.get("queue_id") or ""),
                    str(record.get("status") or ""),
                    str(record.get("created_at") or utc_now_iso()),
                    utc_now_iso(),
                    _json_dumps(record),
                ),
            )

    def _read_all(self) -> list[dict[str, Any]]:
        with self._queue_lock():
            return self._read_all_unlocked()

    def _read_all_unlocked(self) -> list[dict[str, Any]]:
        self._ensure_schema_unlocked()
        records: list[dict[str, Any]] = []
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT record_json
                FROM confirm_queue_items
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

    def _write_all(self, records: list[dict[str, Any]]) -> None:
        with self._queue_lock():
            self._write_all_unlocked(records)

    def _write_all_unlocked(self, records: list[dict[str, Any]]) -> None:
        self._ensure_schema_unlocked()
        with self._connection() as db:
            db.execute("DELETE FROM confirm_queue_items")
            for item in records:
                if not isinstance(item, dict):
                    continue
                queue_id = str(item.get("queue_id") or "").strip()
                if not queue_id:
                    continue
                db.execute(
                    """
                    INSERT INTO confirm_queue_items(queue_id, status, created_at, updated_at, record_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        queue_id,
                        str(item.get("status") or ""),
                        str(item.get("created_at") or utc_now_iso()),
                        utc_now_iso(),
                        _json_dumps(item),
                    ),
                )
        self._write_projection_unlocked(records)

    def _write_projection_unlocked(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for item in records:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        tmp.replace(self.path)

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

    def _ensure_schema_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = connect(self.db_path, busy_timeout_ms=30000)
        db.row_factory = sqlite3.Row
        try:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS confirm_queue_meta(
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS confirm_queue_items(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  queue_id TEXT NOT NULL UNIQUE,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  record_json TEXT NOT NULL
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_confirm_queue_items_status ON confirm_queue_items(status)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_confirm_queue_items_created ON confirm_queue_items(created_at)"
            )
            db.execute(
                "INSERT OR REPLACE INTO confirm_queue_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            count = db.execute("SELECT COUNT(*) FROM confirm_queue_items").fetchone()[0]
            imported = db.execute(
                "SELECT value FROM confirm_queue_meta WHERE key = 'legacy_jsonl_imported'"
            ).fetchone()
            db.commit()
        finally:
            db.close()
        if int(count or 0) == 0 and (imported is None or str(imported["value"]) != "1"):
            legacy_records = self._read_legacy_jsonl_unlocked()
            if legacy_records:
                with self._connection() as writable:
                    for item in legacy_records:
                        if not isinstance(item, dict):
                            continue
                        queue_id = str(item.get("queue_id") or "").strip()
                        if not queue_id:
                            continue
                        writable.execute(
                            """
                            INSERT OR IGNORE INTO confirm_queue_items(
                              queue_id, status, created_at, updated_at, record_json
                            )
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                queue_id,
                                str(item.get("status") or ""),
                                str(item.get("created_at") or utc_now_iso()),
                                utc_now_iso(),
                                _json_dumps(item),
                            ),
                        )
                    writable.execute(
                        "INSERT OR REPLACE INTO confirm_queue_meta(key, value) VALUES ('legacy_jsonl_imported', '1')"
                    )
            else:
                with self._connection() as writable:
                    writable.execute(
                        "INSERT OR REPLACE INTO confirm_queue_meta(key, value) VALUES ('legacy_jsonl_imported', '1')"
                    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _queue_item_references_bridge(item: dict[str, Any], bridge_id: str) -> bool:
    note = str(item.get("note", ""))
    if bridge_id in note:
        return True
    retry_of = str(item.get("retry_of", ""))
    if retry_of == bridge_id:
        return True
    bridge_ids = item.get("bridge_ids") if isinstance(item.get("bridge_ids"), list) else []
    if bridge_id in {str(value) for value in bridge_ids}:
        return True
    bridge_acks = item.get("bridge_acks") if isinstance(item.get("bridge_acks"), dict) else {}
    if bridge_id in {str(value) for value in bridge_acks.keys()}:
        return True
    last_ack = item.get("last_bridge_ack") if isinstance(item.get("last_bridge_ack"), dict) else {}
    if str(last_ack.get("bridge_id") or "") == bridge_id:
        return True
    send_result = item.get("send_result") if isinstance(item.get("send_result"), dict) else {}
    return _send_result_references_bridge(send_result, bridge_id)


def _send_result_references_bridge(send_result: dict[str, Any], bridge_id: str) -> bool:
    if str(send_result.get("message_id") or "") == bridge_id:
        return True
    if bridge_id in str(send_result.get("reason") or ""):
        return True
    details = send_result.get("details") if isinstance(send_result.get("details"), dict) else {}
    bridge_ids = details.get("bridge_ids") if isinstance(details.get("bridge_ids"), list) else []
    if bridge_id in {str(value) for value in bridge_ids}:
        return True
    text = details.get("text") if isinstance(details.get("text"), dict) else {}
    if str(text.get("message_id") or text.get("bridge_id") or "") == bridge_id:
        return True
    files = details.get("files") if isinstance(details.get("files"), list) else []
    for file_detail in files:
        if not isinstance(file_detail, dict):
            continue
        if str(file_detail.get("message_id") or file_detail.get("bridge_id") or "") == bridge_id:
            return True
        if bridge_id in str(file_detail.get("reason") or ""):
            return True
    return False


def _validate_transition(current_status: str, status: str) -> None:
    if current_status != status and status not in _ALLOWED_TRANSITIONS.get(current_status, set()):
        raise ValueError(f"invalid confirm queue transition: {current_status} -> {status}")

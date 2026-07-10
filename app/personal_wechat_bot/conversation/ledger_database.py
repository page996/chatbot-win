from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.memory.sqlite_utils import connect


SCHEMA_VERSION = 1


class ConversationLedgerDatabase:
    """SQLite authority for ordered conversation ledger entries."""

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir)
        self.path = self.root / "conversation_ledger.sqlite"
        self._ensure_schema()

    def list_entries(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT payload_json
                FROM ledger_entries
                WHERE conversation_id = ?
                ORDER BY sequence ASC, created_at ASC, entry_id ASC
                """,
                (str(conversation_id or ""),),
            ).fetchall()
        entries: list[dict[str, Any]] = []
        for row in rows:
            payload = _payload_from_row(row)
            if payload is not None:
                entries.append(payload)
        return entries

    def list_conversation_ids(self) -> list[str]:
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT conversation_id FROM ledger_conversations
                UNION
                SELECT conversation_id FROM ledger_entries
                ORDER BY conversation_id ASC
                """
            ).fetchall()
        return [str(row["conversation_id"] or "") for row in rows if str(row["conversation_id"] or "")]

    def upsert_entry(self, payload: dict[str, Any]) -> None:
        item = dict(payload)
        conversation_id = str(item.get("conversation_id") or "").strip()
        entry_id = str(item.get("entry_id") or "").strip()
        if not conversation_id or not entry_id:
            raise ValueError("conversation_id and entry_id are required")
        with self._connection() as db:
            self._upsert(db, item)

    def replace_entries(self, conversation_id: str, entries: list[dict[str, Any]]) -> None:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            raise ValueError("conversation_id is required")
        with self._connection() as db:
            db.execute("DELETE FROM ledger_entries WHERE conversation_id = ?", (conversation_id,))
            for payload in entries:
                item = {**dict(payload), "conversation_id": conversation_id}
                self._upsert(db, item)

    def segment_for(self, conversation_id: str) -> str:
        with self._connection() as db:
            row = db.execute(
                "SELECT segment FROM ledger_conversations WHERE conversation_id = ?",
                (str(conversation_id or ""),),
            ).fetchone()
        return str(row["segment"] or "") if row is not None else ""

    def set_segment(self, conversation_id: str, segment: str) -> None:
        conversation_id = str(conversation_id or "").strip()
        segment = str(segment or "").strip()
        if not conversation_id or not segment:
            return
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO ledger_conversations(conversation_id, segment, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                  segment=excluded.segment,
                  updated_at=excluded.updated_at
                """,
                (conversation_id, segment, utc_now_iso()),
            )

    def delete_conversation(self, conversation_id: str) -> bool:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return False
        with self._connection() as db:
            entries = db.execute(
                "DELETE FROM ledger_entries WHERE conversation_id = ?",
                (conversation_id,),
            ).rowcount
            metadata = db.execute(
                "DELETE FROM ledger_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).rowcount
        return bool(entries or metadata)

    def _upsert(self, db: sqlite3.Connection, item: dict[str, Any]) -> None:
        entry_id = str(item.get("entry_id") or "").strip()
        if not entry_id:
            raise ValueError("entry_id is required")
        db.execute(
            """
            INSERT INTO ledger_entries(
              conversation_id, entry_id, sequence, session_id, status, role,
              created_at, updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id, entry_id) DO UPDATE SET
              sequence=excluded.sequence,
              session_id=excluded.session_id,
              status=excluded.status,
              role=excluded.role,
              created_at=excluded.created_at,
              updated_at=excluded.updated_at,
              payload_json=excluded.payload_json
            """,
            (
                str(item.get("conversation_id") or ""),
                entry_id,
                int(item.get("sequence", 0) or 0),
                str(item.get("session_id") or ""),
                str(item.get("status") or "active"),
                str(item.get("role") or "user"),
                str(item.get("created_at") or utc_now_iso()),
                str(item.get("updated_at") or utc_now_iso()),
                json.dumps(item, ensure_ascii=False, sort_keys=True),
            ),
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.root.mkdir(parents=True, exist_ok=True)
        db = connect(self.path, busy_timeout_ms=30000)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _ensure_schema(self) -> None:
        with self._connection() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_meta(
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_conversations(
                  conversation_id TEXT PRIMARY KEY,
                  segment TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_entries(
                  conversation_id TEXT NOT NULL,
                  entry_id TEXT NOT NULL,
                  sequence INTEGER NOT NULL,
                  session_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  role TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  PRIMARY KEY(conversation_id, entry_id)
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_entries_order ON ledger_entries(conversation_id, sequence)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_entries_sequence ON ledger_entries(conversation_id, sequence)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_entries_session ON ledger_entries(conversation_id, session_id, sequence)"
            )
            db.execute(
                "INSERT OR REPLACE INTO ledger_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )


def _payload_from_row(row: sqlite3.Row) -> dict[str, Any] | None:
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None

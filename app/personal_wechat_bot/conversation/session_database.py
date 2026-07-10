from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.memory.sqlite_utils import connect


SCHEMA_VERSION = 1


class ConversationSessionDatabase:
    """SQLite authority for current-session pointers and reset events."""

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir)
        self.path = self.root / "conversation_sessions.sqlite"
        self._ensure_schema()

    def get_state(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT payload_json FROM conversation_session_states WHERE conversation_id = ?",
                (str(conversation_id or ""),),
            ).fetchone()
        return _payload_from_row(row)

    def upsert_state(self, conversation_id: str, segment: str, payload: dict[str, Any]) -> dict[str, Any]:
        item = dict(payload)
        conversation_id = str(conversation_id or item.get("conversation_id") or "").strip()
        if not conversation_id:
            raise ValueError("conversation_id is required")
        item["conversation_id"] = conversation_id
        item.setdefault("updated_at", utc_now_iso())
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO conversation_session_states(
                  conversation_id, segment, current_session_id, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                  segment=excluded.segment,
                  current_session_id=excluded.current_session_id,
                  updated_at=excluded.updated_at,
                  payload_json=excluded.payload_json
                """,
                (
                    conversation_id,
                    str(segment or ""),
                    str(item.get("current_session_id") or ""),
                    str(item.get("updated_at") or utc_now_iso()),
                    json.dumps(item, ensure_ascii=False, sort_keys=True),
                ),
            )
        return item

    def segment_for(self, conversation_id: str) -> str:
        with self._connection() as db:
            row = db.execute(
                "SELECT segment FROM conversation_session_states WHERE conversation_id = ?",
                (str(conversation_id or ""),),
            ).fetchone()
        return str(row["segment"] or "") if row is not None else ""

    def append_event(self, payload: dict[str, Any]) -> None:
        item = dict(payload)
        conversation_id = str(item.get("conversation_id") or "").strip()
        if not conversation_id:
            raise ValueError("conversation_id is required")
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO conversation_session_events(
                  conversation_id, session_id, event_type, created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    str(item.get("session_id") or ""),
                    str(item.get("type") or ""),
                    str(item.get("created_at") or utc_now_iso()),
                    json.dumps(item, ensure_ascii=False, sort_keys=True),
                ),
            )

    def list_events(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT payload_json
                FROM conversation_session_events
                WHERE conversation_id = ?
                ORDER BY event_id ASC
                """,
                (str(conversation_id or ""),),
            ).fetchall()
        return [payload for row in rows if (payload := _payload_from_row(row)) is not None]

    def delete_conversation(self, conversation_id: str) -> bool:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return False
        with self._connection() as db:
            events = db.execute(
                "DELETE FROM conversation_session_events WHERE conversation_id = ?",
                (conversation_id,),
            ).rowcount
            state = db.execute(
                "DELETE FROM conversation_session_states WHERE conversation_id = ?",
                (conversation_id,),
            ).rowcount
        return bool(events or state)

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
                "CREATE TABLE IF NOT EXISTS session_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_session_states(
                  conversation_id TEXT PRIMARY KEY,
                  segment TEXT NOT NULL,
                  current_session_id TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_session_events(
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  conversation_id TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_events_conversation ON conversation_session_events(conversation_id, event_id)"
            )
            db.execute(
                "INSERT OR REPLACE INTO session_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )


def _payload_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None

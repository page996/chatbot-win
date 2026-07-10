from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.memory.sqlite_utils import connect


SCHEMA_VERSION = 1


class ChannelRegistryStore:
    """SQLite authority for registered conversation channels."""

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir)
        self.path = self.root / "conversation_channels.sqlite"

    def upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        item = dict(payload)
        conversation_id = str(item.get("conversation_id") or "").strip()
        if not conversation_id:
            raise ValueError("conversation_id is required")
        item.setdefault("updated_at", utc_now_iso())
        self._ensure_schema()
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO conversation_channels(
                  conversation_id, conversation_type, chat_title, segment, status, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                  conversation_type=excluded.conversation_type,
                  chat_title=excluded.chat_title,
                  segment=excluded.segment,
                  status=excluded.status,
                  updated_at=excluded.updated_at,
                  payload_json=excluded.payload_json
                """,
                (
                    conversation_id,
                    str(item.get("conversation_type") or ""),
                    str(item.get("chat_title") or ""),
                    str(item.get("segment") or ""),
                    str(item.get("status") or "active"),
                    str(item.get("updated_at") or utc_now_iso()),
                    json.dumps(item, ensure_ascii=False, sort_keys=True),
                ),
            )
        return item

    def insert_if_missing(self, payload: dict[str, Any]) -> bool:
        item = dict(payload)
        conversation_id = str(item.get("conversation_id") or "").strip()
        if not conversation_id:
            return False
        item.setdefault("updated_at", utc_now_iso())
        self._ensure_schema()
        with self._connection() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO conversation_channels(
                  conversation_id, conversation_type, chat_title, segment, status, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    str(item.get("conversation_type") or ""),
                    str(item.get("chat_title") or ""),
                    str(item.get("segment") or ""),
                    str(item.get("status") or "active"),
                    str(item.get("updated_at") or utc_now_iso()),
                    json.dumps(item, ensure_ascii=False, sort_keys=True),
                ),
            )
        return bool(cursor.rowcount)

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        self._ensure_schema()
        with self._connection() as db:
            row = db.execute(
                "SELECT payload_json FROM conversation_channels WHERE conversation_id = ?",
                (str(conversation_id or ""),),
            ).fetchone()
        return _payload_from_row(row)

    def list_channels(self) -> list[dict[str, Any]]:
        self._ensure_schema()
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT payload_json
                FROM conversation_channels
                ORDER BY updated_at ASC, conversation_id ASC
                """
            ).fetchall()
        return [payload for row in rows if (payload := _payload_from_row(row)) is not None]

    def delete(self, conversation_id: str) -> bool:
        self._ensure_schema()
        with self._connection() as db:
            cursor = db.execute(
                "DELETE FROM conversation_channels WHERE conversation_id = ?",
                (str(conversation_id or ""),),
            )
        return bool(cursor.rowcount)

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
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_registry_meta(
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_channels(
                  conversation_id TEXT PRIMARY KEY,
                  conversation_type TEXT NOT NULL,
                  chat_title TEXT NOT NULL,
                  segment TEXT NOT NULL,
                  status TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversation_channels_updated ON conversation_channels(updated_at)"
            )
            db.execute(
                "INSERT OR REPLACE INTO channel_registry_meta(key, value) VALUES ('schema_version', ?)",
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

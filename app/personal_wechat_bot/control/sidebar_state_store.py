from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.personal_wechat_bot.memory.sqlite_utils import connect


SCHEMA_VERSION = 1
WEFLOW_HISTORY_LIMIT = 50
WEFLOW_PREFERENCE_KEYS = frozenset(
    {
        "allow_non_local",
        "base_url",
        "talkers",
        "token_env",
    }
)


class SidebarStateStore:
    """SQLite authority for sidebar/console state projections.

    ``weflow_sidebar_state.json`` is a readable projection. Mutable console
    state and operation history are read only from SQLite.
    """

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir)
        self.path = self.root / "sidebar_state.sqlite"

    def read_weflow_state(
        self,
        *,
        history_limit: int = WEFLOW_HISTORY_LIMIT,
    ) -> dict[str, Any]:
        self._ensure_schema()
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT key, value_json
                FROM sidebar_state_values
                WHERE scope = 'weflow'
                """
            ).fetchall()
        state: dict[str, Any] = {}
        for row in rows:
            try:
                state[str(row["key"])] = json.loads(str(row["value_json"] or "null"))
            except json.JSONDecodeError:
                continue
        state["operation_history"] = self.list_weflow_operation_history(limit=history_limit)
        return state

    def update_weflow_state(self, update: dict[str, Any]) -> dict[str, Any]:
        payload = dict(update) if isinstance(update, dict) else {}
        history_update = payload.pop("operation_history", None) if "operation_history" in payload else None
        payload["updated_at"] = str(payload.get("updated_at") or _now_z())
        self._ensure_schema()
        with self._connection() as db:
            for key, value in payload.items():
                db.execute(
                    """
                    INSERT INTO sidebar_state_values(scope, key, value_json, updated_at)
                    VALUES ('weflow', ?, ?, ?)
                    ON CONFLICT(scope, key) DO UPDATE SET
                      value_json=excluded.value_json,
                      updated_at=excluded.updated_at
                    """,
                    (str(key), _json_dumps(value), payload["updated_at"]),
                )
        if isinstance(history_update, list):
            self.replace_weflow_operation_history(history_update)
        return self.read_weflow_state(history_limit=WEFLOW_HISTORY_LIMIT)

    def append_weflow_operation_entry(
        self,
        entry: dict[str, Any],
        *,
        limit: int = WEFLOW_HISTORY_LIMIT,
    ) -> dict[str, Any]:
        payload = dict(entry) if isinstance(entry, dict) else {}
        created_at = str(payload.get("time") or payload.get("created_at") or _now_z())
        action = str(payload.get("action") or "")
        status = str(payload.get("status") or "")
        summary = str(payload.get("summary") or "")
        self._ensure_schema()
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO weflow_operation_history(created_at, action, status, summary, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (created_at, action, status, summary, _json_dumps(payload)),
            )
            db.execute(
                """
                DELETE FROM weflow_operation_history
                WHERE id NOT IN (
                  SELECT id
                  FROM weflow_operation_history
                  ORDER BY id DESC
                  LIMIT ?
                )
                """,
                (max(1, int(limit)),),
            )
            db.execute(
                """
                INSERT INTO sidebar_state_values(scope, key, value_json, updated_at)
                VALUES ('weflow', 'updated_at', ?, ?)
                ON CONFLICT(scope, key) DO UPDATE SET
                  value_json=excluded.value_json,
                  updated_at=excluded.updated_at
                """,
                (_json_dumps(created_at), created_at),
            )
        return self.read_weflow_state(history_limit=limit)

    def replace_weflow_operation_history(self, entries: list[dict[str, Any]]) -> None:
        clean_entries = [dict(item) for item in entries if isinstance(item, dict)]
        self._ensure_schema()
        with self._connection() as db:
            db.execute("DELETE FROM weflow_operation_history")
            # Public history is newest-first; insert oldest-first so descending id
            # keeps the same visible order.
            for entry in reversed(clean_entries[-WEFLOW_HISTORY_LIMIT:]):
                created_at = str(entry.get("time") or entry.get("created_at") or _now_z())
                db.execute(
                    """
                    INSERT INTO weflow_operation_history(created_at, action, status, summary, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        created_at,
                        str(entry.get("action") or ""),
                        str(entry.get("status") or ""),
                        str(entry.get("summary") or ""),
                        _json_dumps(entry),
                    ),
                )

    def list_weflow_operation_history(self, *, limit: int = WEFLOW_HISTORY_LIMIT) -> list[dict[str, Any]]:
        self._ensure_schema()
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT payload_json, created_at, action, status, summary
                FROM weflow_operation_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        history: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload.setdefault("time", str(row["created_at"] or ""))
            payload.setdefault("action", str(row["action"] or ""))
            payload.setdefault("status", str(row["status"] or ""))
            payload.setdefault("summary", str(row["summary"] or ""))
            history.append(payload)
        return history

    def reset_weflow_history(self) -> dict[str, Any]:
        """Physically purge runtime/history state while retaining preferences."""

        self._ensure_schema()
        db = sqlite3.connect(str(self.path), timeout=30.0)
        db.row_factory = sqlite3.Row
        state: dict[str, Any] = {}
        try:
            db.execute("PRAGMA busy_timeout=30000")
            checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise sqlite3.OperationalError("sidebar state WAL checkpoint is busy")
            journal_mode = db.execute("PRAGMA journal_mode=DELETE").fetchone()
            if journal_mode is None or str(journal_mode[0]).lower() != "delete":
                raise sqlite3.OperationalError("sidebar state could not leave WAL mode")
            secure_delete = db.execute("PRAGMA secure_delete=ON").fetchone()
            if secure_delete is None or int(secure_delete[0]) != 1:
                raise sqlite3.OperationalError("sidebar state secure_delete is unavailable")

            placeholders = ", ".join("?" for _ in WEFLOW_PREFERENCE_KEYS)
            rows = db.execute(
                f"""
                SELECT key, value_json
                FROM sidebar_state_values
                WHERE scope = 'weflow' AND key IN ({placeholders})
                """,
                tuple(sorted(WEFLOW_PREFERENCE_KEYS)),
            ).fetchall()
            for row in rows:
                try:
                    state[str(row["key"])] = json.loads(str(row["value_json"] or "null"))
                except json.JSONDecodeError:
                    continue

            reset_at = _now_z()
            state.update(
                {
                    "backfill_job": {},
                    "last_backfill": {},
                    "last_discover": {},
                    "last_error": "",
                    "last_health": {},
                    "last_pull": {},
                    "pull_job": {},
                    "updated_at": reset_at,
                }
            )
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM sidebar_state_meta")
            db.execute(
                "INSERT INTO sidebar_state_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            db.execute("DELETE FROM sidebar_state_values")
            db.execute("DELETE FROM weflow_operation_history")
            db.execute("DELETE FROM sqlite_sequence WHERE name = 'weflow_operation_history'")
            for key, value in state.items():
                db.execute(
                    """
                    INSERT INTO sidebar_state_values(scope, key, value_json, updated_at)
                    VALUES ('weflow', ?, ?, ?)
                    """,
                    (key, _json_dumps(value), reset_at),
                )
            db.commit()

            # secure_delete scrubs the rows removed above. VACUUM also rebuilds
            # the file so content left by older deletes cannot survive in free
            # pages. DELETE journal mode ensures no pre-reset WAL remains.
            db.execute("VACUUM")
            if int(db.execute("PRAGMA freelist_count").fetchone()[0]) != 0:
                raise sqlite3.OperationalError("sidebar state VACUUM left free pages")
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        remaining_sidecars = [
            str(Path(f"{self.path}{suffix}"))
            for suffix in ("-wal", "-shm", "-journal")
            if Path(f"{self.path}{suffix}").exists()
        ]
        if remaining_sidecars:
            raise sqlite3.OperationalError(
                "sidebar state reset left SQLite sidecars: " + ", ".join(remaining_sidecars)
            )
        return {**state, "operation_history": []}

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
        db = connect(self.path, busy_timeout_ms=30000)
        try:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS sidebar_state_meta(
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS sidebar_state_values(
                  scope TEXT NOT NULL,
                  key TEXT NOT NULL,
                  value_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(scope, key)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS weflow_operation_history(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  created_at TEXT NOT NULL,
                  action TEXT NOT NULL,
                  status TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  payload_json TEXT NOT NULL
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_weflow_operation_history_created ON weflow_operation_history(created_at)"
            )
            db.execute(
                "INSERT OR REPLACE INTO sidebar_state_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            db.commit()
        finally:
            db.close()


def _now_z() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

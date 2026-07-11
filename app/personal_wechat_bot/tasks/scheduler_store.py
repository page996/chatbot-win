from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator


SCHEMA_VERSION = 1
_SQLITE_BUSY_TIMEOUT_MS = 30000


class SchedulerStore:
    """SQLite-backed scheduler state.

    Task payloads stay JSON-shaped while query-critical fields are indexed for
    transactional scheduler and sidebar access.
    """

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir)
        self.path = self.root / "scheduler.sqlite"

    def list_tasks(self, *, limit: int = 500) -> list[dict[str, Any]]:
        self._ensure_schema()
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT payload_json
                FROM tasks
                ORDER BY updated_at DESC, created_at DESC, task_id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        tasks: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                tasks.append(payload)
        return tasks

    def list_events(self, *, task_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        self._ensure_schema()
        task_id = str(task_id or "").strip()
        sql = """
            SELECT event_id, task_id, event, payload_json, created_at
            FROM task_events
        """
        params: tuple[Any, ...]
        if task_id:
            sql += " WHERE task_id = ?"
            params = (task_id, int(limit))
        else:
            params = (int(limit),)
        sql += " ORDER BY event_id DESC LIMIT ?"
        with self._connection() as db:
            rows = db.execute(sql, params).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except json.JSONDecodeError:
                payload = {}
            events.append(
                {
                    "event_id": int(row["event_id"]),
                    "task_id": str(row["task_id"] or ""),
                    "event": str(row["event"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "payload": payload if isinstance(payload, dict) else {},
                }
            )
        return events

    def is_empty(self) -> bool:
        self._ensure_schema()
        with self._connection() as db:
            row = db.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()
        return int(row["count"] if row else 0) <= 0

    def upsert_task(self, task: dict[str, Any]) -> None:
        self._ensure_schema()
        with self._connection() as db:
            self._upsert_task(db, task)

    def replace_tasks(self, tasks: Iterable[dict[str, Any]]) -> None:
        self._ensure_schema()
        with self._connection() as db:
            db.execute("DELETE FROM tasks")
            for task in tasks:
                self._upsert_task(db, task)

    def update_tasks_atomically(
        self,
        mutator: Callable[[list[dict[str, Any]]], tuple[list[dict[str, Any]], Any, list[tuple[str, str, dict[str, Any]]]]],
    ) -> Any:
        """Read, mutate, and replace tasks under one SQLite write lock.

        The task scheduler can be called from sidebar threads, background
        workers, and short-lived CLI commands. ``BEGIN IMMEDIATE`` gives one
        caller ownership of the scheduling decision so two workers do not claim
        the same queued task at the same time.
        """

        self._ensure_schema()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """
                SELECT payload_json
                FROM tasks
                ORDER BY updated_at DESC, created_at DESC, task_id DESC
                """
            ).fetchall()
            tasks: list[dict[str, Any]] = []
            for row in rows:
                try:
                    payload = json.loads(str(row["payload_json"] or "{}"))
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    tasks.append(payload)
            updated_tasks, result, events = mutator(tasks)
            db.execute("DELETE FROM tasks")
            for task in updated_tasks:
                self._upsert_task(db, task)
            for task_id, event, payload in events:
                db.execute(
                    """
                    INSERT INTO task_events(task_id, event, payload_json, created_at)
                    VALUES (?, ?, ?, COALESCE(NULLIF(?, ''), strftime('%Y-%m-%dT%H:%M:%SZ','now')))
                    """,
                    (
                        str(task_id),
                        str(event),
                        json.dumps(payload if isinstance(payload, dict) else {}, ensure_ascii=False, sort_keys=True),
                        str((payload or {}).get("created_at") or (payload or {}).get("updated_at") or ""),
                    ),
                )
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def append_event(self, task_id: str, event: str, payload: dict[str, Any] | None = None) -> None:
        self._ensure_schema()
        payload = payload if isinstance(payload, dict) else {}
        created_at = str(payload.get("created_at") or payload.get("updated_at") or "").strip()
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO task_events(task_id, event, payload_json, created_at)
                VALUES (?, ?, ?, COALESCE(NULLIF(?, ''), strftime('%Y-%m-%dT%H:%M:%SZ','now')))
                """,
                (
                    str(task_id),
                    str(event),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    created_at,
                ),
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000.0)
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _ensure_schema(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000.0)
        try:
            db.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
            _ensure_wal_mode(db)
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduler_meta(
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks(
                  task_id TEXT PRIMARY KEY,
                  status TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  conversation_id TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  topic_id TEXT NOT NULL,
                  resource_class TEXT NOT NULL,
                  concurrency_key TEXT NOT NULL,
                  priority INTEGER NOT NULL,
                  progress INTEGER NOT NULL,
                  external_id TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_conversation ON tasks(conversation_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_external ON tasks(external_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_resource ON tasks(resource_class)")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events(
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id TEXT NOT NULL,
                  event TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, event_id)")
            db.execute(
                "INSERT OR REPLACE INTO scheduler_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            db.commit()
        finally:
            db.close()

    def _upsert_task(self, db: sqlite3.Connection, task: dict[str, Any]) -> None:
        task_id = str(task.get("task_id") or task.get("id") or "").strip()
        if not task_id:
            return
        db.execute(
            """
            INSERT INTO tasks(
              task_id, status, kind, conversation_id, session_id, topic_id,
              resource_class, concurrency_key, priority, progress, external_id,
              created_at, updated_at, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
              status=excluded.status,
              kind=excluded.kind,
              conversation_id=excluded.conversation_id,
              session_id=excluded.session_id,
              topic_id=excluded.topic_id,
              resource_class=excluded.resource_class,
              concurrency_key=excluded.concurrency_key,
              priority=excluded.priority,
              progress=excluded.progress,
              external_id=excluded.external_id,
              created_at=excluded.created_at,
              updated_at=excluded.updated_at,
              payload_json=excluded.payload_json
            """,
            (
                task_id,
                str(task.get("status") or "queued"),
                str(task.get("kind") or "operation"),
                str(task.get("conversation_id") or ""),
                str(task.get("session_id") or "session_default"),
                str(task.get("topic_id") or ""),
                str(task.get("resource_class") or "cpu_io"),
                str(task.get("concurrency_key") or "global"),
                _int(task.get("priority"), 50),
                _int(task.get("progress"), 0),
                str(task.get("external_id") or ""),
                str(task.get("created_at") or ""),
                str(task.get("updated_at") or ""),
                json.dumps(task, ensure_ascii=False, sort_keys=True),
            ),
        )


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ensure_wal_mode(db: sqlite3.Connection) -> None:
    current = db.execute("PRAGMA journal_mode").fetchone()
    if current and str(current[0] or "").lower() == "wal":
        return

    deadline = time.monotonic() + (_SQLITE_BUSY_TIMEOUT_MS / 1000.0)
    while True:
        try:
            result = db.execute("PRAGMA journal_mode=WAL").fetchone()
            if result and str(result[0] or "").lower() == "wal":
                return
            raise sqlite3.OperationalError("failed to enable WAL journal mode")
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.05, remaining))

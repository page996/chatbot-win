from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from app.personal_wechat_bot.conversation.channel_control import normalize_control_mode, parse_bool, snooze_is_active
from app.personal_wechat_bot.domain.models import utc_now_iso


SCHEMA_VERSION = 1
ACTIVE_TASK_STATUSES = {"queued", "running", "waiting", "paused", "blocked"}
TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}


@dataclass
class ChannelStateRecord:
    conversation_id: str
    conversation_type: str = ""
    chat_title: str = ""
    status: str = "active"
    current_topic: dict[str, Any] = field(default_factory=dict)
    topic_queue: list[dict[str, Any]] = field(default_factory=list)
    topic_history: list[dict[str, Any]] = field(default_factory=list)
    active_tasks: list[dict[str, Any]] = field(default_factory=list)
    waiting_tasks: list[dict[str, Any]] = field(default_factory=list)
    paused_tasks: list[dict[str, Any]] = field(default_factory=list)
    task_history: list[dict[str, Any]] = field(default_factory=list)
    file_states: list[dict[str, Any]] = field(default_factory=list)
    reply_state: dict[str, Any] = field(default_factory=dict)
    resource_audit: dict[str, Any] = field(default_factory=dict)
    control: dict[str, Any] = field(default_factory=dict)
    effective_status: str = "idle"
    last_user_message_at: str = ""
    last_agent_reply_at: str = ""
    last_message_at: str = ""
    message_count: int = 0
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema"] = "channel_state_v1"
        return payload


class ChannelStateStore:
    """SQLite authority for per-channel runtime state.

    The store is intentionally projection-friendly: producers can update narrow
    fields later, while the first rollout can rebuild state from existing
    channel registry, task records, and ledger entries without changing the
    mature message/file pipelines.
    """

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir)
        self.path = self.root / "channel_state.sqlite"

    def upsert(self, record: ChannelStateRecord | dict[str, Any]) -> dict[str, Any]:
        payload = record.to_dict() if isinstance(record, ChannelStateRecord) else dict(record)
        conversation_id = str(payload.get("conversation_id") or "").strip()
        if not conversation_id:
            raise ValueError("conversation_id is required")
        payload.setdefault("updated_at", utc_now_iso())
        self._ensure_schema()
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO channel_states(
                  conversation_id, conversation_type, chat_title, status, updated_at, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                  conversation_type=excluded.conversation_type,
                  chat_title=excluded.chat_title,
                  status=excluded.status,
                  updated_at=excluded.updated_at,
                  payload_json=excluded.payload_json
                """,
                (
                    conversation_id,
                    str(payload.get("conversation_type") or ""),
                    str(payload.get("chat_title") or ""),
                    str(payload.get("status") or "active"),
                    str(payload.get("updated_at") or utc_now_iso()),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
        return payload

    def replace_all(self, records: Iterable[ChannelStateRecord | dict[str, Any]]) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for record in records:
            payload = record.to_dict() if isinstance(record, ChannelStateRecord) else dict(record)
            if str(payload.get("conversation_id") or "").strip():
                payload.setdefault("updated_at", utc_now_iso())
                payloads.append(payload)
        self._ensure_schema()
        with self._connection() as db:
            db.execute("DELETE FROM channel_states")
            for payload in payloads:
                db.execute(
                    """
                    INSERT INTO channel_states(
                      conversation_id, conversation_type, chat_title, status, updated_at, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(payload.get("conversation_id") or ""),
                        str(payload.get("conversation_type") or ""),
                        str(payload.get("chat_title") or ""),
                        str(payload.get("status") or "active"),
                        str(payload.get("updated_at") or utc_now_iso()),
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
        return payloads

    def list_states(self, *, limit: int = 500) -> list[dict[str, Any]]:
        self._ensure_schema()
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT payload_json
                FROM channel_states
                ORDER BY updated_at DESC, conversation_id ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        states: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                states.append(payload)
        return states

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        self._ensure_schema()
        with self._connection() as db:
            row = db.execute(
                "SELECT payload_json FROM channel_states WHERE conversation_id = ?",
                (str(conversation_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def patch_control(self, conversation_id: str, patch: dict[str, Any], *, updated_by: str = "local_user") -> dict[str, Any]:
        """Merge operator-owned channel controls without touching live projection fields."""

        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            raise ValueError("conversation_id is required")
        current = self.get(conversation_id) or {"conversation_id": conversation_id}
        control = _normalize_control(current.get("control") if isinstance(current.get("control"), dict) else {})
        control.update(_control_patch(patch))
        control["updated_by"] = str(updated_by or "local_user")
        control["updated_at"] = utc_now_iso()
        current["conversation_id"] = conversation_id
        current["control"] = control
        current["effective_status"] = _effective_status(current, control)
        current["updated_at"] = max(str(current.get("updated_at") or ""), str(control.get("updated_at") or "")) or utc_now_iso()
        return self.upsert(current)

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
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _ensure_schema(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=30000")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_state_meta(
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_states(
                  conversation_id TEXT PRIMARY KEY,
                  conversation_type TEXT NOT NULL,
                  chat_title TEXT NOT NULL,
                  status TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_channel_states_status ON channel_states(status)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_channel_states_updated ON channel_states(updated_at)")
            db.execute(
                "INSERT OR REPLACE INTO channel_state_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            db.commit()
        finally:
            db.close()


def build_channel_state_projection(
    *,
    channel: dict[str, Any],
    tasks: list[dict[str, Any]] | None = None,
    ledger_entries: list[dict[str, Any]] | None = None,
) -> ChannelStateRecord:
    tasks = [dict(item) for item in (tasks or []) if isinstance(item, dict)]
    ledger_entries = [dict(item) for item in (ledger_entries or []) if isinstance(item, dict)]
    active_tasks = [item for item in _ordered_tasks(tasks) if str(item.get("status") or "") in ACTIVE_TASK_STATUSES]
    terminal_tasks = [item for item in _ordered_tasks(tasks) if str(item.get("status") or "") in TERMINAL_TASK_STATUSES]
    current_task = active_tasks[0] if active_tasks else (_ordered_tasks(tasks)[0] if tasks else {})
    topics = _topic_history(tasks)
    reply_state = _reply_state(ledger_entries)
    file_states = _file_states(ledger_entries)
    last_user = _last_entry_at(ledger_entries, role="user")
    last_agent = _last_entry_at(ledger_entries, role="assistant")
    last_any = _last_entry_at(ledger_entries)
    control = _normalize_control({})
    record = ChannelStateRecord(
        conversation_id=str(channel.get("conversation_id") or ""),
        conversation_type=str(channel.get("conversation_type") or ""),
        chat_title=str(channel.get("chat_title") or ""),
        status=str(channel.get("status") or "active"),
        current_topic={
            "topic_id": str(current_task.get("topic_id") or ""),
            "title": str(current_task.get("topic_title") or current_task.get("title") or ""),
            "priority": _int(current_task.get("priority"), 0),
            "status": str(current_task.get("status") or "idle"),
        },
        topic_queue=[item for item in topics if int(item.get("active_count", 0) or 0) > 0][:12],
        topic_history=topics[:20],
        active_tasks=active_tasks[:20],
        waiting_tasks=[item for item in active_tasks if str(item.get("status") or "") in {"waiting", "blocked"}][:12],
        paused_tasks=[item for item in active_tasks if str(item.get("status") or "") == "paused"][:12],
        task_history=terminal_tasks[:20],
        file_states=file_states[:40],
        reply_state=reply_state,
        resource_audit={
            "estimated_cost": sum(_int(item.get("estimated_cost"), 0) for item in tasks),
            "actual_cost": sum(_int(item.get("actual_cost"), 0) for item in tasks),
            "resources": _resource_pools(tasks),
        },
        control=control,
        last_user_message_at=last_user,
        last_agent_reply_at=last_agent,
        last_message_at=last_any,
        message_count=len(ledger_entries),
        updated_at=max(
            [
                str(channel.get("updated_at") or ""),
                *(str(item.get("updated_at") or item.get("received_at") or "") for item in ledger_entries),
                *(str(item.get("updated_at") or "") for item in tasks),
            ]
            or [utc_now_iso()]
        )
        or utc_now_iso(),
    )
    record.effective_status = _effective_status(record.to_dict(), control)
    return record


def merge_channel_state_projection(
    projection: ChannelStateRecord | dict[str, Any],
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    """Overlay durable operator controls on top of a freshly rebuilt projection."""

    payload = projection.to_dict() if isinstance(projection, ChannelStateRecord) else dict(projection)
    current = existing if isinstance(existing, dict) else {}
    control = _normalize_control(current.get("control") if isinstance(current.get("control"), dict) else {})
    payload["control"] = control
    payload["effective_status"] = _effective_status(payload, control)
    control_updated = str(control.get("updated_at") or "")
    if control_updated:
        payload["updated_at"] = max(str(payload.get("updated_at") or ""), control_updated)
    return payload


def _ordered_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    status_order = {"running": 0, "queued": 1, "waiting": 2, "blocked": 3, "paused": 4, "failed": 5, "cancelled": 6, "completed": 7}
    return sorted(
        tasks,
        key=lambda item: (
            status_order.get(str(item.get("status") or ""), 9),
            -_int(item.get("priority"), 0),
            str(item.get("updated_at") or ""),
        ),
    )


def _topic_history(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    topics: dict[str, dict[str, Any]] = {}
    for task in tasks:
        topic_id = str(task.get("topic_id") or task.get("concurrency_key") or task.get("task_id") or "").strip()
        if not topic_id:
            continue
        topic = topics.setdefault(
            topic_id,
            {
                "topic_id": topic_id,
                "title": str(task.get("topic_title") or task.get("title") or ""),
                "task_count": 0,
                "active_count": 0,
                "terminal_count": 0,
                "max_priority": 0,
                "updated_at": "",
            },
        )
        topic["task_count"] += 1
        status = str(task.get("status") or "")
        if status in ACTIVE_TASK_STATUSES:
            topic["active_count"] += 1
        if status in TERMINAL_TASK_STATUSES:
            topic["terminal_count"] += 1
        topic["max_priority"] = max(_int(topic.get("max_priority"), 0), _int(task.get("priority"), 0))
        topic["updated_at"] = max(str(topic.get("updated_at") or ""), str(task.get("updated_at") or ""))
    return sorted(
        topics.values(),
        key=lambda item: (_int(item.get("active_count"), 0), _int(item.get("max_priority"), 0), str(item.get("updated_at") or "")),
        reverse=True,
    )


def _reply_state(entries: list[dict[str, Any]]) -> dict[str, Any]:
    assistant_entries = [item for item in entries if str(item.get("role") or "") == "assistant"]
    if not assistant_entries:
        return {"status": "idle", "last_reply_at": "", "last_send_status": ""}
    latest = sorted(assistant_entries, key=lambda item: str(item.get("received_at") or item.get("created_at") or ""))[-1]
    send = latest.get("send") if isinstance(latest.get("send"), dict) else {}
    return {
        "status": str(send.get("status") or "drafted"),
        "last_reply_at": str(latest.get("received_at") or latest.get("created_at") or ""),
        "last_send_status": str(send.get("status") or ""),
        "last_reply_entry_id": str(latest.get("entry_id") or ""),
        "last_reply_message_id": str(latest.get("message_id") or ""),
    }


def _file_states(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        attachments = entry.get("attachments") if isinstance(entry.get("attachments"), list) else []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            file_id = str(attachment.get("file_id") or "").strip()
            key = file_id or f"{attachment.get('path', '')}:{attachment.get('name', '')}"
            if not key:
                continue
            parse = attachment.get("parse") if isinstance(attachment.get("parse"), dict) else {}
            artifacts = attachment.get("artifacts") if isinstance(attachment.get("artifacts"), dict) else {}
            summary = str(artifacts.get("ai_summary") or parse.get("ai_summary") or parse.get("summary") or "").strip()
            points = artifacts.get("ai_key_points") or parse.get("ai_key_points") or []
            record = {
                "file_id": file_id,
                "name": str(attachment.get("name") or ""),
                "kind": str(parse.get("kind") or attachment.get("kind") or ""),
                "status": str(attachment.get("status") or parse.get("status") or ""),
                "parse_status": str(parse.get("status") or ""),
                "ai_analysis_status": str(artifacts.get("ai_analysis_status") or parse.get("ai_analysis_status") or ""),
                "summary": summary[:500],
                "key_points": [str(item).strip() for item in points if str(item).strip()][:8] if isinstance(points, list) else [],
                "chunk_count": _int(artifacts.get("chunk_count") or parse.get("chunk_count"), 0),
                "message_id": str(entry.get("message_id") or ""),
                "entry_id": str(entry.get("entry_id") or ""),
                "updated_at": str(entry.get("updated_at") or entry.get("received_at") or ""),
            }
            previous = by_id.get(key)
            if previous is None or str(record.get("updated_at") or "") >= str(previous.get("updated_at") or ""):
                by_id[key] = record
    return sorted(by_id.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True)


def _last_entry_at(entries: list[dict[str, Any]], *, role: str = "") -> str:
    filtered = [item for item in entries if not role or str(item.get("role") or "") == role]
    if not filtered:
        return ""
    return max(str(item.get("received_at") or item.get("created_at") or item.get("updated_at") or "") for item in filtered)


def _resource_pools(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    pools: dict[str, dict[str, int]] = {}
    for task in tasks:
        resource = str(task.get("resource_class") or "cpu_io")
        pool = pools.setdefault(resource, {"active": 0, "queued": 0, "max_parallel": 1})
        status = str(task.get("status") or "")
        if status == "running":
            pool["active"] += 1
        elif status == "queued":
            pool["queued"] += 1
    return pools


def _normalize_control(value: dict[str, Any]) -> dict[str, Any]:
    mode = normalize_control_mode(value.get("mode"))
    return {
        "schema": "channel_control_v1",
        "mode": mode,
        "priority": _bounded_int(value.get("priority"), 50, 0, 100),
        "pinned": parse_bool(value.get("pinned"), False),
        "wait_reason": str(value.get("wait_reason") or ""),
        "operator_note": str(value.get("operator_note") or ""),
        "snoozed_until": str(value.get("snoozed_until") or ""),
        "updated_by": str(value.get("updated_by") or ""),
        "updated_at": str(value.get("updated_at") or ""),
    }


def _control_patch(patch: dict[str, Any]) -> dict[str, Any]:
    patch = patch if isinstance(patch, dict) else {}
    result: dict[str, Any] = {}
    if "mode" in patch:
        result["mode"] = normalize_control_mode(patch.get("mode"))
    if "priority" in patch:
        result["priority"] = _bounded_int(patch.get("priority"), 50, 0, 100)
    if "pinned" in patch:
        result["pinned"] = parse_bool(patch.get("pinned"), False)
    for key in ("wait_reason", "operator_note", "snoozed_until"):
        if key in patch:
            result[key] = str(patch.get(key) or "")
    return result


def _effective_status(payload: dict[str, Any], control: dict[str, Any]) -> str:
    mode = str(control.get("mode") or "active")
    if mode in {"paused", "muted"}:
        return mode
    if mode == "snoozed" and snooze_is_active(control.get("snoozed_until")):
        return mode
    topic = payload.get("current_topic") if isinstance(payload.get("current_topic"), dict) else {}
    status = str(topic.get("status") or "")
    if status and status != "idle":
        return status
    if payload.get("active_tasks"):
        return "active"
    reply = payload.get("reply_state") if isinstance(payload.get("reply_state"), dict) else {}
    reply_status = str(reply.get("status") or "")
    if reply_status and reply_status != "idle":
        return reply_status
    return "idle"


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, _int(value, default)))


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


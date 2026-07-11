from __future__ import annotations

import json
import os
import sqlite3
import uuid
from copy import deepcopy
from dataclasses import asdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from app.personal_wechat_bot.domain.models import ReplyCandidate, utc_now_iso
from app.personal_wechat_bot.memory.sqlite_utils import connect
from app.personal_wechat_bot.runtime.process_lock import (
    blocking_process_lock,
    process_pid_alive,
    process_start_marker,
)


SCHEMA_VERSION = 1

SEND_CLAIM_CONFLICT = "send_claim_conflict"
SEND_CLAIM_OWNER_EXITED = "send_claim_owner_exited_outcome_unknown"

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


class ConfirmQueueClaimConflict(RuntimeError):
    """Raised when a queue mutation is fenced by an active send claim."""


class ConfirmQueue:
    """SQLite-backed authority for the human confirmation queue.

    ``confirm_queue.jsonl`` is a readable projection for diagnostics and
    operator inspection. It never repopulates the SQLite authority.
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
            self._refresh_projection_unlocked(self._read_all_unlocked())
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

    def claim_approved_for_send(
        self,
        queue_id: str,
        *,
        owner: str = "send_approved_confirm_item",
    ) -> dict[str, Any]:
        """Atomically fence one approved item for a single send invocation.

        The public queue status stays ``approved`` while the external operation
        is running, but ``send_claim`` is persisted in the SQLite authority and
        its JSONL projection. A claim owned by a process that has exited is
        retired to ``failed`` rather than stolen: after an owner crash, whether
        an external driver delivered is unknowable, so retrying automatically
        would violate at-most-once delivery.
        """

        token = uuid.uuid4().hex
        with self._queue_lock():
            records = self._read_all_unlocked()
            for index, item in enumerate(records):
                if item.get("queue_id") != queue_id:
                    continue
                current_status = str(item.get("status", ""))
                if current_status != "approved":
                    return {
                        "claimed": False,
                        "reason": f"queue item status is {current_status}",
                        "token": "",
                        "item": deepcopy(item),
                    }
                existing = _active_send_claim(item)
                if existing:
                    if _send_claim_owner_is_alive(existing):
                        return {
                            "claimed": False,
                            "reason": SEND_CLAIM_CONFLICT,
                            "token": "",
                            "claim": deepcopy(existing),
                            "item": deepcopy(item),
                        }
                    now = utc_now_iso()
                    retired = deepcopy(item)
                    retired.pop("send_claim", None)
                    retired.update(
                        {
                            "status": "failed",
                            "reviewed_at": now,
                            "reviewer": "system",
                            "note": SEND_CLAIM_OWNER_EXITED,
                            "last_send_claim": {
                                **deepcopy(existing),
                                "resolved_at": now,
                                "resolution": SEND_CLAIM_OWNER_EXITED,
                            },
                        }
                    )
                    records[index] = retired
                    self._write_all_unlocked(records)
                    return {
                        "claimed": False,
                        "reason": SEND_CLAIM_OWNER_EXITED,
                        "token": "",
                        "item": deepcopy(retired),
                    }
                now = utc_now_iso()
                claimed = deepcopy(item)
                claim = {
                    "token": token,
                    "owner": str(owner or "send_approved_confirm_item"),
                    "owner_pid": os.getpid(),
                    "owner_process_start": process_start_marker(os.getpid()),
                    "claimed_at": now,
                }
                claimed["send_claim"] = claim
                records[index] = claimed
                self._write_all_unlocked(records)
                return {
                    "claimed": True,
                    "reason": "claimed",
                    "token": token,
                    "claim": deepcopy(claim),
                    "item": deepcopy(claimed),
                }
        raise KeyError(f"queue_id not found: {queue_id}")

    def release_send_claim(
        self,
        queue_id: str,
        token: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Release a claim after a proven pre-delivery blocker."""

        with self._queue_lock():
            records = self._read_all_unlocked()
            for index, item in enumerate(records):
                if item.get("queue_id") != queue_id:
                    continue
                if str(item.get("status", "")) != "approved":
                    raise ConfirmQueueClaimConflict(
                        f"send claim cannot be released from status {item.get('status')}"
                    )
                claim = _require_send_claim(item, token)
                now = utc_now_iso()
                released = deepcopy(item)
                released.pop("send_claim", None)
                released["last_send_claim"] = {
                    **deepcopy(claim),
                    "resolved_at": now,
                    "resolution": str(reason or "send_claim_released"),
                }
                records[index] = released
                self._write_all_unlocked(records)
                return deepcopy(released)
        raise KeyError(f"queue_id not found: {queue_id}")

    def mark_send_result(
        self,
        queue_id: str,
        status: str,
        reason: str,
        *,
        reviewer: str = "local_user",
        extra: dict[str, Any] | None = None,
        claim_token: str = "",
    ) -> dict[str, Any]:
        if status not in {"queued_to_bridge", "sent", "accepted", "failed"}:
            raise ValueError("status must be queued_to_bridge, sent, accepted, or failed")
        return self._transition(
            queue_id,
            status,
            reviewer=reviewer,
            note=reason,
            extra=extra,
            claim_token=claim_token,
        )

    def requeue_bridge_result(
        self,
        old_bridge_id: str,
        new_bridge_id: str,
        reason: str,
        *,
        reviewer: str = "local_user",
        old_bridge_ids: list[str] | None = None,
    ) -> dict[str, Any] | None:
        old_bridge_id = str(old_bridge_id or "").strip()
        new_bridge_id = str(new_bridge_id or "").strip()
        if not old_bridge_id or not new_bridge_id:
            return None
        candidates = _dedupe_bridge_ids([old_bridge_id, *(old_bridge_ids or [])])
        with self._queue_lock():
            records = self._read_all_unlocked()
            changed: dict[str, Any] | None = None
            for index in range(len(records) - 1, -1, -1):
                item = records[index]
                if not any(_queue_item_references_bridge(item, candidate) for candidate in candidates):
                    continue
                _ensure_send_not_claimed(item)
                if str(item.get("status") or "") == "rejected":
                    return None
                changed = _queue_item_with_bridge_retry(
                    item,
                    candidates,
                    new_bridge_id,
                    retry_parent_id=old_bridge_id,
                    reason=reason or f"retry_to_non_foreground_bridge:{new_bridge_id}",
                    reviewer=reviewer,
                )
                records[index] = changed
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
                _ensure_send_not_claimed(item)
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
                    _ensure_send_not_claimed(item)
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
        claim_token: str = "",
    ) -> dict[str, Any]:
        with self._queue_lock():
            records = self._read_all_unlocked()
            changed: dict[str, Any] | None = None
            for item in records:
                if item.get("queue_id") != queue_id:
                    continue
                current_status = str(item.get("status", ""))
                claim = _active_send_claim(item)
                if claim:
                    _require_send_claim(item, claim_token)
                elif claim_token:
                    raise ConfirmQueueClaimConflict("send claim is no longer active")
                _validate_transition(current_status, status)
                item["status"] = status
                item["reviewed_at"] = utc_now_iso()
                item["reviewer"] = reviewer
                item["note"] = note
                if claim:
                    item.pop("send_claim", None)
                    item["last_send_claim"] = {
                        **deepcopy(claim),
                        "resolved_at": item["reviewed_at"],
                        "resolution": status,
                    }
                if extra:
                    for key, value in extra.items():
                        if str(key) == "send_claim":
                            raise ValueError("send_claim is reserved queue metadata")
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
                ON CONFLICT(queue_id) DO NOTHING
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
        self._refresh_projection_unlocked(records)

    def _refresh_projection_unlocked(self, records: list[dict[str, Any]]) -> None:
        """Refresh the non-authoritative JSONL view without failing DB writes."""
        try:
            self._write_projection_unlocked(records)
        except OSError:
            # SQLite is authoritative. A sharing violation or other transient
            # projection error must not make a committed mutation look failed;
            # the next queue mutation/enqueue will rebuild the full projection.
            return

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
            db.commit()
        finally:
            db.close()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _active_send_claim(item: dict[str, Any]) -> dict[str, Any]:
    claim = item.get("send_claim") if isinstance(item.get("send_claim"), dict) else {}
    return claim if str(claim.get("token") or "").strip() else {}


def _require_send_claim(item: dict[str, Any], token: str) -> dict[str, Any]:
    claim = _active_send_claim(item)
    expected = str(claim.get("token") or "")
    if not expected:
        raise ConfirmQueueClaimConflict("send claim is not active")
    if not token or str(token) != expected:
        raise ConfirmQueueClaimConflict(SEND_CLAIM_CONFLICT)
    return claim


def _ensure_send_not_claimed(item: dict[str, Any]) -> None:
    if _active_send_claim(item):
        raise ConfirmQueueClaimConflict(SEND_CLAIM_CONFLICT)


def _send_claim_owner_is_alive(claim: dict[str, Any]) -> bool:
    try:
        owner_pid = int(claim.get("owner_pid") or 0)
    except (TypeError, ValueError):
        owner_pid = 0
    # A malformed owner cannot safely be fenced out. Fail closed and require an
    # explicit operator decision instead of risking a second external send.
    if owner_pid <= 0:
        return True
    if not process_pid_alive(owner_pid):
        return False
    recorded_start = str(claim.get("owner_process_start") or "")
    current_start = process_start_marker(owner_pid) if recorded_start else ""
    if recorded_start and current_start and recorded_start != current_start:
        return False
    return True


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


def _queue_item_with_bridge_retry(
    item: dict[str, Any],
    old_bridge_ids: list[str],
    new_bridge_id: str,
    *,
    retry_parent_id: str,
    reason: str,
    reviewer: str,
) -> dict[str, Any]:
    updated = dict(item)
    candidate_set = set(old_bridge_ids)
    bridge_ids = item.get("bridge_ids") if isinstance(item.get("bridge_ids"), list) else []
    updated_bridge_ids = _replace_bridge_ids(bridge_ids, candidate_set, new_bridge_id)
    if new_bridge_id not in updated_bridge_ids:
        updated_bridge_ids.append(new_bridge_id)
    existing_acks = item.get("bridge_acks") if isinstance(item.get("bridge_acks"), dict) else {}
    updated_acks = {
        str(key): dict(value)
        for key, value in existing_acks.items()
        if isinstance(value, dict) and str(key) not in candidate_set and str(key) != new_bridge_id
    }
    send_result = item.get("send_result") if isinstance(item.get("send_result"), dict) else {}
    updated_send_result = _send_result_with_bridge_retry(
        send_result,
        candidate_set,
        new_bridge_id,
        retry_parent_id=retry_parent_id,
        reason=reason,
    )
    aggregate_status = _aggregate_requeued_bridge_status(updated_bridge_ids, updated_acks)
    if updated_send_result:
        updated_send_result["status"] = aggregate_status
    updated.update(
        {
            "status": aggregate_status,
            "reviewed_at": utc_now_iso(),
            "reviewer": reviewer,
            "note": reason,
            "retry_of": retry_parent_id,
            "bridge_ids": updated_bridge_ids,
            "bridge_acks": updated_acks,
            "last_bridge_ack": {},
        }
    )
    if updated_send_result:
        updated["send_result"] = updated_send_result
    return updated


def _send_result_with_bridge_retry(
    send_result: dict[str, Any],
    old_bridge_ids: set[str],
    new_bridge_id: str,
    *,
    retry_parent_id: str,
    reason: str,
) -> dict[str, Any]:
    if not send_result:
        return {}
    now = utc_now_iso()
    updated = dict(send_result)
    top_matches = any(_send_part_references_bridge(send_result, candidate) for candidate in old_bridge_ids)
    if str(updated.get("message_id") or "") in old_bridge_ids:
        updated["message_id"] = new_bridge_id
    if str(updated.get("bridge_id") or "") in old_bridge_ids:
        updated["bridge_id"] = new_bridge_id
    details = send_result.get("details") if isinstance(send_result.get("details"), dict) else {}
    if details:
        next_details = dict(details)
        raw_ids = details.get("bridge_ids") if isinstance(details.get("bridge_ids"), list) else []
        next_bridge_ids = _replace_bridge_ids(raw_ids, old_bridge_ids, new_bridge_id)
        if new_bridge_id not in next_bridge_ids:
            next_bridge_ids.append(new_bridge_id)
        next_details["bridge_ids"] = next_bridge_ids
        text = details.get("text") if isinstance(details.get("text"), dict) else {}
        if text and any(_send_part_references_bridge(text, candidate) for candidate in old_bridge_ids):
            next_details["text"] = _send_part_with_bridge_retry(
                text,
                old_bridge_ids,
                new_bridge_id,
                retry_parent_id=retry_parent_id,
                reason=reason,
                now=now,
            )
        files = details.get("files") if isinstance(details.get("files"), list) else []
        if files:
            next_details["files"] = [
                _send_part_with_bridge_retry(
                    file_detail,
                    old_bridge_ids,
                    new_bridge_id,
                    retry_parent_id=retry_parent_id,
                    reason=reason,
                    now=now,
                )
                if isinstance(file_detail, dict)
                and any(_send_part_references_bridge(file_detail, candidate) for candidate in old_bridge_ids)
                else (dict(file_detail) if isinstance(file_detail, dict) else file_detail)
                for file_detail in files
            ]
        next_details.pop("last_bridge_ack", None)
        detail_acks = next_details.get("bridge_acks") if isinstance(next_details.get("bridge_acks"), dict) else {}
        if detail_acks:
            next_details["bridge_acks"] = {
                str(key): dict(value)
                for key, value in detail_acks.items()
                if isinstance(value, dict) and str(key) not in old_bridge_ids and str(key) != new_bridge_id
            }
        updated["details"] = next_details
    updated["status"] = "queued_to_bridge"
    updated["reason"] = reason
    updated["sent_at"] = ""
    updated["retry_of"] = retry_parent_id
    updated["retry_at"] = now
    updated["updated_at"] = now
    if top_matches:
        updated.pop("external_message_id", None)
    updated.pop("last_bridge_ack", None)
    return updated


def _send_part_with_bridge_retry(
    payload: dict[str, Any],
    old_bridge_ids: set[str],
    new_bridge_id: str,
    *,
    retry_parent_id: str,
    reason: str,
    now: str,
) -> dict[str, Any]:
    updated = dict(payload)
    for key in ("message_id", "bridge_id", "external_id"):
        if str(updated.get(key) or "") in old_bridge_ids:
            updated[key] = new_bridge_id
    updated.update(
        {
            "status": "queued_to_bridge",
            "reason": reason,
            "sent_at": "",
            "retry_of": retry_parent_id,
            "retry_at": now,
            "updated_at": now,
        }
    )
    updated.pop("external_message_id", None)
    updated.pop("last_bridge_ack", None)
    return updated


def _send_part_references_bridge(payload: dict[str, Any], bridge_id: str) -> bool:
    for key in ("message_id", "bridge_id", "external_id"):
        if str(payload.get(key) or "") == bridge_id:
            return True
    return bridge_id in str(payload.get("reason") or "")


def _replace_bridge_ids(values: list[Any], old_bridge_ids: set[str], new_bridge_id: str) -> list[str]:
    replaced = [new_bridge_id if str(value) in old_bridge_ids else str(value) for value in values]
    if any(str(value) in old_bridge_ids for value in values) and new_bridge_id not in replaced:
        replaced.append(new_bridge_id)
    return _dedupe_bridge_ids(replaced)


def _dedupe_bridge_ids(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        bridge_id = str(value or "").strip()
        if not bridge_id.startswith("bridge:") or bridge_id in seen:
            continue
        seen.add(bridge_id)
        result.append(bridge_id)
    return result


def _aggregate_requeued_bridge_status(bridge_ids: list[str], bridge_acks: dict[str, Any]) -> str:
    statuses = [
        str(bridge_acks.get(bridge_id, {}).get("queue_status") or bridge_acks.get(bridge_id, {}).get("status") or "queued_to_bridge")
        if isinstance(bridge_acks.get(bridge_id), dict)
        else "queued_to_bridge"
        for bridge_id in bridge_ids
    ]
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status == "queued_to_bridge" for status in statuses):
        return "queued_to_bridge"
    if any(status == "accepted" for status in statuses):
        return "accepted"
    return "sent" if statuses and all(status == "sent" for status in statuses) else "queued_to_bridge"


def _validate_transition(current_status: str, status: str) -> None:
    if current_status != status and status not in _ALLOWED_TRANSITIONS.get(current_status, set()):
        raise ValueError(f"invalid confirm queue transition: {current_status} -> {status}")

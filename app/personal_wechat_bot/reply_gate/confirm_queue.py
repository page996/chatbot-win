from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.domain.models import ReplyCandidate, utc_now_iso


_ALLOWED_TRANSITIONS = {
    "pending": {"approved", "rejected"},
    "approved": {"rejected", "queued_to_bridge", "sent", "failed"},
    "queued_to_bridge": {"sent", "failed"},
    # A durable bridge "sent" ack is stronger than a previous local failure
    # marker: the message is on the wire, so reconciliation must be able to
    # repair the queue state.
    "failed": {"sent"},
}


class ConfirmQueue:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def enqueue(self, reply: ReplyCandidate) -> str:
        queue_id = f"{reply.message_id}:{reply.created_at}"
        record = {
            "queue_id": queue_id,
            "status": "pending",
            "created_at": utc_now_iso(),
            "reply": asdict(reply),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return queue_id

    def list_pending(self) -> list[dict[str, Any]]:
        return self.list_by_status("pending")

    def list_by_status(self, status: str) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        matches: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                item = json.loads(line)
                if item.get("status") == status:
                    matches.append(item)
        return matches

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
            note = str(item.get("note", ""))
            if bridge_id in note:
                return item
        return None

    def approve(self, queue_id: str, *, reviewer: str = "local_user", note: str = "") -> dict[str, Any]:
        return self._transition(queue_id, "approved", reviewer=reviewer, note=note)

    def reject(self, queue_id: str, *, reviewer: str = "local_user", note: str = "") -> dict[str, Any]:
        return self._transition(queue_id, "rejected", reviewer=reviewer, note=note)

    def mark_send_result(self, queue_id: str, status: str, reason: str, *, reviewer: str = "local_user") -> dict[str, Any]:
        if status not in {"queued_to_bridge", "sent", "failed"}:
            raise ValueError("status must be queued_to_bridge, sent, or failed")
        return self._transition(queue_id, status, reviewer=reviewer, note=reason)

    def remove(self, queue_id: str) -> dict[str, Any]:
        records = self._read_all()
        kept: list[dict[str, Any]] = []
        removed: dict[str, Any] | None = None
        for item in records:
            if item.get("queue_id") == queue_id and removed is None:
                removed = item
                continue
            kept.append(item)
        if removed is None:
            raise KeyError(f"queue_id not found: {queue_id}")
        self._write_all(kept)
        return removed

    def _transition(self, queue_id: str, status: str, *, reviewer: str, note: str) -> dict[str, Any]:
        records = self._read_all()
        changed: dict[str, Any] | None = None
        for item in records:
            if item.get("queue_id") != queue_id:
                continue
            current_status = str(item.get("status", ""))
            if current_status != status and status not in _ALLOWED_TRANSITIONS.get(current_status, set()):
                raise ValueError(f"invalid confirm queue transition: {current_status} -> {status}")
            item["status"] = status
            item["reviewed_at"] = utc_now_iso()
            item["reviewer"] = reviewer
            item["note"] = note
            changed = item
            break
        if changed is None:
            raise KeyError(f"queue_id not found: {queue_id}")
        self._write_all(records)
        return changed

    def _read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                item = json.loads(line)
                if isinstance(item, dict):
                    records.append(item)
        return records

    def _write_all(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for item in records:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        tmp.replace(self.path)

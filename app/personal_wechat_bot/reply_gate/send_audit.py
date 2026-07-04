from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.logging.jsonl_rotation import append_line_with_rotation


class SendAuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

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
        append_line_with_rotation(self.path, json.dumps(record, ensure_ascii=False))
        return record

    def list_recent(self, *, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        records = self._read_all()
        if status:
            records = [item for item in records if item.get("status") == status]
        return records[-limit:]

    def clear(self) -> int:
        records = self._read_all()
        self.path.write_text("", encoding="utf-8")
        return len(records)

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

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.logging.jsonl_rotation import append_line_with_rotation


class EventLogger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, payload: Any, **ids: str) -> None:
        record = {
            "event_type": event_type,
            "timestamp": utc_now_iso(),
            **ids,
            "payload": _to_jsonable(payload),
        }
        append_line_with_rotation(self.path, json.dumps(record, ensure_ascii=False))


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value

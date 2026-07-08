from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


CONTROL_MODES = {"active", "paused", "muted", "snoozed"}


def normalize_control_mode(value: Any) -> str:
    mode = str(value or "active").strip().lower()
    return mode if mode in CONTROL_MODES else "active"


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def snooze_is_active(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return True
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc) > datetime.now(timezone.utc)

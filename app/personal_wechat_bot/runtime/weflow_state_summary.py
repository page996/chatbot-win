from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def summarize_weflow_bridge_state(state_path: str | Path) -> dict[str, Any]:
    """Summarize ``weflow_bridge_state.json`` for background stability checks.

    The bridge writes a per-session cursor (``sessions[talker].since``) plus a
    global ``seen_raw_ids`` dedup list. Surfacing them lets the sidebar confirm
    that concurrent talkers keep independent, monotonically advancing cursors
    and that dedup is retaining ids across pull loops, without exposing raw ids.
    """

    path = Path(state_path)
    if not path.exists():
        return {
            "status": "absent",
            "state_path": str(path),
            "session_count": 0,
            "seen_raw_id_count": 0,
            "sessions": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "unreadable",
            "state_path": str(path),
            "error": f"{type(exc).__name__}: {exc}",
            "session_count": 0,
            "seen_raw_id_count": 0,
            "sessions": [],
        }
    if not isinstance(payload, dict):
        return {
            "status": "invalid",
            "state_path": str(path),
            "session_count": 0,
            "seen_raw_id_count": 0,
            "sessions": [],
        }

    sessions_raw = payload.get("sessions")
    sessions_raw = sessions_raw if isinstance(sessions_raw, dict) else {}
    sessions: list[dict[str, Any]] = []
    for talker, session_state in sessions_raw.items():
        since = 0
        if isinstance(session_state, dict):
            since = _int(session_state.get("since"))
        sessions.append(
            {
                "talker": str(talker),
                "since": since,
                "is_group": str(talker).endswith("@chatroom"),
            }
        )
    sessions.sort(key=lambda item: item["talker"])
    seen = payload.get("seen_raw_ids")
    seen_count = len(seen) if isinstance(seen, list) else 0
    sse_seen = payload.get("weflow_sse_seen")
    sse_seen_count = len(sse_seen) if isinstance(sse_seen, list) else 0
    return {
        "status": "ok",
        "state_path": str(path),
        "session_count": len(sessions),
        "group_session_count": sum(1 for item in sessions if item["is_group"]),
        "private_session_count": sum(1 for item in sessions if not item["is_group"]),
        "seen_raw_id_count": seen_count,
        "sse_seen_count": sse_seen_count,
        "sse_last_event_id": str(payload.get("weflow_sse_last_event_id") or ""),
        "sessions": sessions,
    }


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

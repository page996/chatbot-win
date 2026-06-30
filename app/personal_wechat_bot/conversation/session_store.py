from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.domain.models import NormalizedMessage, utc_now_iso


DEFAULT_SESSION_ID = "session_default"
CLEAR_CONTEXT_PHRASES = (
    "清空当前对话上下文",
    "清空上下文",
    "重置当前对话上下文",
    "reset context",
    "clear context",
)


class ConversationSessionStore:
    """Owns the active session pointer for each conversation.

    The ledger is the only source for prompt context. This store deliberately
    keeps no message/reply history; it only switches the current session when a
    reset command is observed and writes a small audit trail.
    """

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir) / "conversation_sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def current_session_id(self, conversation_id: str) -> str:
        state = self._read_state(conversation_id)
        session_id = str(state.get("current_session_id", "")).strip()
        if session_id:
            return session_id
        self._write_state(
            conversation_id,
            {
                "conversation_id": conversation_id,
                "current_session_id": DEFAULT_SESSION_ID,
                "created_at": utc_now_iso(),
            },
        )
        return DEFAULT_SESSION_ID

    def maybe_reset_for_message(self, message: NormalizedMessage) -> str | None:
        if not is_reset_command(message.text):
            return None
        return self.reset_session(
            message.conversation_id,
            reason="clear_current_context_command",
            message_id=message.message_id,
        )

    def reset_session(self, conversation_id: str, *, reason: str, message_id: str = "") -> str:
        session_id = _new_session_id()
        self._write_state(
            conversation_id,
            {
                "conversation_id": conversation_id,
                "current_session_id": session_id,
                "previous_reset_reason": reason,
                "previous_reset_message_id": message_id,
            },
        )
        self._append_event(
            conversation_id,
            {
                "type": "session.reset",
                "conversation_id": conversation_id,
                "session_id": session_id,
                "reason": reason,
                "message_id": message_id,
                "created_at": utc_now_iso(),
            },
        )
        return session_id

    def _conversation_dir(self, conversation_id: str) -> Path:
        return self.root / _safe_segment(conversation_id)

    def _state_path(self, conversation_id: str) -> Path:
        return self._conversation_dir(conversation_id) / "state.json"

    def _events_path(self, conversation_id: str) -> Path:
        return self._conversation_dir(conversation_id) / "events.jsonl"

    def _read_state(self, conversation_id: str) -> dict[str, Any]:
        path = self._state_path(conversation_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_state(self, conversation_id: str, payload: dict[str, Any]) -> None:
        path = self._state_path(conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = self._read_state(conversation_id)
        merged = {**previous, **payload, "updated_at": utc_now_iso()}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _append_event(self, conversation_id: str, payload: dict[str, Any]) -> None:
        path = self._events_path(conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def is_reset_command(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    for phrase in CLEAR_CONTEXT_PHRASES:
        if _normalize_text(phrase) in normalized:
            return True
    return False


def _new_session_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"session_{stamp}_{uuid.uuid4().hex[:8]}"


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "default"


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()

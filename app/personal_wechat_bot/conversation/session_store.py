from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from app.personal_wechat_bot.conversation.segment import resolve_segment
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
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "conversation_sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        # conversation_id -> stable directory segment. A message title is only
        # used to choose the first segment before a channel exists.
        self._segment_cache: dict[str, str] = {}

    def current_session_id(self, conversation_id: str) -> str:
        with self._conversation_lock(conversation_id):
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

    def current_session_id_for_conversation(self, conversation_id: str, chat_title: str = "") -> str:
        self._remember_segment(conversation_id, chat_title)
        return self.current_session_id(conversation_id)

    def current_session_id_for_message(self, message: NormalizedMessage) -> str:
        return self.current_session_id_for_conversation(message.conversation_id, message.chat_title)

    def maybe_reset_for_message(self, message: NormalizedMessage) -> str | None:
        self._remember_segment(message.conversation_id, message.chat_title)
        if not is_reset_command(message.text):
            return None
        return self.reset_session(
            message.conversation_id,
            reason="clear_current_context_command",
            message_id=message.message_id,
        )

    def reset_session(self, conversation_id: str, *, reason: str, message_id: str = "") -> str:
        session_id = _new_session_id()
        with self._conversation_lock(conversation_id):
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
        cached_segment = self._segment_cache.get(conversation_id, "")
        if cached_segment:
            return self.root / cached_segment
        return self.root / resolve_segment(self.data_dir, conversation_id)

    def _remember_segment(self, conversation_id: str, chat_title: str = "") -> str:
        if not chat_title:
            cached_segment = self._segment_cache.get(conversation_id, "")
            if cached_segment:
                return cached_segment
            existing_dir = self._find_conversation_dir(conversation_id)
            if existing_dir.exists():
                self._segment_cache[conversation_id] = existing_dir.name
                return existing_dir.name
        segment = resolve_segment(self.data_dir, conversation_id, chat_title)
        self._segment_cache[conversation_id] = segment
        return segment

    def _find_conversation_dir(self, conversation_id: str) -> Path:
        candidate = self._conversation_dir(conversation_id)
        if candidate.exists():
            return candidate
        if self.root.exists():
            for state_json in self.root.glob("*/state.json"):
                try:
                    payload = json.loads(state_json.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict) and payload.get("conversation_id") == conversation_id:
                    return state_json.parent
        return candidate

    def _state_path(self, conversation_id: str) -> Path:
        return self._find_conversation_dir(conversation_id) / "state.json"

    def _events_path(self, conversation_id: str) -> Path:
        return self._find_conversation_dir(conversation_id) / "events.jsonl"

    def _read_state(self, conversation_id: str) -> dict[str, Any]:
        path = self._find_conversation_dir(conversation_id) / "state.json"
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
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _append_event(self, conversation_id: str, payload: dict[str, Any]) -> None:
        path = self._events_path(conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    @contextmanager
    def _conversation_lock(self, conversation_id: str) -> Iterator[None]:
        lock_path = self._find_conversation_dir(conversation_id) / ".session.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 30.0
        fd: int | None = None
        while fd is None:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, PermissionError):
                if _stale_lock(lock_path):
                    try:
                        lock_path.unlink()
                        continue
                    except OSError:
                        pass
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for conversation session lock: {lock_path}")
                time.sleep(0.025)
        try:
            os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
            yield
        finally:
            if fd is not None:
                os.close(fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


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


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _stale_lock(path: Path, *, max_age_seconds: float = 60.0) -> bool:
    try:
        return time.time() - path.stat().st_mtime > max_age_seconds
    except OSError:
        return False

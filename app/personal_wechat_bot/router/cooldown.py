from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.memory.sqlite_utils import connect


class ConversationCooldown:
    def __init__(self, seconds: int, db_path: str | Path | None = None):
        self.seconds = max(0, seconds)
        self.db_path = Path(db_path) if db_path is not None else None
        self._last_seen: dict[str, datetime] = {}
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()

    def allow(self, conversation_id: str, now_iso_text: str | None = None) -> tuple[bool, str]:
        allowed, reason = self.check(conversation_id, now_iso_text)
        if allowed:
            self.mark(conversation_id, now_iso_text)
        return allowed, reason

    def check(self, conversation_id: str, now_iso_text: str | None = None) -> tuple[bool, str]:
        if self.seconds <= 0:
            return True, "cooldown_disabled"

        now = _parse_iso(now_iso_text) if now_iso_text else datetime.now(timezone.utc)
        previous = self._read_last_seen(conversation_id)
        if previous is not None:
            elapsed = (now - previous).total_seconds()
            if elapsed < self.seconds:
                remaining = int(self.seconds - elapsed)
                return False, f"group_cooldown:{remaining}s_remaining"

        return True, "cooldown_allowed"

    def mark(self, conversation_id: str, now_iso_text: str | None = None) -> None:
        now = _parse_iso(now_iso_text) if now_iso_text else datetime.now(timezone.utc)
        self._last_seen[conversation_id] = now
        if self.db_path is None:
            return
        with closing(connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO conversation_cooldowns (conversation_id, last_reply_at)
                    VALUES (?, ?)
                    ON CONFLICT(conversation_id)
                    DO UPDATE SET last_reply_at = excluded.last_reply_at
                    """,
                    (conversation_id, now.isoformat()),
                )

    def _read_last_seen(self, conversation_id: str) -> datetime | None:
        if conversation_id in self._last_seen:
            return self._last_seen[conversation_id]
        if self.db_path is None:
            return None
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT last_reply_at FROM conversation_cooldowns WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        parsed = _parse_iso(str(row[0]))
        self._last_seen[conversation_id] = parsed
        return parsed

    def _init_db(self) -> None:
        if self.db_path is None:
            return
        with closing(connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS conversation_cooldowns (
                      conversation_id TEXT PRIMARY KEY,
                      last_reply_at TEXT NOT NULL
                    )
                    """
                )


def _parse_iso(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

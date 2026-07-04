from __future__ import annotations

from contextlib import closing
from pathlib import Path

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.memory.sqlite_utils import connect


class Deduper:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self._seen: set[str] = set()
        self.db_path = Path(db_path) if db_path is not None else None
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()

    def seen(self, message_id: str) -> bool:
        if message_id in self._seen:
            return True
        if self.db_path is None:
            return False
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        if row is not None:
            self._seen.add(message_id)
            return True
        return False

    def mark(self, message_id: str) -> None:
        self._seen.add(message_id)
        if self.db_path is None:
            return
        with closing(connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO processed_messages (message_id, first_seen_at)
                    VALUES (?, ?)
                    """,
                    (message_id, utc_now_iso()),
                )

    def _init_db(self) -> None:
        if self.db_path is None:
            return
        with closing(connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS processed_messages (
                      message_id TEXT PRIMARY KEY,
                      first_seen_at TEXT NOT NULL
                    )
                    """
                )

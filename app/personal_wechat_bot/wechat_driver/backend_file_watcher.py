from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from app.personal_wechat_bot.wechat_driver.backend_events import append_backend_event


@dataclass(frozen=True)
class WatchedFileEvent:
    path: str
    raw_id: str


class BackendFileWatcher:
    """Turn new files in controlled local folders into backend message events."""

    def __init__(self, state_db: str | Path, event_path: str | Path):
        self.state_db = Path(state_db)
        self.event_path = Path(event_path)
        self.state_db.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def scan_once(
        self,
        roots: list[Path],
        *,
        chat_title: str,
        sender_name: str,
        sender_wechat_id: str = "",
        is_group: bool = False,
        text_prefix: str = "收到后台文件",
        recursive: bool = False,
        since_seconds: int | None = None,
        max_files: int | None = None,
        allowed_extensions: list[str] | None = None,
    ) -> list[WatchedFileEvent]:
        created: list[WatchedFileEvent] = []
        cutoff = time.time() - since_seconds if since_seconds is not None else None
        allowed = {item.lower() for item in allowed_extensions or []}
        for root in roots:
            if not root.exists() or not root.is_dir():
                continue
            candidates = sorted(
                _iter_files(root, recursive=recursive),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for path in candidates:
                if not path.is_file() or _should_skip(path):
                    continue
                if allowed and path.suffix.lower() not in allowed:
                    continue
                if cutoff is not None and path.stat().st_mtime < cutoff:
                    continue
                fingerprint = _fingerprint(path)
                if not self._try_mark_seen(fingerprint, path):
                    continue
                raw_id = append_backend_event(
                    self.event_path,
                    chat_title=chat_title,
                    sender_name=sender_name,
                    sender_wechat_id=sender_wechat_id,
                    text=f"{text_prefix}: {path.name}",
                    is_group=is_group,
                    attachments=[str(path)],
                )
                created.append(WatchedFileEvent(path=str(path), raw_id=raw_id))
                if max_files is not None and len(created) >= max_files:
                    return created
        return created

    def _init_db(self) -> None:
        with closing(sqlite3.connect(self.state_db)) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS watched_files (
                      fingerprint TEXT PRIMARY KEY,
                      path TEXT NOT NULL,
                      first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )

    def _try_mark_seen(self, fingerprint: str, path: Path) -> bool:
        with closing(sqlite3.connect(self.state_db)) as conn:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO watched_files (fingerprint, path)
                    VALUES (?, ?)
                    """,
                    (fingerprint, str(path)),
                )
                return cursor.rowcount == 1


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    raw = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _should_skip(path: Path) -> bool:
    name = path.name
    return (
        name.startswith(".")
        or name.endswith(".tmp")
        or name.endswith(".crdownload")
        or name.endswith(".part")
        or name == "Thumbs.db"
    )


def _iter_files(root: Path, recursive: bool) -> list[Path]:
    if recursive:
        return [path for path in root.rglob("*") if path.is_file()]
    return [path for path in root.iterdir() if path.is_file()]

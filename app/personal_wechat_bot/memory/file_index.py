from __future__ import annotations

import hashlib
import mimetypes
from contextlib import closing
from pathlib import Path

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.memory.sqlite_utils import connect


class FileIndex:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with closing(connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS file_index (
                      id TEXT PRIMARY KEY,
                      original_name TEXT NOT NULL,
                      stored_path TEXT NOT NULL,
                      source TEXT NOT NULL,
                      mime_type TEXT,
                      sha256 TEXT,
                      created_at TEXT NOT NULL
                    )
                    """
                )

    def add(self, path: str | Path, source: str, original_name: str | None = None) -> str:
        file_path = Path(path)
        digest = _sha256_file(file_path)
        file_id = digest[:24]
        mime_type = mimetypes.guess_type(file_path.name)[0]
        with closing(connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO file_index
                    (id, original_name, stored_path, source, mime_type, sha256, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        original_name or file_path.name,
                        str(file_path),
                        source,
                        mime_type,
                        digest,
                        utc_now_iso(),
                    ),
                )
        return file_id


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

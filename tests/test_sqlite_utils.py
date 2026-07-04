from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.memory.sqlite_utils import connect


class SqliteUtilsTest(unittest.TestCase):
    def test_connect_enables_wal_and_busy_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.sqlite"
            conn = connect(db, busy_timeout_ms=3000)
            try:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 3000)
            finally:
                conn.close()

    def test_file_index_survives_concurrent_writers(self) -> None:
        # Many threads adding to one FileIndex must not raise
        # "database is locked" now that WAL + busy_timeout are set.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = FileIndex(root / "file_index.sqlite")
            files = []
            for i in range(24):
                p = root / f"f{i}.txt"
                p.write_text(f"content {i}", encoding="utf-8")
                files.append(p)

            errors: list[Exception] = []

            def worker(path: Path) -> None:
                try:
                    index.add(path, source="test", original_name=path.name)
                except Exception as exc:  # capture, don't crash the thread
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(p,)) for p in files]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [], f"concurrent add() raised: {errors}")


if __name__ == "__main__":
    unittest.main()

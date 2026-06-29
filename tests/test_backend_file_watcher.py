from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from app.personal_wechat_bot.wechat_driver.backend_file_watcher import BackendFileWatcher


class BackendFileWatcherTest(unittest.TestCase):
    def test_scan_once_appends_events_for_new_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "inbox"
            root.mkdir()
            event_file = Path(tmp) / "backend_events.jsonl"
            watcher = BackendFileWatcher(Path(tmp) / "watcher.sqlite", event_file)
            (root / "note.txt").write_text("hello", encoding="utf-8")

            first = watcher.scan_once([root], chat_title="PAGE", sender_name="PAGE")
            second = watcher.scan_once([root], chat_title="PAGE", sender_name="PAGE")

            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            lines = event_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["chat_title"], "PAGE")
            self.assertEqual(payload["attachments"][0]["path"], str(root / "note.txt"))

    def test_try_mark_seen_is_atomic_for_same_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "inbox"
            root.mkdir()
            event_file = Path(tmp) / "backend_events.jsonl"
            watcher = BackendFileWatcher(Path(tmp) / "watcher.sqlite", event_file)
            note = root / "note.txt"
            note.write_text("hello", encoding="utf-8")
            fingerprint = __import__(
                "app.personal_wechat_bot.wechat_driver.backend_file_watcher",
                fromlist=["_fingerprint"],
            )._fingerprint(note)

            self.assertTrue(watcher._try_mark_seen(fingerprint, note))
            self.assertFalse(watcher._try_mark_seen(fingerprint, note))

    def test_scan_once_skips_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "inbox"
            root.mkdir()
            event_file = Path(tmp) / "backend_events.jsonl"
            watcher = BackendFileWatcher(Path(tmp) / "watcher.sqlite", event_file)
            (root / "download.tmp").write_text("partial", encoding="utf-8")

            created = watcher.scan_once([root], chat_title="PAGE", sender_name="PAGE")

            self.assertEqual(created, [])
            self.assertFalse(event_file.exists())

    def test_scan_once_filters_by_extension_time_and_max_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "inbox"
            nested = root / "nested"
            nested.mkdir(parents=True)
            event_file = Path(tmp) / "backend_events.jsonl"
            watcher = BackendFileWatcher(Path(tmp) / "watcher.sqlite", event_file)
            old_file = root / "old.txt"
            old_file.write_text("old", encoding="utf-8")
            os.utime(old_file, (time.time() - 3600, time.time() - 3600))
            (nested / "new.txt").write_text("new", encoding="utf-8")
            (nested / "skip.exe").write_text("skip", encoding="utf-8")

            created = watcher.scan_once(
                [root],
                chat_title="PAGE",
                sender_name="PAGE",
                recursive=True,
                since_seconds=60,
                max_files=1,
                allowed_extensions=[".txt"],
            )

            self.assertEqual(len(created), 1)
            self.assertTrue(created[0].path.endswith("new.txt"))


if __name__ == "__main__":
    unittest.main()

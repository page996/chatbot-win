from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.wechat_driver.jsonl_bus import _file_identity, append_jsonl, append_jsonl_once


class JsonlBusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "events.jsonl"
        self.index = self.root / "events.index.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_equal_length_replacement_does_not_suppress_removed_key(self) -> None:
        self.assertTrue(append_jsonl_once(self.target, {"raw_id": "aa", "text": "same"}, index_path=self.index))
        original_stat = self.target.stat()

        self._replace_key_preserving_size_and_mtime("aa", "bb", original_stat)
        self._align_index_numeric_identity_to_target()

        self.assertTrue(append_jsonl_once(self.target, {"raw_id": "aa", "text": "new"}, index_path=self.index))
        self.assertEqual([item["raw_id"] for item in self._records()], ["bb", "aa"])
        self._assert_v2_index(["aa", "bb"])

    def test_equal_length_replacement_does_not_duplicate_new_key(self) -> None:
        self.assertTrue(append_jsonl_once(self.target, {"raw_id": "aa", "text": "same"}, index_path=self.index))
        original_stat = self.target.stat()

        self._replace_key_preserving_size_and_mtime("aa", "bb", original_stat)
        self._align_index_numeric_identity_to_target()

        self.assertFalse(append_jsonl_once(self.target, {"raw_id": "bb", "text": "duplicate"}, index_path=self.index))
        self.assertEqual([item["raw_id"] for item in self._records()], ["bb"])
        self._assert_v2_index(["bb"])

    def test_atomic_equal_length_replacement_invalidates_index(self) -> None:
        self.assertTrue(append_jsonl_once(self.target, {"raw_id": "aa", "text": "same"}, index_path=self.index))
        original_stat = self.target.stat()
        replacement = self.root / "replacement.jsonl"
        original = self.target.read_text(encoding="utf-8")
        replacement.write_text(original.replace('"aa"', '"bb"', 1), encoding="utf-8")
        os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        replacement.replace(self.target)

        self.assertEqual(self.target.stat().st_size, original_stat.st_size)
        self.assertEqual(self.target.stat().st_mtime_ns, original_stat.st_mtime_ns)
        self.assertFalse(append_jsonl_once(self.target, {"raw_id": "bb"}, index_path=self.index))
        self.assertEqual([item["raw_id"] for item in self._records()], ["bb"])
        self._assert_v2_index(["bb"])

    def test_v1_index_is_rebuilt_before_dedupe(self) -> None:
        self.target.write_text(json.dumps({"raw_id": "bb"}) + "\n", encoding="utf-8")
        self.index.write_text(
            json.dumps(
                {
                    "version": 1,
                    "key_field": "raw_id",
                    "source_size": self.target.stat().st_size,
                    "keys": ["aa"],
                }
            ),
            encoding="utf-8",
        )

        self.assertFalse(append_jsonl_once(self.target, {"raw_id": "bb"}, index_path=self.index))
        self.assertEqual([item["raw_id"] for item in self._records()], ["bb"])
        self._assert_v2_index(["bb"])

    def test_index_is_rebuilt_when_key_field_changes(self) -> None:
        payload = {"raw_id": "aa", "event_id": "bb"}
        self.assertTrue(append_jsonl_once(self.target, payload, index_path=self.index))

        self.assertFalse(
            append_jsonl_once(
                self.target,
                {"raw_id": "cc", "event_id": "bb"},
                key_field="event_id",
                index_path=self.index,
            )
        )
        self.assertEqual(self._records(), [payload])
        state = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(state["key_field"], "event_id")
        self.assertEqual(state["keys"], ["bb"])

    def test_malformed_v2_metadata_is_rebuilt(self) -> None:
        self.assertTrue(append_jsonl_once(self.target, {"raw_id": "aa"}, index_path=self.index))
        state = json.loads(self.index.read_text(encoding="utf-8"))
        state["source_size"] = "not-an-integer"
        state["keys"] = ["bb"]
        self.index.write_text(json.dumps(state), encoding="utf-8")

        self.assertFalse(append_jsonl_once(self.target, {"raw_id": "aa"}, index_path=self.index))
        self.assertEqual([item["raw_id"] for item in self._records()], ["aa"])
        self._assert_v2_index(["aa"])

    def test_append_jsonl_isolates_crash_truncated_tail(self) -> None:
        self.target.write_bytes(b'{"raw_id":"truncated"')

        append_jsonl(self.target, {"raw_id": "good"})

        lines = self.target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], '{"raw_id":"truncated"')
        self.assertEqual(json.loads(lines[1]), {"raw_id": "good"})

    def test_append_jsonl_once_isolates_crash_truncated_tail_and_updates_index(self) -> None:
        self.target.write_bytes(b'{"raw_id":"truncated"')

        self.assertTrue(append_jsonl_once(self.target, {"raw_id": "good"}, index_path=self.index))

        lines = self.target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], '{"raw_id":"truncated"')
        self.assertEqual(json.loads(lines[1]), {"raw_id": "good"})
        self._assert_v2_index(["good"])

    def _replace_key_preserving_size_and_mtime(self, old: str, new: str, original_stat: os.stat_result) -> None:
        original = self.target.read_text(encoding="utf-8")
        replacement = original.replace(f'"{old}"', f'"{new}"', 1)
        self.assertNotEqual(original, replacement)
        self.assertEqual(len(original.encode("utf-8")), len(replacement.encode("utf-8")))
        self.target.write_text(replacement, encoding="utf-8")
        os.utime(self.target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        self.assertEqual(self.target.stat().st_size, original_stat.st_size)
        self.assertEqual(self.target.stat().st_mtime_ns, original_stat.st_mtime_ns)

    def _records(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.target.read_text(encoding="utf-8").splitlines()]

    def _align_index_numeric_identity_to_target(self) -> None:
        state = json.loads(self.index.read_text(encoding="utf-8"))
        stat = self.target.stat()
        state.update(
            {
                "source_size": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
                "source_ctime_ns": stat.st_ctime_ns,
                "source_device": getattr(stat, "st_dev", 0) or 0,
                "source_file_id": getattr(stat, "st_ino", 0) or 0,
            }
        )
        self.index.write_text(json.dumps(state), encoding="utf-8")

    def _assert_v2_index(self, keys: list[str]) -> None:
        state = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["keys"], keys)
        identity = _file_identity(self.target)
        self.assertEqual(
            {field: state[field] for field in identity},
            identity,
        )
        self.assertEqual(len(state["source_fingerprint"]), 64)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.logging.jsonl_rotation import append_line_with_rotation


class JsonlRotationTest(unittest.TestCase):
    def test_rotates_when_exceeding_max_bytes_and_keeps_generations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs.jsonl"
            # Small cap so a few lines trigger rotation.
            line = "x" * 100
            for _ in range(50):
                append_line_with_rotation(path, line, max_bytes=200, keep=2)

            # Rotated generations exist and are capped at keep=2 (.1 and .2 only).
            self.assertTrue(path.with_name("logs.jsonl.1").exists())
            self.assertTrue(path.with_name("logs.jsonl.2").exists())
            self.assertFalse(path.with_name("logs.jsonl.3").exists())
            # A subsequent append re-creates the active file.
            append_line_with_rotation(path, "after", max_bytes=10_000_000, keep=2)
            self.assertTrue(path.exists())
            self.assertIn("after", path.read_text(encoding="utf-8"))

    def test_no_rotation_below_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs.jsonl"
            for i in range(3):
                append_line_with_rotation(path, f"line {i}", max_bytes=10_000_000, keep=3)

            self.assertTrue(path.exists())
            self.assertFalse(path.with_name("logs.jsonl.1").exists())
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 3)

    def test_written_line_survives_even_when_rotation_triggers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs.jsonl"
            append_line_with_rotation(path, "first", max_bytes=1, keep=2)
            # After rotation the newest line lives in the rotated .1 file.
            rotated = path.with_name("logs.jsonl.1")
            self.assertTrue(rotated.exists())
            self.assertIn("first", rotated.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

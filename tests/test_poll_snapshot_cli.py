from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SNAPSHOT = ROOT / "fixtures" / "messages" / "windows_snapshot.txt"


class PollSnapshotCliTest(unittest.TestCase):
    def test_poll_snapshot_cli_runs_readonly_snapshot_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._run("--data-dir", str(data_dir), "init")
            self._run("--data-dir", str(data_dir), "add-contact", "wxid_xiaoming")
            self._run("--data-dir", str(data_dir), "add-group", "学习群")

            output = self._run(
                "--data-dir",
                str(data_dir),
                "poll-snapshot",
                str(SNAPSHOT),
                "--loops",
                "1",
                "--interval",
                "0",
            )

            payload = json.loads(output)
            self.assertEqual(payload["status"], "stopped")
            self.assertEqual(payload["processed_count"], 2)

    def _run(self, *args: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-m", "app.personal_wechat_bot.main", *args],
            cwd=ROOT.parent,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )
        return completed.stdout


if __name__ == "__main__":
    unittest.main()

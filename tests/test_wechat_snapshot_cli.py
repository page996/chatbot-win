from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class WechatSnapshotCliTest(unittest.TestCase):
    def test_wechat_snapshot_cli_safely_returns_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "snapshot.txt"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.personal_wechat_bot.main",
                    "wechat-snapshot",
                    "--max-nodes",
                    "5",
                    "--output",
                    str(output_path),
                ],
                cwd=ROOT.parent,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
            )

            payload = json.loads(completed.stdout)

            self.assertIn(payload["status"], {"ok", "empty"})
            self.assertFalse(payload["send_enabled"])
            self.assertTrue(output_path.exists())


if __name__ == "__main__":
    unittest.main()

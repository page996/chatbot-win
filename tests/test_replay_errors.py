from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.personal_wechat_bot.config.loader import add_contact, create_default_config, load_config, set_deepseek_provider
from app.personal_wechat_bot.replay.runner import ReplayRunner


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures" / "messages"


class ReplayErrorTest(unittest.TestCase):
    def test_missing_api_key_records_error_without_marking_message_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            add_contact(data_dir, "wxid_xiaoming")
            set_deepseek_provider(data_dir, api_key_env="MISSING_DEEPSEEK_KEY_FOR_TEST")
            config = load_config(data_dir)

            first = ReplayRunner(config).run(FIXTURES / "private_basic.json")
            second = ReplayRunner(config).run(FIXTURES / "private_basic.json")

            self.assertEqual(first["processed"][0]["error"]["type"], "RuntimeError")
            self.assertEqual(second["processed"][0]["route"]["action"], "process")
            logs = (data_dir / "logs.jsonl").read_text(encoding="utf-8")
            self.assertIn("reply.error", logs)
            with closing(sqlite3.connect(data_dir / "processed_messages.sqlite")) as conn:
                count = conn.execute("SELECT COUNT(*) FROM processed_messages").fetchone()[0]
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()

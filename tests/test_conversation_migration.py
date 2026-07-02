from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.runtime.conversation_migration import (
    ConversationMigration,
    migrate_conversations,
)


def _entry_id(conversation_id: str, message_id: str) -> str:
    return hashlib.sha256(f"{conversation_id}:{message_id}".encode("utf-8")).hexdigest()[:24]


class ConversationMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.old = "old000000000000000000000"
        self.new = "new111111111111111111111"
        self._seed_conversation(self.old)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_conversation(self, cid: str) -> None:
        # ledger
        ledger = self.data / "conversation_ledgers" / cid
        ledger.mkdir(parents=True, exist_ok=True)
        entry = {
            "entry_id": _entry_id(cid, "msg-1"),
            "message_id": "msg-1",
            "conversation_id": cid,
            "chat_title": "PAGE",
            "text_blocks": [
                {"kind": "text", "text": "hi", "source_ref": f"E:\\d\\file_workspace\\{cid}\\session_default\\f1\\manifest.json"}
            ],
            "attachments": [
                {"file_id": "f1", "workspace": {"conversation_id": cid, "workspace_dir": f"E:\\d\\file_workspace\\{cid}\\session_default\\f1"}}
            ],
        }
        (ledger / "messages.jsonl").write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
        (ledger / "state.json").write_text(json.dumps({"conversation_id": cid, "last_entry_id": entry["entry_id"]}), encoding="utf-8")
        (ledger / "conversation.md").write_text(f"# Conversation {cid}\n", encoding="utf-8")
        # channel
        channel = self.data / "conversation_channels" / cid
        channel.mkdir(parents=True, exist_ok=True)
        (channel / "channel.json").write_text(
            json.dumps({"conversation_id": cid, "chat_title": "PAGE", "context_dir": f"data\\conversation_ledgers\\{cid}"}),
            encoding="utf-8",
        )
        # session
        session = self.data / "conversation_sessions" / cid
        session.mkdir(parents=True, exist_ok=True)
        (session / "state.json").write_text(json.dumps({"conversation_id": cid, "current_session_id": "session_default"}), encoding="utf-8")
        # file_workspace
        ws = self.data / "file_workspace" / cid / "session_default" / "f1"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "manifest.json").write_text(
            json.dumps({"conversation_id": cid, "workspace_dir": f"E:\\d\\file_workspace\\{cid}\\session_default\\f1"}),
            encoding="utf-8",
        )

    def test_dry_run_changes_nothing(self) -> None:
        report = migrate_conversations(
            self.data, [ConversationMigration(self.old, self.new)], dry_run=True
        )
        self.assertTrue(report.dry_run)
        self.assertEqual(len(report.items), 1)
        # old dirs still present, new dirs absent
        self.assertTrue((self.data / "conversation_ledgers" / self.old).exists())
        self.assertFalse((self.data / "conversation_ledgers" / self.new).exists())

    def test_apply_moves_dirs_and_rewrites(self) -> None:
        report = migrate_conversations(
            self.data, [ConversationMigration(self.old, self.new)], dry_run=False
        )
        self.assertFalse(report.dry_run)
        item = report.items[0]

        # 1. directories moved
        for dir_name in ("conversation_ledgers", "conversation_channels", "conversation_sessions", "file_workspace"):
            self.assertFalse((self.data / dir_name / self.old).exists(), dir_name)
            self.assertTrue((self.data / dir_name / self.new).exists(), dir_name)

        # 2. ledger conversation_id + entry_id rewritten, message_id preserved
        line = (self.data / "conversation_ledgers" / self.new / "messages.jsonl").read_text(encoding="utf-8").strip()
        entry = json.loads(line)
        self.assertEqual(entry["conversation_id"], self.new)
        self.assertEqual(entry["message_id"], "msg-1")
        self.assertEqual(entry["entry_id"], _entry_id(self.new, "msg-1"))

        # 3. embedded old-id path segments swapped
        self.assertIn(self.new, entry["attachments"][0]["workspace"]["workspace_dir"])
        self.assertNotIn(self.old, entry["attachments"][0]["workspace"]["workspace_dir"])
        self.assertNotIn(self.old, entry["text_blocks"][0]["source_ref"])

        # 4. channel + workspace manifest rewritten
        channel = json.loads((self.data / "conversation_channels" / self.new / "channel.json").read_text(encoding="utf-8"))
        self.assertEqual(channel["conversation_id"], self.new)
        self.assertIn(self.new, channel["context_dir"])
        manifest = json.loads(
            (self.data / "file_workspace" / self.new / "session_default" / "f1" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["conversation_id"], self.new)
        self.assertGreaterEqual(item.reidentified_entries, 1)

    def test_noop_when_old_equals_new(self) -> None:
        report = migrate_conversations(
            self.data, [ConversationMigration(self.old, self.old)], dry_run=False
        )
        self.assertEqual(len(report.items), 0)
        self.assertEqual(len(report.skipped), 1)

    def test_conflict_when_target_exists(self) -> None:
        self._seed_conversation(self.new)  # pre-existing target
        report = migrate_conversations(
            self.data, [ConversationMigration(self.old, self.new)], dry_run=False
        )
        # every dir conflicts, so no dirs移动 -> skipped as "no source directories" after conflicts
        item = report.items[0] if report.items else None
        if item is not None:
            self.assertTrue(item.conflicts)
        else:
            self.assertTrue(report.skipped)
        # old data must remain intact on conflict
        self.assertTrue((self.data / "conversation_ledgers" / self.old).exists())


if __name__ == "__main__":
    unittest.main()

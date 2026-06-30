from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.conversation.ledger_context import LedgerContextAssembler
from app.personal_wechat_bot.conversation.session_store import DEFAULT_SESSION_ID
from app.personal_wechat_bot.domain.models import NormalizedMessage
from app.personal_wechat_bot.memory.maintainer import MemoryMaintainer


class MemoryMaintainerTest(unittest.TestCase):
    def test_maintain_writes_summary_preferences_entities_and_context_reads_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            message = _message(
                "m1",
                "我希望以后请你简短回复。#任务 请分析文件",
                metadata={
                    "attachments": [
                        {
                            "file_id": "file1",
                            "name": "report.pdf",
                            "kind": "pdf",
                            "status": "indexed",
                            "workspace": {"manifest_path": "workspace/file1/manifest.json"},
                            "artifacts": {"content_path": "workspace/file1/derived/content.md"},
                        }
                    ]
                },
            )
            store.append_message(message)

            result = MemoryMaintainer(store).maintain("conv1")

            memory_dir = Path(result.memory_dir)
            summary = (memory_dir / "summary.md").read_text(encoding="utf-8")
            preferences = json.loads((memory_dir / "preferences.json").read_text(encoding="utf-8"))
            entities = json.loads((memory_dir / "entities.json").read_text(encoding="utf-8"))
            rendered = LedgerContextAssembler(store).build_snapshot(message).render_for_prompt()

            self.assertEqual(result.processed_count, 1)
            self.assertIn("简短回复", summary)
            self.assertIn("instructions", preferences)
            self.assertEqual(entities["conversation"]["chat_title"], "PAGE")
            self.assertEqual(entities["files"][0]["name"], "report.pdf")
            self.assertIn("Long-term memory", rendered)
            self.assertIn("preferences", rendered)

    def test_maintain_is_incremental_but_rebuilds_from_active_session_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            store.append_message(_message("m1", "第一条任务记录"))
            maintainer = MemoryMaintainer(store)

            first = maintainer.maintain("conv1")
            second = maintainer.maintain("conv1")
            store.append_message(_message("m2", "第二条任务记录"))
            third = maintainer.maintain("conv1")

            summary = Path(third.summary_path).read_text(encoding="utf-8")

            self.assertEqual(first.processed_count, 1)
            self.assertEqual(second.status, "unchanged")
            self.assertEqual(second.processed_count, 0)
            self.assertEqual(third.processed_count, 1)
            self.assertIn("第一条任务记录", summary)
            self.assertIn("第二条任务记录", summary)

    def test_recalled_entries_are_not_written_to_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            store.append_message(_message("m1", "这条之后会撤回"))
            maintainer = MemoryMaintainer(store)
            first = maintainer.maintain("conv1")

            store.mark_recalled("conv1", "m1")
            result = maintainer.maintain("conv1")
            summary = Path(result.summary_path).read_text(encoding="utf-8")

            self.assertEqual(first.processed_count, 1)
            self.assertEqual(result.processed_count, 0)
            self.assertEqual(result.status, "ok")
            self.assertNotIn("这条之后会撤回", summary)
            self.assertIn("No active session entries yet", summary)

    def test_session_memory_is_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            store.append_message(_message("old", "默认会话内容"))
            store.append_message(_message("new", "新会话内容", metadata={"session_id": "session_new"}))

            maintainer = MemoryMaintainer(store)
            default_result = maintainer.maintain("conv1", session_id=DEFAULT_SESSION_ID)
            new_result = maintainer.maintain("conv1", session_id="session_new")

            default_summary = Path(default_result.summary_path).read_text(encoding="utf-8")
            new_summary = Path(new_result.summary_path).read_text(encoding="utf-8")

            self.assertIn("默认会话内容", default_summary)
            self.assertNotIn("新会话内容", default_summary)
            self.assertIn("sessions", new_result.memory_dir)
            self.assertIn("新会话内容", new_summary)
            self.assertNotIn("默认会话内容", new_summary)


def _message(message_id: str, text: str, *, metadata: dict | None = None) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=message_id,
        conversation_id="conv1",
        conversation_type="private",
        chat_title="PAGE",
        sender_name="PAGE",
        sender_wechat_id="wxid_page",
        text=text,
        is_self=False,
        received_at="2026-06-29T00:00:00+08:00",
        metadata=metadata or {},
    )


if __name__ == "__main__":
    unittest.main()

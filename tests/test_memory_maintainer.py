from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.conversation.segment import conversation_segment
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.conversation.ledger_context import LedgerContextAssembler
from app.personal_wechat_bot.conversation.session_store import DEFAULT_SESSION_ID
from app.personal_wechat_bot.domain.models import NormalizedMessage, ReplyCandidate, SendResult
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

    def test_maintain_all_recovers_conversation_id_from_readable_ledger_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ConversationLedgerStore(root)
            store.append_message(_message("old", "默认会话内容"))
            store.append_message(_message("new", "新会话内容", metadata={"session_id": "session_new"}))

            results = MemoryMaintainer(ConversationLedgerStore(root)).maintain_all()

            by_session = {result.session_id: result for result in results}
            segment = conversation_segment("conv1", "PAGE")
            self.assertEqual({result.conversation_id for result in results}, {"conv1"})
            self.assertIn(DEFAULT_SESSION_ID, by_session)
            self.assertIn("session_new", by_session)
            self.assertTrue((root / "conversation_ledgers" / segment / "memory" / "summary.md").exists())
            self.assertTrue(
                (root / "conversation_ledgers" / segment / "sessions" / "session_new" / "memory" / "summary.md").exists()
            )
            self.assertFalse((root / "conversation_ledgers" / "PAGE_con" / "memory" / "summary.md").exists())

    def test_agent_outgoing_file_memory_keeps_origin_and_send_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = ConversationLedgerStore(data_dir)
            file_path = data_dir / "agent-result.txt"
            file_path.write_text("result", encoding="utf-8")
            reply = ReplyCandidate(
                message_id="reply-file",
                conversation_id="conv1",
                text="已生成文件。",
                send_mode="confirm",
                model="fake",
                attachments=[
                    {
                        "path": str(file_path),
                        "name": "agent-result.txt",
                        "kind": "document",
                    }
                ],
            )
            entry = store.append_reply(reply)
            store.update_reply_send_result(
                "conv1",
                entry.entry_id,
                SendResult(
                    message_id="bridge:conv1:text",
                    conversation_id="conv1",
                    status="queued_to_bridge",
                    reason="queued_to_non_foreground_bridge:bridge:conv1:text",
                    details={
                        "kind": "multi_part_send",
                        "text": {
                            "status": "queued_to_bridge",
                            "reason": "queued_to_non_foreground_bridge:bridge:conv1:text",
                            "message_id": "bridge:conv1:text",
                        },
                        "files": [
                            {
                                "path": str(file_path),
                                "name": "agent-result.txt",
                                "status": "sent",
                                "reason": "wechat_native_http_send_file_verified",
                                "message_id": "bridge:conv1:file",
                            }
                        ],
                        "bridge_ids": ["bridge:conv1:text", "bridge:conv1:file"],
                        "part_count": 2,
                    },
                ),
            )
            store.update_bridge_send_result(
                "conv1",
                "bridge:conv1:file",
                status="sent",
                reason="wechat_native_http_send_file_verified",
                external_message_id="ext-file",
            )

            result = MemoryMaintainer(store).maintain("conv1")
            summary = Path(result.summary_path).read_text(encoding="utf-8")
            entities = json.loads(Path(result.entities_path).read_text(encoding="utf-8"))
            rendered = LedgerContextAssembler(store).build_snapshot(_message("current", "收到")).render_for_prompt()
            file_entity = entities["files"][0]

            self.assertIn("Recent Files", summary)
            self.assertIn("agent-result.txt", summary)
            self.assertIn("origin=agent", summary)
            self.assertIn("direction=outgoing", summary)
            self.assertIn("send_status=sent", summary)
            self.assertIn("bridge_id=bridge:conv1:file", summary)
            self.assertEqual(file_entity["name"], "agent-result.txt")
            self.assertEqual(file_entity["origin"], "agent")
            self.assertEqual(file_entity["direction"], "outgoing")
            self.assertEqual(file_entity["send_status"], "sent")
            self.assertEqual(file_entity["bridge_id"], "bridge:conv1:file")
            self.assertEqual(file_entity["external_message_id"], "ext-file")
            self.assertIn("origin=agent", rendered)
            self.assertIn("direction=outgoing", rendered)
            self.assertIn("send_status=sent", rendered)
            self.assertIn("bridge_id=bridge:conv1:file", rendered)

    def test_file_only_agent_reply_is_visible_in_memory_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = ConversationLedgerStore(data_dir)
            file_path = data_dir / "only-file.txt"
            file_path.write_text("file result", encoding="utf-8")
            reply = ReplyCandidate(
                message_id="reply-only-file",
                conversation_id="conv1",
                text="",
                send_mode="confirm",
                model="fake",
                attachments=[
                    {
                        "path": str(file_path),
                        "name": "only-file.txt",
                        "kind": "document",
                    }
                ],
            )
            entry = store.append_reply(reply)
            store.update_reply_send_result(
                "conv1",
                entry.entry_id,
                SendResult(
                    message_id="bridge:conv1:fileonly",
                    conversation_id="conv1",
                    status="queued_to_bridge",
                    reason="queued_to_non_foreground_bridge:bridge:conv1:fileonly",
                    details={
                        "kind": "file_send",
                        "files": [
                            {
                                "path": str(file_path),
                                "name": "only-file.txt",
                                "status": "sent",
                                "reason": "wechat_native_http_send_file_verified",
                                "message_id": "bridge:conv1:fileonly",
                            }
                        ],
                        "bridge_ids": ["bridge:conv1:fileonly"],
                        "part_count": 1,
                    },
                ),
            )
            store.update_bridge_send_result(
                "conv1",
                "bridge:conv1:fileonly",
                status="sent",
                reason="wechat_native_http_send_file_verified",
                external_message_id="ext-file-only",
            )

            result = MemoryMaintainer(store).maintain("conv1")
            summary = Path(result.summary_path).read_text(encoding="utf-8")

            self.assertIn("No active session text entries yet", summary)
            self.assertIn("Recent Files", summary)
            self.assertIn("only-file.txt", summary)
            self.assertIn("origin=agent", summary)
            self.assertIn("direction=outgoing", summary)
            self.assertIn("send_status=sent", summary)
            self.assertIn("bridge_id=bridge:conv1:fileonly", summary)

    def test_llm_memory_preserves_deterministic_file_send_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = ConversationLedgerStore(data_dir)
            file_path = data_dir / "llm-file.txt"
            file_path.write_text("file result", encoding="utf-8")
            reply = ReplyCandidate(
                message_id="reply-llm-file",
                conversation_id="conv1",
                text="file attached",
                send_mode="confirm",
                model="fake",
                attachments=[
                    {
                        "path": str(file_path),
                        "name": "llm-file.txt",
                        "kind": "document",
                    }
                ],
            )
            entry = store.append_reply(reply)
            store.update_reply_send_result(
                "conv1",
                entry.entry_id,
                SendResult(
                    message_id="bridge:conv1:llmfile",
                    conversation_id="conv1",
                    status="queued_to_bridge",
                    reason="queued_to_non_foreground_bridge:bridge:conv1:llmfile",
                    details={
                        "files": [
                            {
                                "path": str(file_path),
                                "name": "llm-file.txt",
                                "status": "sent",
                                "reason": "wechat_native_http_send_file_verified",
                                "message_id": "bridge:conv1:llmfile",
                            }
                        ],
                        "bridge_ids": ["bridge:conv1:llmfile"],
                    },
                ),
            )
            store.update_bridge_send_result(
                "conv1",
                "bridge:conv1:llmfile",
                status="sent",
                reason="wechat_native_http_send_file_verified",
                external_message_id="ext-llm-file",
            )
            llm = _JsonMemoryLLM(
                {
                    "summary": {"conversation_review": "remember the attachment"},
                    "preferences": {},
                    "entities": {"files": [{"name": "llm-file.txt"}]},
                }
            )

            result = MemoryMaintainer(store, llm=llm).maintain("conv1")
            summary = Path(result.summary_path).read_text(encoding="utf-8")
            entities = json.loads(Path(result.entities_path).read_text(encoding="utf-8"))
            file_entity = entities["files"][0]

            self.assertIn("Recent Files", summary)
            self.assertIn("bridge_id=bridge:conv1:llmfile", summary)
            self.assertEqual(file_entity["name"], "llm-file.txt")
            self.assertEqual(file_entity["send_status"], "sent")
            self.assertEqual(file_entity["bridge_id"], "bridge:conv1:llmfile")
            self.assertEqual(file_entity["external_message_id"], "ext-llm-file")


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


class _JsonMemoryLLM:
    model = "fake"

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def generate_reply(self, prompt: str, *, workload: str = "interactive") -> str:
        return json.dumps(self.payload, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()

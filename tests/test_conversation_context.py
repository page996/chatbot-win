from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.conversation.context_store import ConversationContextStore, DEFAULT_SESSION_ID
from app.personal_wechat_bot.conversation.prompt_builder import PromptBuilder
from app.personal_wechat_bot.domain.models import NormalizedMessage, SpeakDecision


class ConversationContextStoreTest(unittest.TestCase):
    def test_records_mixed_message_context_for_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationContextStore(Path(tmp), max_recent_messages=5)
            message = _message(
                text="请分析这个文件",
                metadata={
                    "attachments": [
                        {
                            "status": "indexed",
                            "file_id": "file123",
                            "name": "report.pdf",
                            "kind": "file",
                            "workspace": {"workspace_dir": "workspace/c1/session_default/file123"},
                            "artifacts": {"content_path": "workspace/c1/session_default/file123/derived/content.md"},
                            "parse": {
                                "status": "parsed",
                                "kind": "pdf",
                                "summary": "已提取 PDF 文本预览",
                                "text": "第一段内容\n第二段内容",
                            },
                        }
                    ]
                },
            )

            store.record_message(message)
            snapshot = store.build_snapshot(message)
            prompt = PromptBuilder().build(
                message,
                SpeakDecision(message.conversation_id, "speak", "private_chat_allowed"),
                context_snapshot=snapshot,
            )

            self.assertEqual(snapshot.session_id, DEFAULT_SESSION_ID)
            self.assertIn("近期消息", prompt)
            self.assertIn("report.pdf", prompt)
            self.assertIn("第一段内容", prompt)
            self.assertIn("模型不能直接访问微信原始文件", prompt)
            self.assertIn("混合上下文分析", prompt)
            self.assertIn("file_analysis_or_processing_task", prompt)
            self.assertNotIn("[后台附件内容]", snapshot.recent_messages[0]["text"])

    def test_clear_context_command_switches_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationContextStore(Path(tmp))
            message = _message(text="清空当前对话上下文")

            new_session = store.maybe_reset_for_message(message)
            store.record_message(message)
            snapshot = store.build_snapshot(message)

            self.assertIsNotNone(new_session)
            self.assertEqual(snapshot.session_id, new_session)
            self.assertNotEqual(snapshot.session_id, DEFAULT_SESSION_ID)

    def test_quote_context_includes_neighbor_messages_and_file_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationContextStore(Path(tmp), max_recent_messages=10)
            before = _message("上文一", message_id="m-before")
            quoted = _message(
                "请看这个报告里的第二段",
                message_id="m-quoted",
                metadata={
                    "attachments": [
                        {
                            "status": "indexed",
                            "file_id": "file-quote",
                            "name": "quoted-report.pdf",
                            "kind": "file",
                            "parse": {"status": "parsed", "summary": "quoted summary", "text": "quoted body"},
                        }
                    ]
                },
            )
            after = _message("后续补充", message_id="m-after")
            current = _message(
                "这条引用里的任务继续处理",
                message_id="m-current",
                metadata={"quote": {"message_id": "m-quoted", "text": "报告里的第二段", "sender_name": "PAGE"}},
            )

            for item in [before, quoted, after, current]:
                store.record_message(item)

            snapshot = store.build_snapshot(current)
            prompt = PromptBuilder().build(
                current,
                SpeakDecision(current.conversation_id, "speak", "private_chat_allowed"),
                context_snapshot=snapshot,
            )

            self.assertEqual(snapshot.quote_refs[0]["match"]["status"], "found")
            self.assertEqual(snapshot.quote_refs[0]["match"]["message_id"], "m-quoted")
            self.assertIn("上文一", prompt)
            self.assertIn("请看这个报告里的第二段", prompt)
            self.assertIn("后续补充", prompt)
            self.assertIn("quoted-report.pdf", prompt)
            self.assertIn("引用消息上下文", prompt)

    def test_quote_context_can_match_by_text_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationContextStore(Path(tmp), max_recent_messages=10)
            quoted = _message("上一条没有 message id 也能被引用", message_id="m-1")
            current = _message(
                "引用这条继续说",
                message_id="m-2",
                metadata={"quote": {"text": "没有 message id 也能被引用"}},
            )

            store.record_message(quoted)
            store.record_message(current)

            snapshot = store.build_snapshot(current)

            self.assertEqual(snapshot.quote_refs[0]["match"]["status"], "found")
            self.assertEqual(snapshot.quote_refs[0]["match"]["message_id"], "m-1")

    def test_empty_quote_source_does_not_create_quote_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationContextStore(Path(tmp), max_recent_messages=10)
            message = _message("普通消息", metadata={"quote": {"source": "append_backend_event_cli"}})

            store.record_message(message)
            snapshot = store.build_snapshot(message)

            self.assertEqual(snapshot.quote_refs, [])


def _message(text: str, metadata: dict | None = None, message_id: str = "msg1") -> NormalizedMessage:
    return NormalizedMessage(
        message_id=message_id,
        conversation_id="conv1",
        conversation_type="private",
        chat_title="PAGE",
        sender_name="PAGE",
        sender_wechat_id="PAGE",
        text=text,
        is_self=False,
        received_at="2026-06-29T00:00:00+08:00",
        metadata=metadata or {},
    )


if __name__ == "__main__":
    unittest.main()

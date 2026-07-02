from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.config.loader import accept_contact, create_default_config, load_config
from app.personal_wechat_bot.conversation.context_store import CLEAR_CONTEXT_PHRASES, DEFAULT_SESSION_ID
from app.personal_wechat_bot.domain.models import RawWeChatMessage
from app.personal_wechat_bot.processor.message_processor import MessageProcessor


class MessageProcessorTest(unittest.TestCase):
    def test_processor_records_self_message_without_replying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            processor = MessageProcessor(runtime)

            result = processor.process(
                RawWeChatMessage(
                    raw_id="self-1",
                    chat_title="小明",
                    sender_name="me",
                    text="hello",
                    is_self=True,
                )
            )

            self.assertIsNotNone(result)
            self.assertTrue(result["message"]["is_self"])
            self.assertTrue(result["context_only"])
            self.assertNotIn("reply", result)
            entries = runtime.ledger_store.read_entries(result["message"]["conversation_id"])
            self.assertEqual(len(entries), 1)
            self.assertTrue(entries[0].is_self)

    def test_processor_runs_private_accepted_channel_closed_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            accept_contact(data_dir, "wxid_alice")
            runtime = build_runtime(load_config(data_dir))
            processor = MessageProcessor(runtime)

            result = processor.process(
                RawWeChatMessage(
                    raw_id="msg-1",
                    chat_title="Alice",
                    sender_name="Alice",
                    sender_wechat_id="wxid_alice",
                    text="今天有点累",
                    observed_at="2026-06-28T01:00:00+00:00",
                )
            )

            self.assertIsNotNone(result)
            self.assertEqual(result["route"]["action"], "process")
            self.assertEqual(result["send"]["status"], "skipped")
            entries = runtime.ledger_store.read_entries(result["message"]["conversation_id"])
            self.assertEqual(entries[-1].role, "assistant")
            self.assertEqual(entries[-1].send["status"], "skipped")
            self.assertEqual(entries[-1].send["reason"], "dry_run")

    def test_processor_auto_registers_unknown_private_and_group_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            processor = MessageProcessor(runtime)

            private_result = processor.process(
                RawWeChatMessage(
                    raw_id="private-new",
                    chat_title="New Friend",
                    sender_name="New Friend",
                    sender_wechat_id="wxid_new_friend",
                    text="hello",
                    observed_at="2026-06-28T01:00:00+00:00",
                    driver_meta={"source": "backend_events_jsonl"},
                )
            )
            group_result = processor.process(
                RawWeChatMessage(
                    raw_id="group-new",
                    chat_title="New Group",
                    sender_name="Group Member",
                    sender_wechat_id="wxid_member",
                    text="@bot hello",
                    is_group=True,
                    observed_at="2026-06-28T01:01:00+00:00",
                    driver_meta={"source": "backend_events_jsonl", "topic_decision": "speak"},
                )
            )
            channels = runtime.channel_store.list_channels()
            channel_types = {item.conversation_type for item in channels}

            self.assertEqual(private_result["route"]["action"], "process")
            self.assertEqual(group_result["route"]["action"], "process")
            self.assertEqual(channel_types, {"private", "group"})
            self.assertTrue((data_dir / "conversation_channels" / "index.json").exists())

    def test_processor_runs_link_annotation_after_ledger_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            runtime.link_annotations = _FakeLinkAnnotations(runtime.ledger_store)
            processor = MessageProcessor(runtime)

            result = processor.process(
                RawWeChatMessage(
                    raw_id="msg-link",
                    chat_title="PAGE",
                    sender_name="PAGE",
                    text="read https://example.com/a",
                    observed_at="2026-06-28T01:00:00+00:00",
                )
            )

            self.assertIsNotNone(result)
            self.assertEqual(result["link_annotations"][0]["status"], "ok")
            entries = runtime.ledger_store.read_entries(result["message"]["conversation_id"])
            self.assertEqual(entries[0].links[0]["status"], "completed")

    def test_processor_threads_current_session_into_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            processor = MessageProcessor(runtime)

            result = processor.process(
                RawWeChatMessage(
                    raw_id="clear-context",
                    chat_title="PAGE",
                    sender_name="PAGE",
                    text=CLEAR_CONTEXT_PHRASES[0],
                    observed_at="2026-06-28T01:00:00+00:00",
                )
            )

            self.assertIsNotNone(result)
            session_id = result["message"]["metadata"]["session_id"]
            entries = runtime.ledger_store.read_entries(result["message"]["conversation_id"])
            self.assertNotEqual(session_id, DEFAULT_SESSION_ID)
            self.assertEqual(result["context"]["session_id"], session_id)
            self.assertEqual(entries[0].session_id, session_id)
            self.assertEqual(entries[-1].session_id, session_id)


class _FakeLinkAnnotations:
    def __init__(self, ledger_store):
        self.ledger_store = ledger_store

    def annotate_entry(self, entry):
        url_id = entry.links[0]["url_id"]
        self.ledger_store.annotate_link(
            entry.conversation_id,
            entry.entry_id,
            url_id,
            status="completed",
            summary="summary",
            text="page text",
            source_path="web_fetch/page.md",
        )
        return [{"status": "ok", "url_id": url_id}]


if __name__ == "__main__":
    unittest.main()

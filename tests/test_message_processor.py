from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.config.loader import accept_contact, create_default_config, load_config
from app.personal_wechat_bot.conversation.session_store import CLEAR_CONTEXT_PHRASES, DEFAULT_SESSION_ID
from app.personal_wechat_bot.conversation.segment import conversation_segment
from app.personal_wechat_bot.domain.models import RawWeChatMessage
from app.personal_wechat_bot.processor.message_processor import MessageProcessor
from app.personal_wechat_bot.tasks.manager import TaskStatusStore
from app.personal_wechat_bot.tools.permissions import resolve_allowed_roots
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import BackendAttachmentParser
from app.personal_wechat_bot.wechat_driver.backend_events import BackendEventJsonlDriver, append_backend_event


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

    def test_processor_projects_reply_and_send_subtasks_to_channel_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            accept_contact(data_dir, "wxid_alice")
            runtime = build_runtime(load_config(data_dir))
            processor = MessageProcessor(runtime)

            result = processor.process(
                RawWeChatMessage(
                    raw_id="msg-task",
                    chat_title="Alice",
                    sender_name="Alice",
                    sender_wechat_id="wxid_alice",
                    text="帮我看下这个安排",
                    observed_at="2026-06-28T01:00:00+00:00",
                )
            )

            self.assertIsNotNone(result)
            message_id = result["message"]["message_id"]
            state = TaskStatusStore(data_dir).state()
            tasks = {item["task_id"]: item for item in state["tasks"]}

            self.assertEqual(tasks[f"reply-{message_id}"]["status"], "completed")
            self.assertEqual(tasks[f"reply-{message_id}"]["kind"], "reply")
            self.assertEqual(tasks[f"send-{message_id}"]["status"], "completed")
            self.assertEqual(tasks[f"send-{message_id}"]["kind"], "send")
            self.assertEqual(state["channels"][0]["conversation_id"], result["message"]["conversation_id"])
            lane_task_ids = {item["task_id"] for item in state["channels"][0]["history"]}
            self.assertIn(f"reply-{message_id}", lane_task_ids)
            self.assertIn(f"send-{message_id}", lane_task_ids)

    def test_processor_blocks_unknown_private_but_auto_registers_group_channel(self) -> None:
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

            self.assertEqual(private_result["route"]["action"], "ignore")
            self.assertTrue(private_result["blocked"])
            self.assertIn("private_contact_unknown_or_unidentified", private_result["route"]["reason"])
            self.assertEqual(group_result["route"]["action"], "process")
            self.assertEqual(channel_types, {"group"})
            self.assertTrue((data_dir / "conversation_channels" / "index.json").exists())

    def test_processor_blocks_wechat_user_placeholder_private_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))

            result = MessageProcessor(runtime).process(
                RawWeChatMessage(
                    raw_id="private-wechat-user-placeholder",
                    chat_title="微信用户",
                    sender_name="微信用户",
                    text="deliver me",
                    observed_at="2026-07-10T08:07:00+00:00",
                    driver_meta={"source": "backend_events_jsonl", "banner": "对方还不是你的朋友"},
                )
            )

            self.assertIsNotNone(result)
            self.assertEqual(result["route"]["action"], "ignore")
            self.assertTrue(result["blocked"])
            self.assertIn("private_contact_explicitly_not_friend", result["route"]["reason"])
            self.assertEqual(runtime.channel_store.list_channels(), [])

    def test_processor_auto_registers_explicit_friend_private_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))

            result = MessageProcessor(runtime).process(
                RawWeChatMessage(
                    raw_id="private-friend",
                    chat_title="Alice",
                    sender_name="Alice",
                    sender_wechat_id="wxid_alice",
                    text="hello",
                    observed_at="2026-06-28T01:00:00+00:00",
                    driver_meta={"source": "backend_events_jsonl", "conversation_key": "wxid_alice", "is_friend": True},
                )
            )
            channels = runtime.channel_store.list_channels()

            self.assertEqual(result["route"]["action"], "process")
            self.assertEqual(len(channels), 1)
            self.assertTrue(channels[0].is_friend)

    def test_processor_blocks_legacy_unidentified_existing_private_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            legacy = runtime.normalizer.normalize(
                RawWeChatMessage(
                    raw_id="legacy-channel-seed",
                    chat_title="wxid_ghost",
                    sender_name="wxid_ghost",
                    sender_wechat_id="wxid_ghost",
                    text="legacy",
                    driver_meta={
                        "source": "weflow_discovery",
                        "trusted_channel_source": True,
                        "conversation_key": "wxid_ghost",
                    },
                )
            )
            assert legacy is not None
            runtime.channel_store.ensure_channel(legacy)

            result = MessageProcessor(runtime).process(
                RawWeChatMessage(
                    raw_id="legacy-channel-new",
                    chat_title="wxid_ghost",
                    sender_name="wxid_ghost",
                    sender_wechat_id="wxid_ghost",
                    text="should not open",
                    observed_at="2026-06-28T01:00:00+00:00",
                    driver_meta={"source": "backend_events_jsonl", "conversation_key": "wxid_ghost"},
                )
            )

            self.assertIsNotNone(result)
            self.assertEqual(result["route"]["action"], "ignore")
            self.assertIn("private_contact_unknown_or_unidentified", result["route"]["reason"])
            self.assertTrue(result["blocked"])
            self.assertEqual(runtime.ledger_store.read_entries(legacy.conversation_id), [])

    def test_processor_keeps_identified_existing_private_channel_when_incoming_title_is_wxid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            legacy = runtime.normalizer.normalize(
                RawWeChatMessage(
                    raw_id="identified-channel-seed",
                    chat_title="Alice",
                    sender_name="Alice",
                    sender_wechat_id="wxid_alice",
                    text="legacy",
                    driver_meta={
                        "source": "weflow_discovery",
                        "trusted_channel_source": True,
                        "conversation_key": "wxid_alice",
                        "is_friend": True,
                    },
                )
            )
            assert legacy is not None
            runtime.channel_store.ensure_channel(legacy)

            result = MessageProcessor(runtime).process(
                RawWeChatMessage(
                    raw_id="identified-channel-new",
                    chat_title="wxid_alice",
                    sender_name="wxid_alice",
                    sender_wechat_id="wxid_alice",
                    text="hello again",
                    observed_at="2026-06-28T01:00:00+00:00",
                    driver_meta={"source": "backend_events_jsonl", "conversation_key": "wxid_alice"},
                )
            )

            self.assertIsNotNone(result)
            self.assertEqual(result["route"]["action"], "process")
            self.assertNotIn("blocked", result)

    def test_processor_skips_duplicate_dedupe_key_without_rewriting_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            processor = MessageProcessor(runtime)
            meta = {
                "source": "backend_events_jsonl",
                "conversation_key": "wxid_page",
                "is_friend": True,
                "local_id": "1",
                "create_time": "100",
                "sort_key": "100",
                "context_only": True,
            }

            first = processor.process(
                RawWeChatMessage(
                    raw_id="weflow:message:wxid_page:E%3A%5Cdb%5Cmessage_0.db:Msg_a:1",
                    chat_title="PAGE",
                    sender_name="PAGE",
                    sender_wechat_id="wxid_page",
                    text="hello",
                    observed_at="2026-06-28T01:00:00+00:00",
                    driver_meta={**meta, "message_key": "E%3A%5Cdb%5Cmessage_0.db:Msg_a:1"},
                )
            )
            second = processor.process(
                RawWeChatMessage(
                    raw_id="weflow:message:wxid_page:E%3A%5Cother%5Cmessage_0.db:Msg_b:1",
                    chat_title="PAGE",
                    sender_name="PAGE",
                    sender_wechat_id="wxid_page",
                    text="hello",
                    observed_at="2026-06-28T01:00:00+00:00",
                    driver_meta={**meta, "message_key": "E%3A%5Cother%5Cmessage_0.db:Msg_b:1"},
                )
            )

            self.assertEqual(first["ledger"]["status"], "created")
            self.assertEqual(second["route"]["action"], "duplicate")
            self.assertEqual(second["ledger"]["status"], "duplicate")
            self.assertTrue(second["context_only"])
            self.assertNotIn("reply", second)
            entries = runtime.ledger_store.read_entries(first["message"]["conversation_id"])
            self.assertEqual(len(entries), 1)

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
                    text=f"@bot {CLEAR_CONTEXT_PHRASES[0]}",
                    observed_at="2026-06-28T01:00:00+00:00",
                )
            )

            self.assertIsNotNone(result)
            session_id = result["message"]["metadata"]["session_id"]
            entries = runtime.ledger_store.read_entries(result["message"]["conversation_id"])
            self.assertNotEqual(session_id, DEFAULT_SESSION_ID)
            self.assertEqual(result["context"]["session_id"], session_id)
            self.assertTrue(result["context_only"])
            self.assertNotIn("reply", result)
            self.assertEqual(entries[0].session_id, session_id)
            self.assertEqual(entries[-1].session_id, session_id)
            self.assertEqual(entries[0].text_blocks[0]["kind"], "control:session_reset")
            self.assertFalse(entries[0].text_blocks[0]["metadata"]["visible_in_context"])
            markdown = runtime.ledger_store.conversation_markdown_path(result["message"]["conversation_id"]).read_text(encoding="utf-8")
            self.assertIn("[control:session_reset status=applied", markdown)
            self.assertIn("hidden_text=true", markdown)
            self.assertNotIn(CLEAR_CONTEXT_PHRASES[0], markdown)

    def test_processor_does_not_reset_session_for_unmentioned_clear_context_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            processor = MessageProcessor(runtime)

            result = processor.process(
                RawWeChatMessage(
                    raw_id="clear-context-bare",
                    chat_title="PAGE",
                    sender_name="PAGE",
                    text=CLEAR_CONTEXT_PHRASES[0],
                    observed_at="2026-06-28T01:00:00+00:00",
                    driver_meta={"context_only": True},
                )
            )

            self.assertIsNotNone(result)
            self.assertEqual(result["message"]["metadata"]["session_id"], DEFAULT_SESSION_ID)
            self.assertNotIn("context", result)

    def test_first_backend_attachment_message_uses_one_segment_for_all_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            runtime = build_runtime(config)
            driver = BackendEventJsonlDriver(
                data_dir / "backend_events.jsonl",
                runtime.file_index,
                allowed_input_roots=resolve_allowed_roots(data_dir, config.file_read_roots),
                allowed_extensions=config.file_allowed_extensions,
                max_input_bytes=config.file_max_bytes,
                attachment_parser=BackendAttachmentParser(),
                file_workspace=runtime.file_workspace,
                session_store=runtime.session_store,
            )
            runtime.active_driver = driver
            note = data_dir / "inbox" / "note.txt"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text("attachment body", encoding="utf-8")
            append_backend_event(
                data_dir / "backend_events.jsonl",
                chat_title="First Title",
                sender_name="First Title",
                sender_wechat_id="wxid_first_title",
                text="please read",
                attachments=["note.txt"],
                source_payload={"is_friend": True},
            )
            raw = driver.read_new_messages()[0]

            result = MessageProcessor(runtime).process(raw)

            self.assertIsNotNone(result)
            conversation_id = result["message"]["conversation_id"]
            session_id = result["message"]["metadata"]["session_id"]
            segment = conversation_segment(conversation_id, "First Title")
            self.assertTrue((data_dir / "conversation_channels" / segment / "channel.json").exists())
            self.assertTrue((data_dir / "conversation_sessions" / segment / "state.json").exists())
            self.assertTrue((data_dir / "conversation_ledgers" / segment / "messages.jsonl").exists())
            self.assertTrue((data_dir / "file_workspace" / segment / session_id).exists())
            attachment = result["message"]["metadata"]["attachments"][0]
            self.assertIn(segment, attachment["workspace"]["workspace_dir"])


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

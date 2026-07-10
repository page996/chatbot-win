from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.config.loader import create_default_config, load_config
from app.personal_wechat_bot.conversation.session_store import CLEAR_CONTEXT_PHRASES, DEFAULT_SESSION_ID
from app.personal_wechat_bot.domain.models import RawWeChatMessage
from app.personal_wechat_bot.processor.message_processor import MessageProcessor
from app.personal_wechat_bot.runtime.conversation_scheduler import ConversationScheduler


class ConversationSchedulerTest(unittest.TestCase):
    def test_scheduler_preserves_order_within_conversation(self) -> None:
        seen: list[tuple[str, str]] = []
        lock = threading.Lock()

        def handle(raw: RawWeChatMessage) -> dict:
            time.sleep(0.01)
            with lock:
                seen.append((raw.chat_title, raw.raw_id))
            return {"raw_id": raw.raw_id}

        messages = [
            RawWeChatMessage(raw_id="a1", chat_title="A", sender_name="u", text="1"),
            RawWeChatMessage(raw_id="a2", chat_title="A", sender_name="u", text="2"),
            RawWeChatMessage(raw_id="b1", chat_title="B", sender_name="u", text="1"),
            RawWeChatMessage(raw_id="b2", chat_title="B", sender_name="u", text="2"),
        ]

        result = ConversationScheduler(handle, max_parallel_conversations=2).process_batch(messages)

        self.assertLess(seen.index(("A", "a1")), seen.index(("A", "a2")))
        self.assertLess(seen.index(("B", "b1")), seen.index(("B", "b2")))
        self.assertEqual(result.max_running_seen, 2)

    def test_scheduler_caps_parallel_conversations(self) -> None:
        running = 0
        max_running = 0
        lock = threading.Lock()

        def handle(raw: RawWeChatMessage) -> dict:
            nonlocal running, max_running
            with lock:
                running += 1
                max_running = max(max_running, running)
            time.sleep(0.02)
            with lock:
                running -= 1
            return {"raw_id": raw.raw_id}

        messages = [
            RawWeChatMessage(raw_id=f"m{i}", chat_title=f"C{i}", sender_name="u", text="hello")
            for i in range(5)
        ]

        result = ConversationScheduler(handle, max_parallel_conversations=2).process_batch(messages)

        self.assertEqual(len(result.processed), 5)
        self.assertLessEqual(max_running, 2)
        self.assertEqual(result.max_running_seen, 2)

    def test_scheduler_isolates_single_message_failure(self) -> None:
        def handle(raw: RawWeChatMessage) -> dict:
            if raw.raw_id == "bad":
                raise RuntimeError("boom")
            return {"raw_id": raw.raw_id}

        messages = [
            RawWeChatMessage(raw_id="good-1", chat_title="A", sender_name="u", text="1"),
            RawWeChatMessage(raw_id="bad", chat_title="B", sender_name="u", text="2"),
            RawWeChatMessage(raw_id="good-2", chat_title="C", sender_name="u", text="3"),
        ]

        result = ConversationScheduler(handle, max_parallel_conversations=3).process_batch(messages)

        raw_ids = {item.get("raw_id") for item in result.processed}
        errors = [item for item in result.processed if item.get("error")]
        self.assertIn("good-1", raw_ids)
        self.assertIn("good-2", raw_ids)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["raw_id"], "bad")

    def test_scheduler_defers_earlier_live_messages_per_conversation(self) -> None:
        def handle(raw: RawWeChatMessage) -> dict:
            return {"raw_id": raw.raw_id, "meta": raw.driver_meta}

        messages = [
            RawWeChatMessage(raw_id="a1", chat_title="A", sender_name="u", text="first"),
            RawWeChatMessage(raw_id="a2", chat_title="A", sender_name="u", text="second"),
            RawWeChatMessage(raw_id="b1", chat_title="B", sender_name="u", text="first"),
            RawWeChatMessage(raw_id="b2", chat_title="B", sender_name="u", text="second"),
        ]

        result = ConversationScheduler(handle, max_parallel_conversations=2).process_batch(messages)

        by_raw_id = {item["raw_id"]: item for item in result.processed}
        self.assertTrue(by_raw_id["a1"]["meta"]["context_only"])
        self.assertTrue(by_raw_id["a1"]["meta"]["deferred_reply"])
        self.assertEqual(by_raw_id["a1"]["meta"]["deferred_reply_anchor_raw_id"], "a2")
        self.assertNotIn("context_only", by_raw_id["a2"]["meta"])
        self.assertTrue(by_raw_id["b1"]["meta"]["context_only"])
        self.assertEqual(by_raw_id["b1"]["meta"]["deferred_reply_anchor_raw_id"], "b2")
        self.assertNotIn("context_only", by_raw_id["b2"]["meta"])

    def test_scheduler_keeps_ledger_linear_for_batched_private_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            processor = MessageProcessor(runtime)
            messages = [
                RawWeChatMessage(
                    raw_id="m1",
                    chat_title="Alice",
                    sender_name="Alice",
                    text="first",
                    observed_at="2026-06-28T01:00:00+00:00",
                ),
                RawWeChatMessage(
                    raw_id="m2",
                    chat_title="Alice",
                    sender_name="Alice",
                    text="second",
                    observed_at="2026-06-28T01:00:01+00:00",
                ),
            ]

            result = ConversationScheduler(processor.process, max_parallel_conversations=1).process_batch(messages)

            self.assertEqual(len(result.processed), 2)
            self.assertTrue(result.processed[0]["context_only"])
            self.assertNotIn("reply", result.processed[0])
            self.assertIn("reply", result.processed[1])
            conversation_id = result.processed[1]["message"]["conversation_id"]
            entries = runtime.ledger_store.read_entries(conversation_id)
            self.assertEqual([entry.role for entry in entries], ["user", "user", "assistant"])
            self.assertEqual(entries[0].text_blocks[0]["text"], "first")
            self.assertEqual(entries[1].text_blocks[0]["text"], "second")
            self.assertTrue(entries[0].text_blocks[0]["metadata"]["context_only"])
            self.assertTrue(entries[0].text_blocks[0]["metadata"]["deferred_reply"])

    def test_scheduler_applies_reset_before_later_message_in_same_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            processor = MessageProcessor(runtime)
            messages = [
                RawWeChatMessage(
                    raw_id="reset-first",
                    chat_title="Alice",
                    sender_name="Alice",
                    text=f"@bot {CLEAR_CONTEXT_PHRASES[0]}",
                    observed_at="2026-06-28T01:00:00+00:00",
                ),
                RawWeChatMessage(
                    raw_id="after-reset",
                    chat_title="Alice",
                    sender_name="Alice",
                    text="continue in a clean session",
                    observed_at="2026-06-28T01:00:01+00:00",
                ),
            ]

            result = ConversationScheduler(processor.process, max_parallel_conversations=1).process_batch(messages)

            self.assertEqual(len(result.processed), 2)
            self.assertTrue(result.processed[0]["context"]["reset"])
            self.assertIn("reply", result.processed[1])
            conversation_id = result.processed[1]["message"]["conversation_id"]
            state = runtime.session_store.state_for_conversation(conversation_id)
            self.assertNotEqual(state["current_session_id"], DEFAULT_SESSION_ID)
            entries = runtime.ledger_store.read_entries(conversation_id)
            self.assertEqual([entry.role for entry in entries], ["user", "user", "assistant"])
            self.assertTrue(all(entry.session_id == state["current_session_id"] for entry in entries))


if __name__ == "__main__":
    unittest.main()

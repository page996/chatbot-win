from __future__ import annotations

import threading
import time
import unittest

from app.personal_wechat_bot.domain.models import RawWeChatMessage
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


if __name__ == "__main__":
    unittest.main()

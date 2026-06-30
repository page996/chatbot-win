from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.conversation.session_store import (
    CLEAR_CONTEXT_PHRASES,
    DEFAULT_SESSION_ID,
    ConversationSessionStore,
    is_reset_command,
)
from app.personal_wechat_bot.domain.models import NormalizedMessage


class ConversationSessionStoreTest(unittest.TestCase):
    def test_current_session_defaults_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationSessionStore(Path(tmp))

            first = store.current_session_id("conv1")
            second = ConversationSessionStore(Path(tmp)).current_session_id("conv1")

            self.assertEqual(first, DEFAULT_SESSION_ID)
            self.assertEqual(second, DEFAULT_SESSION_ID)
            self.assertTrue((Path(tmp) / "conversation_sessions" / "conv1" / "state.json").exists())

    def test_clear_context_command_switches_session_without_context_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationSessionStore(Path(tmp))

            new_session = store.maybe_reset_for_message(
                _message("m1", f"@bot {CLEAR_CONTEXT_PHRASES[0]}")
            )
            state_path = Path(tmp) / "conversation_sessions" / "conv1" / "state.json"
            events_path = Path(tmp) / "conversation_sessions" / "conv1" / "events.jsonl"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]

            self.assertIsNotNone(new_session)
            self.assertNotEqual(new_session, DEFAULT_SESSION_ID)
            self.assertEqual(store.current_session_id("conv1"), new_session)
            self.assertEqual(state["current_session_id"], new_session)
            self.assertEqual(events[-1]["type"], "session.reset")
            self.assertFalse(hasattr(store, "record_message"))
            self.assertFalse(hasattr(store, "build_snapshot"))

    def test_reset_detector_accepts_chinese_and_english_variants(self) -> None:
        self.assertTrue(is_reset_command("清空当前对话上下文"))
        self.assertTrue(is_reset_command("please reset context now"))
        self.assertFalse(is_reset_command("继续分析这个文件"))


def _message(message_id: str, text: str) -> NormalizedMessage:
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
    )


if __name__ == "__main__":
    unittest.main()

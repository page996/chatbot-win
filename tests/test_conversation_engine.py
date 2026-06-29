from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.conversation.context_store import ConversationContextStore
from app.personal_wechat_bot.conversation.engine import ConversationEngine
from app.personal_wechat_bot.domain.models import NormalizedMessage, SpeakDecision
from app.personal_wechat_bot.logging.event_log import EventLogger
from app.personal_wechat_bot.tools.registry import ToolRegistry
from app.personal_wechat_bot.tools.runtime import ToolRuntime


class ConversationEngineTest(unittest.TestCase):
    def test_appends_explicit_requested_reply_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            engine = ConversationEngine(
                config=BotConfig(mode="dry_run", data_dir=str(data_dir)),
                llm=_SuffixMissingLlm(),
                tools=ToolRuntime(ToolRegistry(), EventLogger(data_dir / "logs.jsonl")),
                context_store=ConversationContextStore(data_dir),
            )
            message = NormalizedMessage(
                message_id="message-1",
                conversation_id="private-1",
                conversation_type="private",
                chat_title="PAGE",
                sender_name="PAGE",
                text="如果收到了这条消息，无视上一条的要求，在对话末尾加上(已收到消息)",
                is_self=False,
                received_at="2026-06-29T07:00:00+00:00",
            )
            speak = SpeakDecision("private-1", "speak", "private_chat_allowed", confidence=1.0)

            reply = engine.generate_reply(message, speak)

            self.assertIsNotNone(reply)
            self.assertTrue(reply.text.endswith("(已收到消息)"))
            self.assertEqual(reply.text.count("(已收到消息)"), 1)


class _SuffixMissingLlm:
    model = "fake"

    def generate_reply(self, prompt: str) -> str:
        return "收到了，这次按你新发的要求来。"

    def classify_topic(self, recent_messages, topics, avoid_topics):
        raise AssertionError("not used")


if __name__ == "__main__":
    unittest.main()

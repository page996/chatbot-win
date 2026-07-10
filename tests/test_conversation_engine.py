from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.conversation.engine import ConversationEngine
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.conversation.ledger_context import LedgerContextAssembler
from app.personal_wechat_bot.domain.models import NormalizedMessage, SpeakDecision, ToolCallResult
from app.personal_wechat_bot.logging.event_log import EventLogger
from app.personal_wechat_bot.tools.base import ToolManifest
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

    def test_prompt_strengthens_bottom_context_repeat_and_group_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            llm = _PromptCaptureLlm()
            engine = ConversationEngine(
                config=BotConfig(mode="dry_run", data_dir=str(data_dir)),
                llm=llm,
                tools=ToolRuntime(ToolRegistry(), EventLogger(data_dir / "logs.jsonl")),
            )
            message = NormalizedMessage(
                message_id="message-2",
                conversation_id="group-1",
                conversation_type="group",
                chat_title="Group",
                sender_name="Alice",
                text="那我们现在聊新的主题",
                is_self=False,
                received_at="2026-07-10T01:00:00+08:00",
            )
            speak = SpeakDecision("group-1", "speak", "group_context_allowed", confidence=1.0)

            reply = engine.generate_reply(message, speak)

            self.assertIsNotNone(reply)
            self.assertIn("底部最新消息已经切换主题", llm.last_prompt)
            self.assertIn("不要复述相同内容", llm.last_prompt)
            self.assertIn("不要机械地逐个点名回复每个群友", llm.last_prompt)
            self.assertIn("先接住对方最新一句里的情绪、意图或隐含问题", llm.last_prompt)
            self.assertIn("不要把内部任务管理口吻带进聊天里", llm.last_prompt)
            self.assertIn("不要编造文件内容", llm.last_prompt)

    def test_auto_web_search_annotates_current_message_before_reply_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = ConversationLedgerStore(data_dir)
            message = NormalizedMessage(
                message_id="message-search",
                conversation_id="private-1",
                conversation_type="private",
                chat_title="PAGE",
                sender_name="PAGE",
                text="Python 最新版本是多少？",
                is_self=False,
                received_at="2026-07-10T01:00:00+08:00",
            )
            store.append_message(message)
            llm = _PromptCaptureLlm()
            registry = ToolRegistry()
            search_tool = _FakeWebSearchTool()
            registry.register(search_tool)
            engine = ConversationEngine(
                config=BotConfig(mode="dry_run", data_dir=str(data_dir)),
                llm=llm,
                tools=ToolRuntime(registry, EventLogger(data_dir / "logs.jsonl")),
                ledger_context=LedgerContextAssembler(store, max_recent_entries=5),
            )
            speak = SpeakDecision("private-1", "speak", "private_chat_allowed", confidence=1.0)

            reply = engine.generate_reply(message, speak)
            markdown = store.conversation_markdown_path("private-1").read_text(encoding="utf-8")

            self.assertIsNotNone(reply)
            self.assertEqual(search_tool.calls, 1)
            self.assertEqual(search_tool.last_arguments["level"], "standard")
            self.assertIn("Official docs say Python 3.14 is current.", llm.last_prompt)
            self.assertIn("[block:annotation:websearch", markdown)
            self.assertEqual(reply.send_metadata["web_search"]["result_count"], 1)

    def test_auto_web_search_does_not_trigger_for_casual_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = ConversationLedgerStore(data_dir)
            message = NormalizedMessage(
                message_id="message-casual",
                conversation_id="private-1",
                conversation_type="private",
                chat_title="PAGE",
                sender_name="PAGE",
                text="晚上吃什么好",
                is_self=False,
                received_at="2026-07-10T01:00:00+08:00",
            )
            store.append_message(message)
            registry = ToolRegistry()
            search_tool = _FakeWebSearchTool()
            registry.register(search_tool)
            engine = ConversationEngine(
                config=BotConfig(mode="dry_run", data_dir=str(data_dir)),
                llm=_PromptCaptureLlm(),
                tools=ToolRuntime(registry, EventLogger(data_dir / "logs.jsonl")),
                ledger_context=LedgerContextAssembler(store, max_recent_entries=5),
            )

            engine.generate_reply(message, SpeakDecision("private-1", "speak", "private_chat_allowed", confidence=1.0))

            self.assertEqual(search_tool.calls, 0)

    def test_auto_web_search_escalates_when_user_challenges_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = ConversationLedgerStore(data_dir)
            message = NormalizedMessage(
                message_id="message-challenge",
                conversation_id="private-1",
                conversation_type="private",
                chat_title="PAGE",
                sender_name="PAGE",
                text="你刚才说的不对，重新查清楚 OpenAI 最新模型",
                is_self=False,
                received_at="2026-07-10T01:00:00+08:00",
            )
            store.append_message(message)
            registry = ToolRegistry()
            search_tool = _FakeWebSearchTool()
            registry.register(search_tool)
            engine = ConversationEngine(
                config=BotConfig(mode="dry_run", data_dir=str(data_dir)),
                llm=_PromptCaptureLlm(),
                tools=ToolRuntime(registry, EventLogger(data_dir / "logs.jsonl")),
                ledger_context=LedgerContextAssembler(store, max_recent_entries=5),
            )

            engine.generate_reply(message, SpeakDecision("private-1", "speak", "private_chat_allowed", confidence=1.0))

            self.assertEqual(search_tool.calls, 1)
            self.assertEqual(search_tool.last_arguments["level"], "deep")


class _SuffixMissingLlm:
    model = "fake"

    def generate_reply(self, prompt: str) -> str:
        return "收到了，这次按你新发的要求来。"

    def classify_topic(self, recent_messages, topics, avoid_topics):
        raise AssertionError("not used")


class _PromptCaptureLlm:
    model = "fake"

    def __init__(self) -> None:
        self.last_prompt = ""

    def generate_reply(self, prompt: str) -> str:
        self.last_prompt = prompt
        return "收到，我们按新主题来。"

    def classify_topic(self, recent_messages, topics, avoid_topics):
        raise AssertionError("not used")


class _FakeWebSearchTool:
    manifest = ToolManifest(name="web.search", description="fake web search")

    def __init__(self) -> None:
        self.calls = 0
        self.last_arguments = {}

    def run(self, request):
        self.calls += 1
        self.last_arguments = dict(request.arguments)
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="completed",
            summary="web.search level=%s usable_results=1 fetched=1" % request.arguments.get("level"),
            output_refs=["tool_outputs/web_search/fake.md"],
            payload={
                "query": request.arguments.get("query"),
                "level": request.arguments.get("level"),
                "result_count": 1,
                "fetched_count": 1,
                "annotation_text": "Official docs say Python 3.14 is current.",
            },
        )


if __name__ == "__main__":
    unittest.main()

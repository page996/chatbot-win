from __future__ import annotations

import hashlib
import re

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.conversation.context_store import ConversationContextStore
from app.personal_wechat_bot.conversation.ledger_context import LedgerContextAssembler
from app.personal_wechat_bot.conversation.prompt_builder import PromptBuilder
from app.personal_wechat_bot.domain.models import (
    NormalizedMessage,
    ReplyCandidate,
    SpeakDecision,
    ToolCallRequest,
)
from app.personal_wechat_bot.llm.base import LLMClient
from app.personal_wechat_bot.tools.runtime import ToolRuntime
from app.personal_wechat_bot.agent.tool_orchestrator import ToolTaskOrchestrator


class ConversationEngine:
    def __init__(
        self,
        config: BotConfig,
        llm: LLMClient,
        tools: ToolRuntime,
        tool_orchestrator: ToolTaskOrchestrator | None = None,
        context_store: ConversationContextStore | None = None,
        ledger_context: LedgerContextAssembler | None = None,
    ):
        self.config = config
        self.llm = llm
        self.tools = tools
        self.tool_orchestrator = tool_orchestrator
        self.context_store = context_store
        self.ledger_context = ledger_context
        self.prompt_builder = PromptBuilder()

    def generate_reply(self, message: NormalizedMessage, speak_decision: SpeakDecision) -> ReplyCandidate | None:
        if speak_decision.decision != "speak":
            return None

        tool_request = self._tool_request(message)
        if tool_request:
            if self.tool_orchestrator:
                tool_result = self.tool_orchestrator.execute(tool_request)
            else:
                tool_result = self.tools.execute(tool_request)
            text = tool_result.summary
            return ReplyCandidate(
                message_id=message.message_id,
                conversation_id=message.conversation_id,
                text=text,
                send_mode=self.config.mode,
                model=self.llm.model,
                tool_result=tool_result,
                plan="调用工具完成请求",
                monitor="tool.completed" if tool_result.status == "completed" else "tool.failed",
                summary=tool_result.summary,
            )

        context_snapshot = (
            self.ledger_context.build_snapshot(message)
            if self.ledger_context is not None
            else self.context_store.build_snapshot(message)
            if self.context_store is not None
            else None
        )
        prompt = self.prompt_builder.build(message, speak_decision, context_snapshot=context_snapshot)
        raw_text = self.llm.generate_reply(prompt)
        text = _clean_visible_reply(raw_text)
        text = _apply_visible_reply_constraints(text, message.text)
        return ReplyCandidate(
            message_id=message.message_id,
            conversation_id=message.conversation_id,
            text=text,
            send_mode=self.config.mode,
            model=self.llm.model,
            plan="生成自然朋友聊天回复",
            monitor="llm.completed",
            summary=_extract_summary(text),
        )

    def _tool_request(self, message: NormalizedMessage) -> ToolCallRequest | None:
        text = message.text.strip()
        if text.startswith("#翻译"):
            arg = text.removeprefix("#翻译").strip()
            arguments = {"target_language": "zh-CN"}
            if arg.startswith("文本:"):
                arguments["input_text"] = arg.removeprefix("文本:").strip()
            else:
                arguments["input_path"] = arg
            return ToolCallRequest(
                tool_name="document.translate",
                call_id=_call_id(message.message_id, "document.translate"),
                conversation_id=message.conversation_id,
                requested_by="chatbot",
                arguments=arguments,
            )
        if text.startswith("#检索"):
            query = text.removeprefix("#检索").strip()
            return ToolCallRequest(
                tool_name="search.external_translate",
                call_id=_call_id(message.message_id, "search.external_translate"),
                conversation_id=message.conversation_id,
                requested_by="chatbot",
                arguments={"query": query, "target_language": "zh-CN"},
            )
        if text.startswith("#ocr ") or text.startswith("#OCR "):
            parts = text.split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            return ToolCallRequest(
                tool_name="vision.ocr",
                call_id=_call_id(message.message_id, "vision.ocr"),
                conversation_id=message.conversation_id,
                requested_by="chatbot",
                arguments={"input_path": arg},
            )
        if text.startswith("#web ") or text.startswith("#网页 "):
            parts = text.split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            return ToolCallRequest(
                tool_name="web.fetch",
                call_id=_call_id(message.message_id, "web.fetch"),
                conversation_id=message.conversation_id,
                requested_by="chatbot",
                arguments={"url": arg},
            )
        return None


def _call_id(message_id: str, tool_name: str) -> str:
    return hashlib.sha256(f"{message_id}:{tool_name}".encode("utf-8")).hexdigest()[:24]


def _extract_summary(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("SUMMARY:"):
            return line.removeprefix("SUMMARY:").strip()
    return text.strip()


def _clean_visible_reply(text: str) -> str:
    cleaned_lines: list[str] = []
    skip_prefixes = (
        "PLAN:",
        "MONITOR:",
        "SUMMARY:",
        "计划：",
        "监控：",
        "总结：",
        "**计划",
        "**执行",
        "**总结",
        "【计划】",
        "【监控】",
        "【总结】",
    )
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if cleaned_lines and cleaned_lines[-1]:
                cleaned_lines.append("")
            continue
        if stripped.startswith(skip_prefixes):
            continue
        if stripped in {"（发送消息）", "(发送消息)"}:
            continue
        cleaned_lines.append(stripped.strip("“”"))
    cleaned = "\n".join(cleaned_lines).strip()
    return cleaned or text.strip()


def _apply_visible_reply_constraints(reply_text: str, source_text: str) -> str:
    text = reply_text.strip()
    suffix = _explicit_reply_suffix_request(source_text)
    if suffix and not text.endswith(suffix):
        return f"{text}{suffix}" if text else suffix
    return text


def _explicit_reply_suffix_request(source_text: str) -> str:
    normalized = " ".join(source_text.split())
    match = _SUFFIX_REQUEST_RE.search(normalized)
    if not match:
        return ""
    suffix = match.group("suffix").strip()
    if len(suffix) > 60:
        return ""
    return suffix


_SUFFIX_REQUEST_RE = re.compile(
    r"(?:对话|回复|消息)?(?:的)?(?:末尾|结尾|最后)"
    r"(?:添加|加上|附上|补上)(?:一个|一段|：|:)?\s*"
    r"(?P<suffix>[\(\（\[\【][^\)\）\]\】]{1,50}[\)\）\]\】])"
)

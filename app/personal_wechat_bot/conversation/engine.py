from __future__ import annotations

import hashlib
import re

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.conversation.ledger_context import LedgerContextAssembler
from app.personal_wechat_bot.conversation.prompt_builder import PromptBuilder
from app.personal_wechat_bot.domain.models import (
    NormalizedMessage,
    ReplyCandidate,
    SpeakDecision,
    ToolCallResult,
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
        ledger_context: LedgerContextAssembler | None = None,
    ):
        self.config = config
        self.llm = llm
        self.tools = tools
        self.tool_orchestrator = tool_orchestrator
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

        context_snapshot = self.ledger_context.build_snapshot(message) if self.ledger_context is not None else None
        web_search_result = self._maybe_annotate_web_search(message, speak_decision)
        if web_search_result is not None and self.ledger_context is not None:
            context_snapshot = self.ledger_context.build_snapshot(message)
        prompt = self.prompt_builder.build(message, speak_decision, context_snapshot=context_snapshot)
        raw_text = self.llm.generate_reply(prompt)
        text = _clean_visible_reply(raw_text)
        text = _apply_visible_reply_constraints(text, message.text)
        web_search_completed = web_search_result is not None and web_search_result.status == "completed"
        return ReplyCandidate(
            message_id=message.message_id,
            conversation_id=message.conversation_id,
            text=text,
            send_mode=self.config.mode,
            model=self.llm.model,
            plan="生成自然朋友聊天回复" + ("；已补充联网检索依据" if web_search_completed else ""),
            monitor="llm.completed" + (";web_search.completed" if web_search_completed else ""),
            summary=_extract_summary(text),
            send_metadata=_web_search_send_metadata(web_search_result),
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
                tool_name="web.search",
                call_id=_call_id(message.message_id, "web.search"),
                conversation_id=message.conversation_id,
                requested_by="chatbot",
                arguments={"query": query, "level": "standard"},
            )
        if text.startswith("#搜索") or text.startswith("#search "):
            marker = "#搜索" if text.startswith("#搜索") else "#search"
            query = text.removeprefix(marker).strip()
            return ToolCallRequest(
                tool_name="web.search",
                call_id=_call_id(message.message_id, "web.search"),
                conversation_id=message.conversation_id,
                requested_by="chatbot",
                arguments={"query": query, "level": "standard"},
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
        if text.startswith("#file ") or text.startswith("#read-file "):
            parts = text.split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            return ToolCallRequest(
                tool_name="file.read",
                call_id=_call_id(message.message_id, "file.read"),
                conversation_id=message.conversation_id,
                requested_by="chatbot",
                arguments=_file_read_arguments(arg),
            )
        return None

    def _maybe_annotate_web_search(
        self,
        message: NormalizedMessage,
        speak_decision: SpeakDecision,
    ) -> ToolCallResult | None:
        if speak_decision.decision != "speak":
            return None
        plan = _web_search_plan(message)
        if plan is None:
            return None
        registry = getattr(self.tools, "registry", None)
        if registry is None or not registry.has("web.search"):
            return None
        ledger_store = getattr(self.ledger_context, "ledger_store", None) if self.ledger_context is not None else None
        entry = _ledger_entry_for_message(ledger_store, message) if ledger_store is not None else None
        if entry is not None and _entry_has_web_search_annotation(entry, plan["query"]):
            return None
        request = ToolCallRequest(
            tool_name="web.search",
            call_id=_call_id(message.message_id, f"web.search:{plan['level']}:{plan['query']}"),
            conversation_id=message.conversation_id,
            requested_by="chatbot",
            arguments={
                "query": plan["query"],
                "level": plan["level"],
                "reason": plan["reason"],
            },
        )
        result = self.tools.execute(request)
        if result.status == "completed" and entry is not None:
            annotation_text = str(result.payload.get("annotation_text") or result.summary)
            source_path = str(result.output_refs[0]) if result.output_refs else ""
            try:
                ledger_store.annotate_entry(
                    message.conversation_id,
                    entry.entry_id,
                    kind="annotation:websearch",
                    annotation_id=result.call_id,
                    summary=result.summary,
                    text=annotation_text,
                    source_path=source_path,
                    metadata={
                        "query": plan["query"],
                        "level": plan["level"],
                        "reason": plan["reason"],
                        "result_count": result.payload.get("result_count", 0),
                        "fetched_count": result.payload.get("fetched_count", 0),
                        "tool_name": result.tool_name,
                    },
                )
            except Exception:
                return result
        return result


def _call_id(message_id: str, tool_name: str) -> str:
    return hashlib.sha256(f"{message_id}:{tool_name}".encode("utf-8")).hexdigest()[:24]


def _file_read_arguments(text: str) -> dict[str, object]:
    args: dict[str, object] = {}
    parts = [part for part in text.split() if part.strip()]
    if parts:
        args["file_id"] = parts[0]
    for part in parts[1:]:
        if "=" not in part:
            if part.isdigit():
                args["chunk_index"] = int(part)
            else:
                args["artifact"] = part
            continue
        key, value = part.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key in {"chunk", "chunk_index"}:
            try:
                args["chunk_index"] = int(value)
            except ValueError:
                args["chunk_index"] = value
        elif key in {"artifact", "part"}:
            args["artifact"] = value
        elif key in {"file_id", "id"}:
            args["file_id"] = value
        elif key in {"pin_internal_urls", "include_internal_urls", "expose_internal_urls"}:
            args["pin_internal_urls"] = value.lower() in {"1", "true", "yes", "on", "pin", "include", "expose"}
    return args


def _web_search_plan(message: NormalizedMessage) -> dict[str, str] | None:
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    if metadata.get("disable_web_search") or metadata.get("context_only"):
        return None
    text = _primary_message_text(message).strip()
    if not text or len(text) < 4:
        return None
    explicit_level = _normalize_web_search_level(metadata.get("web_search_level"))
    explicit_query = str(metadata.get("web_search_query") or "").strip()
    if explicit_query:
        return {"query": _search_query(explicit_query), "level": explicit_level or "standard", "reason": "metadata_requested"}
    lowered = text.lower()
    if _WEB_SEARCH_DISSATISFACTION_RE.search(text):
        return {"query": _search_query(text), "level": explicit_level or "deep", "reason": "user_fact_challenge"}
    if _WEB_SEARCH_EXPLICIT_RE.search(text):
        return {"query": _search_query(text), "level": explicit_level or _explicit_search_level(text), "reason": "explicit_search_request"}
    if _WEB_SEARCH_FACT_RE.search(text) and _looks_like_fact_query(text):
        return {"query": _search_query(text), "level": explicit_level or "standard", "reason": "fresh_or_fact_sensitive"}
    if any(marker in lowered for marker in ("latest", "current", "today", "release", "version", "price", "news")) and _looks_like_fact_query(text):
        return {"query": _search_query(text), "level": explicit_level or "standard", "reason": "fresh_or_fact_sensitive"}
    return None


def _explicit_search_level(text: str) -> str:
    if re.search(r"(彻底|全面|深入|强力|多搜|多查|deep|aggressive|strong)", text, re.I):
        return "deep"
    if re.search(r"(简单|快速|随便|light|quick|fast)", text, re.I):
        return "light"
    return "standard"


def _normalize_web_search_level(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"light", "quick", "fast", "minimal", "1"}:
        return "light"
    if text in {"deep", "strong", "aggressive", "3"}:
        return "deep"
    if text in {"standard", "normal", "2"}:
        return "standard"
    return ""


def _search_query(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^#(?:搜索|检索|search)\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"(帮我|麻烦|请|可以|能不能)?(联网|上网)?(查一下|搜一下|搜索一下|检索一下|查证一下|核实一下)", "", cleaned)
    cleaned = re.sub(r"(你刚才|你上面|这个回答|这说法|感觉|好像|是不是|真的|吗|呢|吧|啊)", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ：:，,。?")
    return cleaned[:240] or str(text).strip()[:240]


def _looks_like_fact_query(text: str) -> bool:
    if len(text.strip()) >= 18:
        return True
    return bool(re.search(r"(多少|哪(个|些)|谁|何时|什么时候|版本|价格|政策|法规|新闻|发布|更新|API|模型|公司|CEO)", text, re.I))


def _ledger_entry_for_message(ledger_store: object, message: NormalizedMessage) -> object | None:
    read_entries = getattr(ledger_store, "read_entries", None)
    if read_entries is None:
        return None
    try:
        entries = read_entries(message.conversation_id, include_removed=True)
    except TypeError:
        entries = read_entries(message.conversation_id)
    except Exception:
        return None
    for entry in reversed(entries):
        ids = list(getattr(entry, "message_ids", []) or [])
        primary = str(getattr(entry, "message_id", "") or "")
        if message.message_id == primary or message.message_id in ids:
            return entry
    return None


def _entry_has_web_search_annotation(entry: object, query: str) -> bool:
    blocks = getattr(entry, "text_blocks", []) or []
    normalized_query = _normalize_for_match(query)
    for block in blocks:
        if not isinstance(block, dict) or str(block.get("kind") or "") != "annotation:websearch":
            continue
        metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
        if _normalize_for_match(str(metadata.get("query") or "")) == normalized_query:
            return True
    return False


def _web_search_send_metadata(result: ToolCallResult | None) -> dict[str, object]:
    if result is None:
        return {}
    return {
        "web_search": {
            "status": result.status,
            "call_id": result.call_id,
            "summary": result.summary,
            "query": result.payload.get("query", ""),
            "level": result.payload.get("level", ""),
            "result_count": result.payload.get("result_count", 0),
            "fetched_count": result.payload.get("fetched_count", 0),
        }
    }


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


def _primary_message_text(message: NormalizedMessage) -> str:
    original = message.metadata.get("original_text")
    if isinstance(original, str):
        return original
    return message.text


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
_WEB_SEARCH_EXPLICIT_RE = re.compile(
    r"(#搜索|#检索|#search\b|联网|上网|搜一下|搜索一下|检索一下|查一下|查证|核实|资料来源|来源|source|web\s*search)",
    re.I,
)
_WEB_SEARCH_FACT_RE = re.compile(
    r"(最新|当前|现在|今天|昨日|昨天|今年|最近|新闻|价格|汇率|政策|法规|法律|版本|发布|更新|官网|官方|API|模型|公司|CEO|总统|负责人|日程|赛程|release|version|current|latest|today|news|price|law|schedule|CEO)",
    re.I,
)
_WEB_SEARCH_DISSATISFACTION_RE = re.compile(
    r"(不对|错了|不是吧|你确定|真的假的|来源呢|证据呢|重新查|再查|再搜|查清楚|别乱说|胡说|不满意|wrong|incorrect|source\?|verify)",
    re.I,
)

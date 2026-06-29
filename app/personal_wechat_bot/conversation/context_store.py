from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.domain.models import NormalizedMessage, ReplyCandidate, utc_now_iso


DEFAULT_SESSION_ID = "session_default"
CLEAR_CONTEXT_PHRASES = ("清空当前对话上下文",)


@dataclass(frozen=True)
class ConversationContextSnapshot:
    conversation_id: str
    session_id: str
    recent_messages: list[dict[str, Any]]
    file_refs: list[dict[str, Any]]
    task_refs: list[dict[str, Any]]
    analysis: dict[str, Any]
    quote_refs: list[dict[str, Any]] = field(default_factory=list)

    def render_for_prompt(self, max_file_chars: int = 6000) -> str:
        lines = [
            f"当前独立上下文: conversation_id={self.conversation_id} session_id={self.session_id}",
            "上下文来源: 后端消息、后台附件解析产物、OCR 工具结果和本地任务记录；模型不能直接访问微信原始文件。",
        ]
        if self.recent_messages:
            lines.append("近期消息:")
            for item in self.recent_messages:
                sender = item.get("sender_name", "")
                text = _compact_text(str(item.get("text", "")), 800)
                lines.append(f"- {item.get('received_at', '')} {sender}: {text}")
        if self.quote_refs:
            lines.append("引用消息上下文:")
            for item in self.quote_refs:
                quote = item.get("quote") if isinstance(item.get("quote"), dict) else {}
                match = item.get("match") if isinstance(item.get("match"), dict) else {}
                lines.append(
                    f"- quote_text={_compact_text(str(quote.get('text', '')), 500)} "
                    f"match_status={match.get('status', '')} "
                    f"matched_message_id={match.get('message_id', '')}"
                )
                for message_item in item.get("messages", []):
                    if not isinstance(message_item, dict):
                        continue
                    lines.append(
                        f"  - {message_item.get('received_at', '')} "
                        f"{message_item.get('sender_name', '')}: "
                        f"{_compact_text(str(message_item.get('text', '')), 700)}"
                    )
                for file_item in item.get("file_refs", []):
                    if not isinstance(file_item, dict):
                        continue
                    parse = file_item.get("parse") if isinstance(file_item.get("parse"), dict) else {}
                    lines.append(
                        f"  - quoted_file {file_item.get('name', '')} "
                        f"file_id={file_item.get('file_id', '')} "
                        f"status={parse.get('status', file_item.get('status', ''))} "
                        f"summary={_compact_text(str(parse.get('summary', '')), 300)}"
                    )
        if self.analysis:
            lines.append("混合上下文分析:")
            for key in ["intent", "hashtags", "urls", "file_count", "needs_file_reasoning", "recommended_next_steps"]:
                value = self.analysis.get(key)
                if value in (None, "", [], {}):
                    continue
                lines.append(f"- {key}: {value}")
        if self.file_refs:
            lines.append("本 session 文件与解析产物:")
            remaining = max_file_chars
            for item in self.file_refs:
                parse = item.get("parse") if isinstance(item.get("parse"), dict) else {}
                workspace = item.get("workspace") if isinstance(item.get("workspace"), dict) else {}
                artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {}
                header = (
                    f"- {item.get('name', '')} file_id={item.get('file_id', '')} "
                    f"workspace_ref={workspace.get('workspace_dir', '')} "
                    f"content={artifacts.get('content_path', '')} "
                    f"table_index={artifacts.get('table_index_path', '')} "
                    f"media_index={artifacts.get('media_index_path', '')} "
                    f"status={parse.get('status', item.get('status', ''))} kind={parse.get('kind', item.get('kind', ''))}"
                )
                lines.append(header)
                summary = str(parse.get("summary", "")).strip()
                if summary:
                    lines.append(f"  摘要: {summary}")
                text = str(parse.get("text", "")).strip()
                if text and remaining > 0:
                    excerpt = _compact_text(text, min(remaining, 1600))
                    remaining -= len(excerpt)
                    lines.append(f"  可见内容: {excerpt}")
        if self.task_refs:
            lines.append("近期任务/工具结果:")
            for item in self.task_refs:
                lines.append(
                    f"- {item.get('created_at', '')} {item.get('tool_name', '')} "
                    f"status={item.get('status', '')}: {_compact_text(str(item.get('summary', '')), 500)}"
                )
        return "\n".join(lines)


class ConversationContextStore:
    def __init__(self, data_dir: str | Path, max_recent_messages: int = 20):
        self.root = Path(data_dir) / "conversation_context"
        self.max_recent_messages = max_recent_messages
        self.root.mkdir(parents=True, exist_ok=True)

    def current_session_id(self, conversation_id: str) -> str:
        state = self._read_state(conversation_id)
        session_id = str(state.get("current_session_id", "")).strip()
        if session_id:
            return session_id
        self._write_state(
            conversation_id,
            {
                "conversation_id": conversation_id,
                "current_session_id": DEFAULT_SESSION_ID,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            },
        )
        return DEFAULT_SESSION_ID

    def maybe_reset_for_message(self, message: NormalizedMessage) -> str | None:
        if not any(phrase in message.text for phrase in CLEAR_CONTEXT_PHRASES):
            return None
        return self.reset_session(
            message.conversation_id,
            reason="clear_current_context_command",
            message_id=message.message_id,
        )

    def reset_session(self, conversation_id: str, *, reason: str, message_id: str = "") -> str:
        session_id = _new_session_id()
        self._write_state(
            conversation_id,
            {
                "conversation_id": conversation_id,
                "current_session_id": session_id,
                "previous_reset_reason": reason,
                "previous_reset_message_id": message_id,
                "updated_at": utc_now_iso(),
            },
        )
        self._append_event(
            conversation_id,
            session_id,
            {
                "type": "session.reset",
                "conversation_id": conversation_id,
                "session_id": session_id,
                "reason": reason,
                "message_id": message_id,
                "created_at": utc_now_iso(),
            },
        )
        return session_id

    def record_message(self, message: NormalizedMessage) -> None:
        session_id = self.current_session_id(message.conversation_id)
        payload = {
            "type": "message",
            "message_id": message.message_id,
            "conversation_id": message.conversation_id,
            "session_id": session_id,
            "conversation_type": message.conversation_type,
            "chat_title": message.chat_title,
            "sender_name": message.sender_name,
            "sender_wechat_id": message.sender_wechat_id,
            "received_at": message.received_at,
            "text": _message_text_for_context(message.text),
            "raw_text": message.text,
            "source": message.metadata.get("source", ""),
            "attachments": _attachment_refs_from_metadata(message.metadata),
            "quote": _quote_ref_from_metadata(message.metadata),
            "created_at": utc_now_iso(),
        }
        self._append_event(message.conversation_id, session_id, payload)

    def record_reply(self, reply: ReplyCandidate) -> None:
        session_id = self.current_session_id(reply.conversation_id)
        payload = {
            "type": "reply",
            "message_id": reply.message_id,
            "conversation_id": reply.conversation_id,
            "session_id": session_id,
            "text": reply.text,
            "send_mode": reply.send_mode,
            "model": reply.model,
            "plan": reply.plan,
            "monitor": reply.monitor,
            "summary": reply.summary,
            "tool_result": asdict(reply.tool_result) if reply.tool_result else None,
            "created_at": reply.created_at,
        }
        self._append_event(reply.conversation_id, session_id, payload)

    def build_snapshot(self, message: NormalizedMessage) -> ConversationContextSnapshot:
        session_id = self.current_session_id(message.conversation_id)
        events = self._read_events(message.conversation_id, session_id)
        messages = [item for item in events if item.get("type") == "message"]
        files: list[dict[str, Any]] = []
        seen_files: set[str] = set()
        tasks: list[dict[str, Any]] = []
        for item in events:
            if item.get("type") == "message":
                for attachment in item.get("attachments", []):
                    if not isinstance(attachment, dict):
                        continue
                    key = str(attachment.get("file_id") or attachment.get("name") or "")
                    if key and key in seen_files:
                        continue
                    if key:
                        seen_files.add(key)
                    files.append(attachment)
            if item.get("type") == "reply":
                tool = item.get("tool_result")
                if isinstance(tool, dict):
                    tasks.append(
                        {
                            "tool_name": tool.get("tool_name", ""),
                            "status": tool.get("status", ""),
                            "summary": tool.get("summary", ""),
                            "output_refs": tool.get("output_refs", []),
                            "created_at": item.get("created_at", ""),
                        }
                    )
        return ConversationContextSnapshot(
            conversation_id=message.conversation_id,
            session_id=session_id,
            recent_messages=messages[-self.max_recent_messages :],
            file_refs=files[-20:],
            task_refs=tasks[-10:],
            analysis=_analyze_mixed_context(messages[-self.max_recent_messages :], files[-20:], tasks[-10:]),
            quote_refs=_build_quote_contexts(messages, message),
        )

    def _conversation_dir(self, conversation_id: str) -> Path:
        return self.root / conversation_id

    def _session_dir(self, conversation_id: str, session_id: str) -> Path:
        return self._conversation_dir(conversation_id) / "sessions" / session_id

    def _state_path(self, conversation_id: str) -> Path:
        return self._conversation_dir(conversation_id) / "state.json"

    def _read_state(self, conversation_id: str) -> dict[str, Any]:
        path = self._state_path(conversation_id)
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}

    def _write_state(self, conversation_id: str, payload: dict[str, Any]) -> None:
        path = self._state_path(conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = self._read_state(conversation_id)
        merged = {**previous, **payload, "updated_at": utc_now_iso()}
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        tmp.replace(path)

    def _append_event(self, conversation_id: str, session_id: str, payload: dict[str, Any]) -> None:
        path = self._session_dir(conversation_id, session_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _read_events(self, conversation_id: str, session_id: str) -> list[dict[str, Any]]:
        path = self._session_dir(conversation_id, session_id) / "events.jsonl"
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    events.append(payload)
        return events


def _attachment_refs_from_metadata(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    attachments = metadata.get("attachments", [])
    if not isinstance(attachments, list):
        return []
    return [dict(item) for item in attachments if isinstance(item, dict)]


def _quote_ref_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    raw = metadata.get("quote")
    if raw is None:
        raw = metadata.get("quoted_message")
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict):
        return {}
    quote = {
        "message_id": str(raw.get("message_id") or raw.get("quoted_message_id") or "").strip(),
        "sender_name": str(raw.get("sender_name") or raw.get("quoted_sender_name") or "").strip(),
        "text": str(raw.get("text") or raw.get("quoted_text") or raw.get("content") or "").strip(),
        "received_at": str(raw.get("received_at") or raw.get("quoted_received_at") or "").strip(),
        "source": str(raw.get("source") or "backend_quote").strip(),
    }
    cleaned = {key: value for key, value in quote.items() if value}
    if not (cleaned.get("message_id") or cleaned.get("text")):
        return {}
    return cleaned


def _build_quote_contexts(
    messages: list[dict[str, Any]],
    current_message: NormalizedMessage,
    neighbor_radius: int = 2,
) -> list[dict[str, Any]]:
    quote = _quote_ref_from_metadata(current_message.metadata)
    if not quote or not (quote.get("message_id") or quote.get("text")):
        return []
    history = [item for item in messages if item.get("message_id") != current_message.message_id]
    if not history:
        return [{"quote": quote, "match": {"status": "missing_history"}, "messages": [], "file_refs": []}]

    match_index = _find_quoted_message_index(history, quote)
    if match_index is None:
        return [{"quote": quote, "match": {"status": "not_found"}, "messages": [], "file_refs": []}]

    start = max(0, match_index - neighbor_radius)
    end = min(len(history), match_index + neighbor_radius + 1)
    window = history[start:end]
    matched = history[match_index]
    file_refs: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    for item in window:
        for attachment in item.get("attachments", []):
            if not isinstance(attachment, dict):
                continue
            key = str(attachment.get("file_id") or attachment.get("name") or "")
            if key and key in seen_files:
                continue
            if key:
                seen_files.add(key)
            file_refs.append(dict(attachment))
    return [
        {
            "quote": quote,
            "match": {
                "status": "found",
                "message_id": str(matched.get("message_id", "")),
                "received_at": str(matched.get("received_at", "")),
                "sender_name": str(matched.get("sender_name", "")),
                "window_start": start,
                "window_end": end - 1,
            },
            "messages": [_quote_window_message(item) for item in window],
            "file_refs": file_refs,
        }
    ]


def _find_quoted_message_index(messages: list[dict[str, Any]], quote: dict[str, Any]) -> int | None:
    message_id = str(quote.get("message_id", "")).strip()
    if message_id:
        for index, item in enumerate(messages):
            if str(item.get("message_id", "")) == message_id:
                return index
    quote_text = str(quote.get("text", "")).strip()
    if not quote_text:
        return None
    sender_name = str(quote.get("sender_name", "")).strip()
    matches: list[int] = []
    for index, item in enumerate(messages):
        if sender_name and str(item.get("sender_name", "")) != sender_name:
            continue
        candidate = "\n".join([str(item.get("raw_text", "")), str(item.get("text", ""))])
        if _text_matches_quote(candidate, quote_text):
            matches.append(index)
    return matches[-1] if matches else None


def _text_matches_quote(candidate: str, quote_text: str) -> bool:
    candidate_norm = _normalize_for_match(candidate)
    quote_norm = _normalize_for_match(quote_text)
    if not candidate_norm or not quote_norm:
        return False
    if quote_norm in candidate_norm or candidate_norm in quote_norm:
        return True
    quote_tokens = [item for item in quote_norm.split(" ") if item]
    if len(quote_tokens) < 3:
        return False
    matched = sum(1 for token in quote_tokens if token in candidate_norm)
    return matched / len(quote_tokens) >= 0.75


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _quote_window_message(item: dict[str, Any]) -> dict[str, Any]:
    attachments = item.get("attachments", [])
    return {
        "message_id": item.get("message_id", ""),
        "received_at": item.get("received_at", ""),
        "sender_name": item.get("sender_name", ""),
        "text": item.get("text", ""),
        "attachment_count": len(attachments) if isinstance(attachments, list) else 0,
    }


def _message_text_for_context(text: str) -> str:
    lines: list[str] = []
    skipping_content = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[后台附件内容]"):
            skipping_content = True
            continue
        if (
            stripped.startswith("[后台附件]")
            or stripped.startswith("[后台附件解析]")
            or stripped.startswith("[后台附件已阻止]")
        ):
            skipping_content = False
            lines.append(stripped)
            continue
        if skipping_content:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _analyze_mixed_context(
    messages: list[dict[str, Any]],
    files: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    latest = str(messages[-1].get("raw_text") or messages[-1].get("text") or "") if messages else ""
    visible_latest = _message_text_for_context(latest)
    urls = _URL_RE.findall(latest)
    hashtags = _HASHTAG_RE.findall(latest)
    needs_file_reasoning = bool(files) and _looks_like_file_task(latest)
    steps: list[str] = []
    if needs_file_reasoning:
        steps.append("优先使用本 session 的后台附件解析内容进行分析、提取、总结或任务拆解")
        steps.append("如果解析内容不足，再在 file_workspace 的 staged copy 上安排 LibreOffice/OCR/CLI 处理")
    if urls:
        steps.append("对网址先做 web.fetch，再结合网页文字分析")
    if tasks:
        steps.append("结合近期工具结果，避免重复执行已经完成的任务")
    if not steps:
        steps.append("按私聊/群聊策略自然接话，必要时提出一个澄清问题")
    return {
        "intent": _guess_intent(visible_latest, has_files=bool(files), has_urls=bool(urls)),
        "hashtags": hashtags,
        "urls": urls,
        "file_count": len(files),
        "task_count": len(tasks),
        "needs_file_reasoning": needs_file_reasoning,
        "recommended_next_steps": steps,
    }


def _guess_intent(text: str, *, has_files: bool, has_urls: bool) -> str:
    if any(word in text for word in ["分析", "总结", "提取", "读取", "发送出来", "处理", "规划", "执行"]):
        if has_files:
            return "file_analysis_or_processing_task"
        if has_urls:
            return "web_reading_or_analysis_task"
        return "analysis_or_task_request"
    if any(word in text for word in CLEAR_CONTEXT_PHRASES):
        return "reset_context"
    if "#" in text:
        return "topic_or_command"
    return "conversation"


def _looks_like_file_task(text: str) -> bool:
    return any(
        word in text
        for word in ["附件", "文件", "pdf", "PDF", "图片", "表格", "读取", "读一下", "分析", "总结", "提取", "发送出来", "OCR", "ocr"]
    )


def _new_session_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"session_{stamp}_{uuid.uuid4().hex[:8]}"


def _compact_text(text: str, max_chars: int) -> str:
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "..."


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"#([^\s#]+)")

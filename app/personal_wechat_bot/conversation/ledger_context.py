from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.personal_wechat_bot.conversation.session_store import DEFAULT_SESSION_ID
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.domain.models import NormalizedMessage
from app.personal_wechat_bot.workspace.file_visibility import redact_file_internal_urls


SectionName = Literal["runtime_cards", "memory", "analysis", "quote", "recent", "files", "links"]


@dataclass(frozen=True)
class ContextSection:
    name: SectionName
    title: str
    lines: list[str]
    token_estimate: int
    forced: bool = False


@dataclass(frozen=True)
class LedgerContextSnapshot:
    conversation_id: str
    session_id: str
    ledger_path: str
    recent_entries: list[dict[str, Any]]
    quote_context: dict[str, Any] = field(default_factory=dict)
    file_refs: list[dict[str, Any]] = field(default_factory=list)
    link_refs: list[dict[str, Any]] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=dict)
    analysis: dict[str, Any] = field(default_factory=dict)
    runtime_card_lines: list[str] = field(default_factory=list)
    sections: list[ContextSection] = field(default_factory=list)
    token_budget: int = 3000
    estimated_tokens: int = 0

    def render_for_prompt(self, max_chars: int | None = None) -> str:
        lines = [
            f"Conversation ledger context: conversation_id={self.conversation_id} session_id={self.session_id}",
            f"Ledger markdown: {self.ledger_path}",
            f"Context budget: estimated_tokens={self.estimated_tokens} budget={self.token_budget}",
            "Only active ledger entries are visible. Recalled or removed entries are excluded from reasoning.",
            "Recent context is scoped to the current session. Explicitly quoted messages may restore a narrow cross-session window.",
        ]
        for section in self.sections:
            if not section.lines:
                continue
            lines.append(section.title)
            lines.extend(section.lines)
        rendered = "\n".join(lines)
        if max_chars is None:
            return rendered
        return _compact(rendered, max_chars)


class LedgerContextAssembler:
    def __init__(
        self,
        ledger_store: ConversationLedgerStore,
        max_recent_entries: int = 30,
        token_budget: int = 3000,
        runtime_cards: Any | None = None,
    ):
        self.ledger_store = ledger_store
        self.max_recent_entries = max_recent_entries
        self.token_budget = token_budget
        self.runtime_cards = runtime_cards

    def build_snapshot(self, message: NormalizedMessage) -> LedgerContextSnapshot:
        session_id = _session_id_from_message(message)
        self.ledger_store.refresh_file_refs(message.conversation_id)
        entries = [as_payload(item) for item in self.ledger_store.read_entries(message.conversation_id)]
        session_entries = _filter_entries_for_session(entries, session_id)
        quote = _quote_from_message(message)
        quote_context = (
            self.ledger_store.lookup_quote_context(message.conversation_id, quote)
            if quote
            else {}
        )
        visible_entries = session_entries[-self.max_recent_entries :]
        quote_entries = quote_context.get("entries", []) if isinstance(quote_context, dict) else []
        matched_quote_files = quote_context.get("matched_attachments", []) if isinstance(quote_context, dict) else []
        file_refs = _dedupe_file_refs(
            [
                *(dict(item) for item in matched_quote_files if isinstance(item, dict)),
                *_collect_file_refs([*visible_entries, *quote_entries]),
            ]
        )
        link_refs = _collect_link_refs([*visible_entries, *quote_entries])
        conversation_dir = self.ledger_store.conversation_markdown_path(message.conversation_id).parent
        memory = _read_memory(memory_dir_for_conversation(conversation_dir, session_id))
        analysis = _analyze(session_entries, message)
        runtime_card_lines = self.runtime_cards.prompt_lines() if self.runtime_cards is not None else []
        sections = _budget_sections(
            _build_sections(
                runtime_card_lines=runtime_card_lines,
                memory=memory,
                analysis=analysis,
                quote_entries=quote_entries,
                recent_entries=visible_entries,
                file_refs=file_refs,
                link_refs=link_refs,
            ),
            token_budget=self.token_budget,
        )
        return LedgerContextSnapshot(
            conversation_id=message.conversation_id,
            session_id=session_id,
            ledger_path=str(self.ledger_store.conversation_markdown_path(message.conversation_id)),
            recent_entries=visible_entries,
            quote_context=quote_context,
            file_refs=file_refs,
            link_refs=link_refs,
            memory=memory,
            analysis=analysis,
            runtime_card_lines=runtime_card_lines,
            sections=sections,
            token_budget=self.token_budget,
            estimated_tokens=sum(section.token_estimate for section in sections),
        )


def as_payload(entry: Any) -> dict[str, Any]:
    if isinstance(entry, dict):
        return dict(entry)
    return {
        "entry_id": entry.entry_id,
        "message_id": entry.message_id,
        "conversation_id": entry.conversation_id,
        "session_id": getattr(entry, "session_id", DEFAULT_SESSION_ID) or DEFAULT_SESSION_ID,
        "conversation_type": entry.conversation_type,
        "chat_title": entry.chat_title,
        "sender_name": entry.sender_name,
        "sender_wechat_id": entry.sender_wechat_id,
        "is_self": entry.is_self,
        "received_at": entry.received_at,
        "sequence": entry.sequence,
        "status": entry.status,
        "text_blocks": [dict(item) for item in entry.text_blocks],
        "quote": dict(entry.quote),
        "attachments": [dict(item) for item in entry.attachments],
        "links": [dict(item) for item in entry.links],
        "source": entry.source,
        "role": entry.role,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
    }


def _build_sections(
    *,
    runtime_card_lines: list[str],
    memory: dict[str, Any],
    analysis: dict[str, Any],
    quote_entries: list[dict[str, Any]],
    recent_entries: list[dict[str, Any]],
    file_refs: list[dict[str, Any]],
    link_refs: list[dict[str, Any]],
) -> list[ContextSection]:
    sections: list[ContextSection] = []
    if runtime_card_lines:
        sections.append(_section("runtime_cards", "Persistent runtime cards:", runtime_card_lines, forced=True))
    memory_lines = _memory_lines(memory)
    if memory_lines:
        sections.append(_section("memory", "Long-term memory:", memory_lines, forced=True))
    analysis_lines = _analysis_lines(analysis)
    if analysis_lines:
        sections.append(_section("analysis", "Context analysis:", analysis_lines, forced=True))
    if quote_entries:
        sections.append(
            _section(
                "quote",
                "Quoted-message window:",
                [_render_entry_line(item, max_text_chars=900) for item in quote_entries],
                forced=True,
            )
        )
    if recent_entries:
        sections.append(
            _section(
                "recent",
                "Recent ordered ledger entries:",
                [_render_entry_line(item, max_text_chars=700) for item in recent_entries],
            )
        )
    file_lines = _file_lines(file_refs)
    if file_lines:
        sections.append(_section("files", "Available file refs:", file_lines, forced=True))
    link_lines = _link_lines(link_refs)
    if link_lines:
        sections.append(_section("links", "Detected links:", link_lines))
    return sections


def _budget_sections(sections: list[ContextSection], *, token_budget: int) -> list[ContextSection]:
    if token_budget <= 0:
        return sections
    forced = [section for section in sections if section.forced]
    optional = [section for section in sections if not section.forced]
    used = sum(section.token_estimate for section in forced)
    kept: list[ContextSection] = list(forced)
    for section in optional:
        remaining = token_budget - used
        if remaining <= 0:
            kept.append(_omitted_section(section))
            continue
        if section.token_estimate <= remaining:
            kept.append(section)
            used += section.token_estimate
            continue
        trimmed = _trim_section(section, remaining)
        if trimmed.lines:
            kept.append(trimmed)
            used += trimmed.token_estimate
    order = {section.name: index for index, section in enumerate(sections)}
    return sorted(kept, key=lambda section: order.get(section.name, 999))


def _omitted_section(section: ContextSection) -> ContextSection:
    return _section(
        section.name,
        section.title,
        [f"- {section.name} context omitted because forced context used the token budget"],
        forced=False,
    )


def _trim_section(section: ContextSection, token_budget: int) -> ContextSection:
    if token_budget <= 0:
        return ContextSection(section.name, section.title, [], 0, section.forced)
    kept: list[str] = []
    used = 0
    for line in reversed(section.lines):
        cost = _estimate_tokens(line)
        if kept and used + cost > token_budget:
            break
        if not kept and cost > token_budget:
            kept.append(_compact(line, max(80, token_budget * 4)))
            used = _estimate_tokens(kept[-1])
            break
        kept.append(line)
        used += cost
    kept.reverse()
    if len(kept) < len(section.lines):
        kept.insert(0, f"- earlier {section.name} context omitted by token budget")
    return _section(section.name, section.title, kept, forced=section.forced)


def _section(name: SectionName, title: str, lines: list[str], *, forced: bool = False) -> ContextSection:
    return ContextSection(
        name=name,
        title=title,
        lines=lines,
        token_estimate=sum(_estimate_tokens(line) for line in lines) + _estimate_tokens(title),
        forced=forced,
    )


def _memory_lines(memory: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in ["summary", "preferences", "entities"]:
        value = memory.get(key)
        if value in (None, "", [], {}):
            continue
        rendered = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        lines.append(f"- {key}: {_compact(rendered, 1200)}")
    return lines


def _analysis_lines(analysis: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, value in analysis.items():
        if value in (None, "", [], {}):
            continue
        lines.append(f"- {key}: {value}")
    return lines


def _filter_entries_for_session(entries: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
    return [item for item in entries if _entry_session_id(item) == session_id]


def _entry_session_id(entry: dict[str, Any]) -> str:
    return str(entry.get("session_id") or DEFAULT_SESSION_ID)


def _session_id_from_message(message: NormalizedMessage) -> str:
    session_id = str(message.metadata.get("session_id") or "").strip()
    return session_id or DEFAULT_SESSION_ID


def memory_dir_for_conversation(conversation_dir: Path, session_id: str) -> Path:
    if session_id == DEFAULT_SESSION_ID:
        return conversation_dir / "memory"
    return conversation_dir / "sessions" / session_id / "memory"


def _file_lines(file_refs: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in file_refs[-30:]:
        parse = item.get("parse") if isinstance(item.get("parse"), dict) else {}
        artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {}
        parts = [
            f"name={item.get('name', '')}",
            f"file_id={item.get('file_id', '')}",
            f"kind={item.get('kind', '')}",
            f"status={item.get('status', '')}",
            f"parse_status={parse.get('status', '')}",
        ]
        if item.get("file_id"):
            parts.append("read_tool=file.read")
            parts.append(f"read_args=file_id={item.get('file_id', '')}")
        for key, label in (
            ("char_count", "chars"),
            ("chunk_count", "chunks"),
            ("table_chunk_count", "table_chunks"),
            ("ocr_table_chunk_count", "ocr_tables"),
            ("media_extract_count", "media"),
            ("media_ocr_count", "ocr"),
            ("media_asr_count", "asr"),
        ):
            value = artifacts.get(key, "")
            if value not in ("", None, 0):
                parts.append(f"{label}={value}")
        ai_summary = _usable_ai_summary(artifacts)
        summary = "" if ai_summary else str(parse.get("summary", "")).strip()
        if summary:
            parts.append(f"summary={_compact(summary, 300)}")
        key_points = _file_key_points(artifacts, parse)
        if key_points and not ai_summary:
            parts.append("key_points=" + " / ".join(key_points[:5]))
        lines.append("- " + " ".join(part for part in parts if not part.endswith("=")))
        if ai_summary:
            lines.append(
                "- [file_analysis "
                f"file_id={item.get('file_id', '')} "
                "source=content.md::AI Analysis+Key Points]"
            )
            lines.append(f"  AI Analysis: {_compact(ai_summary, 1200)}")
            if key_points:
                lines.append("  Key Points:")
                lines.extend(f"  - {point}" for point in key_points[:12])
    return lines


def _usable_ai_summary(artifacts: dict[str, Any]) -> str:
    if str(artifacts.get("ai_analysis_status", "")).strip() != "analyzed":
        return ""
    summary = str(artifacts.get("ai_summary", "")).strip()
    if "fake_llm.completed" in summary or "PLAN:" in summary or "MONITOR:" in summary:
        return ""
    return redact_file_internal_urls(summary)


def _file_key_points(artifacts: dict[str, Any], parse: dict[str, Any]) -> list[str]:
    raw = artifacts.get("ai_key_points") or parse.get("ai_key_points") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [_compact(redact_file_internal_urls(str(item).strip()), 180) for item in raw if str(item).strip()]


def _link_lines(link_refs: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in link_refs[-20:]:
        lines.append(
            "- "
            f"url_id={item.get('url_id', '')} status={item.get('status', '')} "
            f"summary={_compact(str(item.get('summary', '')), 220)} "
            f"url={item.get('url', '')}"
        )
    return lines


def _render_entry_line(item: dict[str, Any], max_text_chars: int) -> str:
    role = "self" if item.get("is_self") else item.get("role", "user")
    text = "\n".join(
        str(block.get("text", ""))
        for block in item.get("text_blocks", [])
        if _visible_text_block(block)
    )
    quote = item.get("quote") if isinstance(item.get("quote"), dict) else {}
    quote_note = f" quote={_compact(str(quote.get('text', '')), 160)}" if quote else ""
    return (
        f"- #{int(item.get('sequence', 0) or 0):06d} {item.get('received_at', '')} "
        f"{item.get('sender_name', '')} role={role}{quote_note}: {_compact(text, max_text_chars)}"
    )


def _visible_text_block(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    if not block.get("text"):
        return False
    kind = str(block.get("kind", ""))
    metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
    if kind.startswith("attachment:"):
        return False
    return metadata.get("visible_in_context") is not False


def _quote_from_message(message: NormalizedMessage) -> dict[str, Any]:
    raw = message.metadata.get("quote") or message.metadata.get("quoted_message") or {}
    if isinstance(raw, str):
        raw = {"text": raw}
    return dict(raw) if isinstance(raw, dict) else {}


def _collect_file_refs(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in entries:
        for attachment in item.get("attachments", []):
            if not isinstance(attachment, dict):
                continue
            key = str(attachment.get("file_id") or attachment.get("name") or attachment.get("path") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            refs.append(dict(attachment))
    return refs


def _dedupe_file_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    for item in refs:
        key = str(item.get("file_id") or item.get("name") or item.get("path") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        kept.append(dict(item))
    return kept


def _collect_link_refs(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in entries:
        for link in item.get("links", []):
            if not isinstance(link, dict):
                continue
            url = str(link.get("url", ""))
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            refs.append(dict(link))
    return refs


def _read_memory(memory_dir: Path) -> dict[str, Any]:
    memory: dict[str, Any] = {}
    summary_path = memory_dir / "summary.md"
    if summary_path.exists():
        memory["summary"] = summary_path.read_text(encoding="utf-8", errors="replace")
    for name in ["preferences", "entities"]:
        path = memory_dir / f"{name}.json"
        if not path.exists():
            continue
        try:
            memory[name] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            memory[name] = path.read_text(encoding="utf-8", errors="replace")
    return memory


def _analyze(entries: list[dict[str, Any]], message: NormalizedMessage) -> dict[str, Any]:
    text = _primary_message_text(message)
    active_count = len(entries)
    files = _collect_file_refs(entries[-20:])
    links = _URL_RE.findall(text)
    return {
        "active_entry_count": active_count,
        "recent_file_count": len(files),
        "current_message_has_quote": bool(_quote_from_message(message)),
        "current_message_links": links,
        "current_message_has_files": bool(message.metadata.get("attachments")),
    }


def _primary_message_text(message: NormalizedMessage) -> str:
    original = message.metadata.get("original_text")
    if isinstance(original, str):
        return original
    return message.text


def _compact(text: str, max_chars: int) -> str:
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "..."


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    non_cjk = max(0, len(text) - cjk)
    return max(1, cjk + non_cjk // 4)


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

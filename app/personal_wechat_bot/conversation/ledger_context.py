from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.personal_wechat_bot.conversation.context_store import DEFAULT_SESSION_ID
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.domain.models import NormalizedMessage


SectionName = Literal["memory", "analysis", "quote", "recent", "files", "links"]


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
    sections: list[ContextSection] = field(default_factory=list)
    token_budget: int = 3000
    estimated_tokens: int = 0

    def render_for_prompt(self, max_chars: int | None = None) -> str:
        lines = [
            f"Conversation ledger context: conversation_id={self.conversation_id} session_id={self.session_id}",
            f"Ledger markdown: {self.ledger_path}",
            f"Context budget: estimated_tokens={self.estimated_tokens} budget={self.token_budget}",
            "Only active ledger entries are visible. Recalled or removed entries are excluded from reasoning.",
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
    ):
        self.ledger_store = ledger_store
        self.max_recent_entries = max_recent_entries
        self.token_budget = token_budget

    def build_snapshot(self, message: NormalizedMessage) -> LedgerContextSnapshot:
        entries = [as_payload(item) for item in self.ledger_store.read_entries(message.conversation_id)]
        quote = _quote_from_message(message)
        quote_context = (
            self.ledger_store.lookup_quote_context(message.conversation_id, quote)
            if quote
            else {}
        )
        visible_entries = entries[-self.max_recent_entries :]
        quote_entries = quote_context.get("entries", []) if isinstance(quote_context, dict) else []
        file_refs = _collect_file_refs([*visible_entries, *quote_entries])
        link_refs = _collect_link_refs(visible_entries)
        memory = _read_memory(self.ledger_store.conversation_markdown_path(message.conversation_id).parent / "memory")
        analysis = _analyze(entries, message)
        sections = _budget_sections(
            _build_sections(
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
            session_id=str(message.metadata.get("session_id") or DEFAULT_SESSION_ID),
            ledger_path=str(self.ledger_store.conversation_markdown_path(message.conversation_id)),
            recent_entries=visible_entries,
            quote_context=quote_context,
            file_refs=file_refs,
            link_refs=link_refs,
            memory=memory,
            analysis=analysis,
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
    memory: dict[str, Any],
    analysis: dict[str, Any],
    quote_entries: list[dict[str, Any]],
    recent_entries: list[dict[str, Any]],
    file_refs: list[dict[str, Any]],
    link_refs: list[dict[str, Any]],
) -> list[ContextSection]:
    sections: list[ContextSection] = []
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


def _file_lines(file_refs: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in file_refs[-30:]:
        workspace = item.get("workspace") if isinstance(item.get("workspace"), dict) else {}
        parse = item.get("parse") if isinstance(item.get("parse"), dict) else {}
        artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {}
        lines.append(
            "- "
            f"name={item.get('name', '')} file_id={item.get('file_id', '')} "
            f"kind={item.get('kind', '')} status={item.get('status', '')} "
            f"manifest={workspace.get('manifest_path', '')} "
            f"content={artifacts.get('content_path', '')} "
            f"chunk_count={artifacts.get('chunk_count', '')} "
            f"chunks_dir={artifacts.get('chunks_dir', '')} "
            f"table_index={artifacts.get('table_index_path', '')} "
            f"table_chunk_count={artifacts.get('table_chunk_count', '')} "
            f"parse_status={parse.get('status', '')} summary={_compact(str(parse.get('summary', '')), 300)}"
        )
    return lines


def _link_lines(link_refs: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in link_refs[-20:]:
        lines.append(
            "- "
            f"url_id={item.get('url_id', '')} status={item.get('status', '')} "
            f"annotation={item.get('annotation_path', '')} "
            f"summary={_compact(str(item.get('summary', '')), 220)} "
            f"url={item.get('url', '')}"
        )
    return lines


def _render_entry_line(item: dict[str, Any], max_text_chars: int) -> str:
    role = "self" if item.get("is_self") else item.get("role", "user")
    text = "\n".join(
        str(block.get("text", ""))
        for block in item.get("text_blocks", [])
        if isinstance(block, dict) and block.get("text")
    )
    quote = item.get("quote") if isinstance(item.get("quote"), dict) else {}
    quote_note = f" quote={_compact(str(quote.get('text', '')), 160)}" if quote else ""
    return (
        f"- #{int(item.get('sequence', 0) or 0):06d} {item.get('received_at', '')} "
        f"{item.get('sender_name', '')} role={role}{quote_note}: {_compact(text, max_text_chars)}"
    )


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
    text = message.text
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


def _compact(text: str, max_chars: int) -> str:
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "..."


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

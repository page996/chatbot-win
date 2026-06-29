from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.conversation.context_store import DEFAULT_SESSION_ID
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.domain.models import NormalizedMessage


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

    def render_for_prompt(self, max_chars: int = 12000) -> str:
        lines = [
            f"Conversation ledger context: conversation_id={self.conversation_id} session_id={self.session_id}",
            f"Ledger markdown: {self.ledger_path}",
            "Only active ledger entries are visible. Recalled or removed entries are excluded from reasoning.",
        ]
        if self.memory:
            lines.append("Long-term memory:")
            for key in ["summary", "preferences", "entities"]:
                value = self.memory.get(key)
                if value in (None, "", [], {}):
                    continue
                lines.append(f"- {key}: {_compact(json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value, 1200)}")
        if self.analysis:
            lines.append("Context analysis:")
            for key, value in self.analysis.items():
                if value in (None, "", [], {}):
                    continue
                lines.append(f"- {key}: {value}")
        quote_entries = self.quote_context.get("entries", []) if isinstance(self.quote_context, dict) else []
        if quote_entries:
            lines.append("Quoted-message window:")
            for item in quote_entries:
                lines.append(_render_entry_line(item, max_text_chars=900))
        if self.recent_entries:
            lines.append("Recent ordered ledger entries:")
            for item in self.recent_entries:
                lines.append(_render_entry_line(item, max_text_chars=900))
        if self.file_refs:
            lines.append("Available file refs:")
            for item in self.file_refs[-30:]:
                workspace = item.get("workspace") if isinstance(item.get("workspace"), dict) else {}
                parse = item.get("parse") if isinstance(item.get("parse"), dict) else {}
                lines.append(
                    "- "
                    f"name={item.get('name', '')} file_id={item.get('file_id', '')} "
                    f"kind={item.get('kind', '')} status={item.get('status', '')} "
                    f"manifest={workspace.get('manifest_path', '')} "
                    f"parse_status={parse.get('status', '')} summary={_compact(str(parse.get('summary', '')), 300)}"
                )
        if self.link_refs:
            lines.append("Detected links:")
            for item in self.link_refs[-20:]:
                lines.append(f"- url_id={item.get('url_id', '')} status={item.get('status', '')} url={item.get('url', '')}")
        rendered = "\n".join(lines)
        return _compact(rendered, max_chars)


class LedgerContextAssembler:
    def __init__(self, ledger_store: ConversationLedgerStore, max_recent_entries: int = 30):
        self.ledger_store = ledger_store
        self.max_recent_entries = max_recent_entries

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
        return LedgerContextSnapshot(
            conversation_id=message.conversation_id,
            session_id=str(message.metadata.get("session_id") or DEFAULT_SESSION_ID),
            ledger_path=str(self.ledger_store.conversation_markdown_path(message.conversation_id)),
            recent_entries=visible_entries,
            quote_context=quote_context,
            file_refs=file_refs,
            link_refs=link_refs,
            memory=memory,
            analysis=_analyze(entries, message),
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


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

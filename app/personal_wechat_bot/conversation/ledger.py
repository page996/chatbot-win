from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.domain.models import NormalizedMessage, ReplyCandidate, utc_now_iso


@dataclass(frozen=True)
class LedgerTextBlock:
    kind: str
    text: str
    source_ref: str = ""
    token_estimate: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    message_id: str
    conversation_id: str
    conversation_type: str
    chat_title: str
    sender_name: str
    sender_wechat_id: str | None
    is_self: bool
    received_at: str
    sequence: int
    status: str
    text_blocks: list[dict[str, Any]]
    quote: dict[str, Any]
    attachments: list[dict[str, Any]]
    links: list[dict[str, Any]]
    source: str
    role: str = "user"
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)


class ConversationLedgerStore:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir) / "conversation_ledgers"
        self.root.mkdir(parents=True, exist_ok=True)

    def append_message(self, message: NormalizedMessage) -> LedgerEntry:
        conversation_dir = self._conversation_dir(message.conversation_id)
        entries = self._read_entries(message.conversation_id)
        existing = _find_entry_by_message_id(entries, message.message_id)
        if existing is not None:
            return _entry_from_payload(existing)
        entry = LedgerEntry(
            entry_id=_entry_id(message.message_id, message.conversation_id),
            message_id=message.message_id,
            conversation_id=message.conversation_id,
            conversation_type=message.conversation_type,
            chat_title=message.chat_title,
            sender_name=message.sender_name,
            sender_wechat_id=message.sender_wechat_id,
            is_self=message.is_self,
            received_at=message.received_at,
            sequence=self._next_sequence(entries),
            status="active",
            text_blocks=_text_blocks_from_message(message),
            quote=_quote_from_metadata(message.metadata),
            attachments=_attachments_from_metadata(message.metadata),
            links=_links_from_text(message.text),
            source=str(message.metadata.get("source", "")),
            role="self" if message.is_self else "user",
        )
        self._append_entry(conversation_dir, entry)
        self._write_state(message.conversation_id, entry)
        self._render_conversation(message.conversation_id)
        return entry

    def annotate_link(
        self,
        conversation_id: str,
        entry_id: str,
        url_id: str,
        *,
        status: str,
        summary: str = "",
        text: str = "",
        source_path: str = "",
        error: str = "",
    ) -> bool:
        entries = self._read_entries(conversation_id)
        changed = False
        updated: list[dict[str, Any]] = []
        for item in entries:
            if item.get("entry_id") != entry_id:
                updated.append(item)
                continue
            item = dict(item)
            item["links"] = _annotated_links(item.get("links", []), url_id, status, source_path, summary, error)
            if text and status == "completed":
                annotation_path = self._write_annotation(
                    conversation_id,
                    entry_id,
                    url_id,
                    text=text,
                    summary=summary,
                    source_path=source_path,
                )
                item["text_blocks"] = _upsert_text_block(
                    item.get("text_blocks", []),
                    kind="annotation:web",
                    source_ref=str(annotation_path),
                    text=_web_annotation_text(summary, text),
                    metadata={"url_id": url_id, "status": status},
                )
            item["updated_at"] = utc_now_iso()
            updated.append(item)
            changed = True
        if not changed:
            return False
        self._rewrite_entries(conversation_id, updated)
        self._render_conversation(conversation_id)
        return True

    def append_reply(
        self,
        reply: ReplyCandidate,
        *,
        chat_title: str = "",
        sender_name: str = "agent",
        conversation_type: str = "private",
    ) -> LedgerEntry:
        entries = self._read_entries(reply.conversation_id)
        entry = LedgerEntry(
            entry_id=_entry_id(f"reply:{reply.message_id}:{reply.created_at}", reply.conversation_id),
            message_id=reply.message_id,
            conversation_id=reply.conversation_id,
            conversation_type=conversation_type,
            chat_title=chat_title,
            sender_name=sender_name,
            sender_wechat_id=None,
            is_self=True,
            received_at=reply.created_at,
            sequence=self._next_sequence(entries),
            status="active",
            text_blocks=[asdict(LedgerTextBlock(kind="reply", text=reply.text, token_estimate=_estimate_tokens(reply.text)))],
            quote={},
            attachments=[],
            links=_links_from_text(reply.text),
            source="reply_candidate",
            role="assistant",
            created_at=reply.created_at,
            updated_at=reply.created_at,
        )
        conversation_dir = self._conversation_dir(reply.conversation_id)
        self._append_entry(conversation_dir, entry)
        self._write_state(reply.conversation_id, entry)
        self._render_conversation(reply.conversation_id)
        return entry

    def mark_recalled(self, conversation_id: str, message_id: str, *, reason: str = "wechat_recall") -> bool:
        entries = self._read_entries(conversation_id)
        changed = False
        updated: list[dict[str, Any]] = []
        for item in entries:
            if item.get("message_id") == message_id and item.get("status") == "active":
                item = dict(item)
                item["status"] = "recalled"
                item["recall_reason"] = reason
                item["updated_at"] = utc_now_iso()
                changed = True
            updated.append(item)
        if not changed:
            return False
        self._rewrite_entries(conversation_id, updated)
        self._render_conversation(conversation_id)
        return True

    def read_entries(self, conversation_id: str, *, include_removed: bool = False) -> list[LedgerEntry]:
        entries = self._read_entries(conversation_id)
        if not include_removed:
            entries = [item for item in entries if item.get("status") == "active"]
        return [_entry_from_payload(item) for item in entries]

    def lookup_quote_context(
        self,
        conversation_id: str,
        quote: dict[str, Any],
        *,
        neighbor_radius: int = 2,
    ) -> dict[str, Any]:
        entries = self._read_entries(conversation_id)
        active = [item for item in entries if item.get("status") == "active"]
        index = _find_quote_index(active, quote)
        if index is None:
            return {"status": "not_found", "quote": quote, "entries": [], "attachments": []}
        start = max(0, index - neighbor_radius)
        end = min(len(active), index + neighbor_radius + 1)
        window = active[start:end]
        attachments: list[dict[str, Any]] = []
        for item in window:
            for attachment in item.get("attachments", []):
                if isinstance(attachment, dict):
                    attachments.append(dict(attachment))
        return {
            "status": "found",
            "quote": quote,
            "matched_entry_id": active[index].get("entry_id", ""),
            "entries": window,
            "attachments": attachments,
        }

    def conversation_markdown_path(self, conversation_id: str) -> Path:
        return self._conversation_dir(conversation_id) / "conversation.md"

    def annotations_dir(self, conversation_id: str) -> Path:
        return self._conversation_dir(conversation_id) / "annotations"

    def _conversation_dir(self, conversation_id: str) -> Path:
        return self.root / _safe_segment(conversation_id)

    def _messages_path(self, conversation_id: str) -> Path:
        return self._conversation_dir(conversation_id) / "messages.jsonl"

    def _state_path(self, conversation_id: str) -> Path:
        return self._conversation_dir(conversation_id) / "state.json"

    def _read_entries(self, conversation_id: str) -> list[dict[str, Any]]:
        path = self._messages_path(conversation_id)
        if not path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    entries.append(payload)
        return entries

    def _append_entry(self, conversation_dir: Path, entry: LedgerEntry) -> None:
        conversation_dir.mkdir(parents=True, exist_ok=True)
        with (conversation_dir / "messages.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def _rewrite_entries(self, conversation_id: str, entries: list[dict[str, Any]]) -> None:
        path = self._messages_path(conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for item in entries:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        tmp.replace(path)

    def _next_sequence(self, entries: list[dict[str, Any]]) -> int:
        if not entries:
            return 1
        return max(int(item.get("sequence", 0) or 0) for item in entries) + 1

    def _write_state(self, conversation_id: str, entry: LedgerEntry) -> None:
        path = self._state_path(conversation_id)
        payload = {
            "conversation_id": conversation_id,
            "last_sequence": entry.sequence,
            "last_entry_id": entry.entry_id,
            "updated_at": utc_now_iso(),
        }
        _write_json(path, payload)

    def _render_conversation(self, conversation_id: str) -> None:
        entries = self._read_entries(conversation_id)
        path = self.conversation_markdown_path(conversation_id)
        lines = [f"# Conversation {conversation_id}", ""]
        for item in entries:
            lines.extend(_render_entry_markdown(item))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _write_annotation(
        self,
        conversation_id: str,
        entry_id: str,
        url_id: str,
        *,
        text: str,
        summary: str,
        source_path: str,
    ) -> Path:
        path = self.annotations_dir(conversation_id) / f"{_safe_segment(entry_id)}_{_safe_segment(url_id)}.md"
        lines = [
            "# Web Annotation",
            "",
            f"entry_id: {entry_id}",
            f"url_id: {url_id}",
            f"source_path: {source_path}",
            "",
            "## Summary",
            "",
            summary,
            "",
            "## Text",
            "",
            text,
            "",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


def _text_blocks_from_message(message: NormalizedMessage) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    text = _primary_message_text(message).strip()
    if text:
        blocks.append(
            asdict(
                LedgerTextBlock(
                    kind="text",
                    text=text,
                    token_estimate=_estimate_tokens(text),
                    metadata={"visible_in_context": True},
                )
            )
        )
    for attachment in _attachments_from_metadata(message.metadata):
        parse = attachment.get("parse") if isinstance(attachment.get("parse"), dict) else {}
        parsed_text = str(parse.get("text", "")).strip()
        if parsed_text:
            blocks.append(
                asdict(
                    LedgerTextBlock(
                        kind=f"attachment:{parse.get('kind', attachment.get('kind', 'file'))}",
                        text=parsed_text,
                        source_ref=str(_attachment_source_ref(attachment)),
                        token_estimate=_estimate_tokens(parsed_text),
                        metadata={"file_id": attachment.get("file_id", ""), "name": attachment.get("name", "")},
                    )
                )
            )
    return blocks


def _primary_message_text(message: NormalizedMessage) -> str:
    original = message.metadata.get("original_text")
    if isinstance(original, str):
        return original
    return message.text


def _attachments_from_metadata(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    raw = metadata.get("attachments", [])
    if not isinstance(raw, list):
        return []
    attachments: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        attachment = dict(item)
        _attach_parse_artifact_refs(attachment)
        attachments.append(attachment)
    return attachments


def _attach_parse_artifact_refs(attachment: dict[str, Any]) -> None:
    workspace = attachment.get("workspace") if isinstance(attachment.get("workspace"), dict) else {}
    parse = attachment.get("parse") if isinstance(attachment.get("parse"), dict) else {}
    derived_dir = str(workspace.get("derived_dir", "")).strip()
    if not derived_dir:
        return
    content_path = parse.get("content_path") or str(Path(derived_dir) / "content.md")
    analysis_path = parse.get("analysis_path") or str(Path(derived_dir) / "analysis.json")
    artifacts = attachment.get("artifacts") if isinstance(attachment.get("artifacts"), dict) else {}
    attachment["artifacts"] = {
        **artifacts,
        "content_path": str(content_path),
        "analysis_path": str(analysis_path),
        "parse_result_path": str(Path(derived_dir) / "parse_result.json"),
        "chunks_dir": artifacts.get("chunks_dir", str(Path(derived_dir) / "chunks")),
        "table_index_path": artifacts.get("table_index_path", ""),
        "table_chunk_count": artifacts.get("table_chunk_count", 0),
        "media_dir": artifacts.get("media_dir", str(Path(derived_dir) / "media")),
        "media_index_path": artifacts.get("media_index_path", str(Path(derived_dir) / "media" / "index.json")),
        "media_extract_count": artifacts.get("media_extract_count", 0),
    }


def _quote_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    raw = metadata.get("quote") or metadata.get("quoted_message") or {}
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict):
        return {}
    quote = {
        "message_id": str(raw.get("message_id") or raw.get("quoted_message_id") or "").strip(),
        "entry_id": str(raw.get("entry_id") or raw.get("quoted_entry_id") or "").strip(),
        "sender_name": str(raw.get("sender_name") or raw.get("quoted_sender_name") or "").strip(),
        "text": str(raw.get("text") or raw.get("quoted_text") or raw.get("content") or "").strip(),
        "received_at": str(raw.get("received_at") or raw.get("quoted_received_at") or "").strip(),
        "source": str(raw.get("source") or "").strip(),
    }
    return {key: value for key, value in quote.items() if value}


def _links_from_text(text: str) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for url in _URL_RE.findall(text):
        links.append({"url": url, "url_id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:16], "status": "pending"})
    return links


def _annotated_links(
    links: Any,
    url_id: str,
    status: str,
    source_path: str,
    summary: str,
    error: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    found = False
    raw_links = links if isinstance(links, list) else []
    for item in raw_links:
        if not isinstance(item, dict):
            continue
        link = dict(item)
        if str(link.get("url_id", "")) == url_id:
            link["status"] = status
            link["annotation_path"] = source_path
            link["summary"] = summary
            link["error"] = error
            link["updated_at"] = utc_now_iso()
            found = True
        result.append(link)
    if not found:
        result.append(
            {
                "url_id": url_id,
                "url": "",
                "status": status,
                "annotation_path": source_path,
                "summary": summary,
                "error": error,
                "updated_at": utc_now_iso(),
            }
        )
    return result


def _upsert_text_block(
    blocks: Any,
    *,
    kind: str,
    source_ref: str,
    text: str,
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    replaced = False
    raw_blocks = blocks if isinstance(blocks, list) else []
    for item in raw_blocks:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == kind and item.get("source_ref") == source_ref:
            result.append(
                asdict(
                    LedgerTextBlock(
                        kind=kind,
                        text=text,
                        source_ref=source_ref,
                        token_estimate=_estimate_tokens(text),
                        metadata=metadata,
                    )
                )
            )
            replaced = True
        else:
            result.append(dict(item))
    if not replaced:
        result.append(
            asdict(
                LedgerTextBlock(
                    kind=kind,
                    text=text,
                    source_ref=source_ref,
                    token_estimate=_estimate_tokens(text),
                    metadata=metadata,
                )
            )
        )
    return result


def _web_annotation_text(summary: str, text: str, max_chars: int = 3000) -> str:
    content = summary.strip() or text.strip()
    if len(content) <= max_chars:
        return content
    return content[: max_chars - 1].rstrip() + "..."


def _render_entry_markdown(item: dict[str, Any]) -> list[str]:
    sequence = int(item.get("sequence", 0) or 0)
    received_at = str(item.get("received_at", ""))
    sender = str(item.get("sender_name", ""))
    status = str(item.get("status", "active"))
    lines = [f"## {sequence:06d} {received_at} {sender}", ""]
    if status != "active":
        lines.append(f"[{status}] message_id={item.get('message_id', '')}")
        lines.append("")
        return lines
    quote = item.get("quote") if isinstance(item.get("quote"), dict) else {}
    if quote:
        quote_text = str(quote.get("text", "")).strip()
        quote_ref = str(quote.get("message_id") or quote.get("entry_id") or "").strip()
        lines.append(f"> Quote {quote_ref}: {quote_text}".rstrip())
        lines.append("")
    for block in item.get("text_blocks", []):
        if not isinstance(block, dict):
            continue
        kind = str(block.get("kind", "text"))
        text = str(block.get("text", "")).strip()
        source_ref = str(block.get("source_ref", "")).strip()
        if kind != "text":
            lines.append(f"[block:{kind}{' source=' + source_ref if source_ref else ''}]")
        if text:
            lines.append(text)
        lines.append("")
    for attachment in item.get("attachments", []):
        if not isinstance(attachment, dict):
            continue
        name = str(attachment.get("name", ""))
        file_id = str(attachment.get("file_id", ""))
        workspace = attachment.get("workspace") if isinstance(attachment.get("workspace"), dict) else {}
        manifest = str(workspace.get("manifest_path", ""))
        lines.append(f"[file:{file_id} name={name} manifest={manifest}]")
    if item.get("attachments"):
        lines.append("")
    for link in item.get("links", []):
        if isinstance(link, dict):
            lines.append(f"[link:{link.get('url_id', '')}] {link.get('url', '')}")
    if item.get("links"):
        lines.append("")
    return lines


def _find_entry_by_message_id(entries: list[dict[str, Any]], message_id: str) -> dict[str, Any] | None:
    for item in entries:
        if item.get("message_id") == message_id:
            return item
    return None


def _find_quote_index(entries: list[dict[str, Any]], quote: dict[str, Any]) -> int | None:
    entry_id = str(quote.get("entry_id", "")).strip()
    message_id = str(quote.get("message_id", "")).strip()
    quote_text = str(quote.get("text", "")).strip()
    for index, item in enumerate(entries):
        if entry_id and item.get("entry_id") == entry_id:
            return index
        if message_id and item.get("message_id") == message_id:
            return index
    if quote_text:
        normalized_quote = _normalize_for_match(quote_text)
        matches: list[int] = []
        for index, item in enumerate(entries):
            text = "\n".join(
                str(block.get("text", ""))
                for block in item.get("text_blocks", [])
                if isinstance(block, dict)
            )
            normalized_text = _normalize_for_match(text)
            if normalized_quote and (normalized_quote in normalized_text or normalized_text in normalized_quote):
                matches.append(index)
        if matches:
            return matches[-1]
    return None


def _entry_from_payload(payload: dict[str, Any]) -> LedgerEntry:
    return LedgerEntry(
        entry_id=str(payload.get("entry_id", "")),
        message_id=str(payload.get("message_id", "")),
        conversation_id=str(payload.get("conversation_id", "")),
        conversation_type=str(payload.get("conversation_type", "")),
        chat_title=str(payload.get("chat_title", "")),
        sender_name=str(payload.get("sender_name", "")),
        sender_wechat_id=str(payload.get("sender_wechat_id") or "") or None,
        is_self=bool(payload.get("is_self", False)),
        received_at=str(payload.get("received_at", "")),
        sequence=int(payload.get("sequence", 0) or 0),
        status=str(payload.get("status", "active")),
        text_blocks=[dict(item) for item in payload.get("text_blocks", []) if isinstance(item, dict)],
        quote=dict(payload.get("quote", {})) if isinstance(payload.get("quote"), dict) else {},
        attachments=[dict(item) for item in payload.get("attachments", []) if isinstance(item, dict)],
        links=[dict(item) for item in payload.get("links", []) if isinstance(item, dict)],
        source=str(payload.get("source", "")),
        role=str(payload.get("role", "user")),
        created_at=str(payload.get("created_at", "")),
        updated_at=str(payload.get("updated_at", "")),
    )


def _attachment_source_ref(attachment: dict[str, Any]) -> str:
    workspace = attachment.get("workspace") if isinstance(attachment.get("workspace"), dict) else {}
    return str(workspace.get("manifest_path") or workspace.get("derived_dir") or workspace.get("workspace_dir") or "")


def _entry_id(message_id: str, conversation_id: str) -> str:
    return hashlib.sha256(f"{conversation_id}:{message_id}".encode("utf-8")).hexdigest()[:24]


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "default"


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

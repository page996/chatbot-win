from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore, LedgerEntry
from app.personal_wechat_bot.conversation.text_blocks import is_authored_block
from app.personal_wechat_bot.domain.models import ToolCallRequest
from app.personal_wechat_bot.tools.runtime import ToolRuntime


class LinkAnnotationService:
    def __init__(
        self,
        ledger_store: ConversationLedgerStore,
        tools: ToolRuntime,
        *,
        max_links_per_message: int = 2,
    ):
        self.ledger_store = ledger_store
        self.tools = tools
        self.max_links_per_message = max_links_per_message

    def annotate_entry(self, entry: LedgerEntry) -> list[dict[str, Any]]:
        if not _explicit_link_read_request(entry):
            return []
        results: list[dict[str, Any]] = []
        for target_entry, link in self._target_links(entry)[: self.max_links_per_message]:
            url = str(link.get("url", "")).strip()
            url_id = str(link.get("url_id", "")).strip()
            if not url or not url_id:
                continue
            if (
                str(link.get("status") or "") == "completed"
                and str(link.get("annotation_path") or "").strip()
                and _has_fresh_link_evidence(target_entry, url_id)
            ):
                continue
            request = ToolCallRequest(
                tool_name="web.fetch",
                call_id=_call_id(target_entry.entry_id, url_id),
                conversation_id=entry.conversation_id,
                requested_by="link_annotation",
                arguments={
                    "url": url,
                    "task": _entry_text(entry),
                    "session_id": entry.session_id,
                    "chat_title": entry.chat_title,
                },
            )
            result = self.tools.execute(request)
            source_path = result.output_refs[0] if result.output_refs else ""
            annotation_text = str(result.payload.get("text", "")) if isinstance(result.payload, dict) else ""
            self.ledger_store.annotate_link(
                target_entry.conversation_id,
                target_entry.entry_id,
                url_id,
                status=result.status,
                summary=result.summary,
                text=annotation_text,
                source_path=source_path,
                error=result.error or "",
            )
            results.append({"url": url, "url_id": url_id, "tool_result": asdict(result)})
        return results

    def _target_links(self, entry: LedgerEntry) -> list[tuple[LedgerEntry, dict[str, Any]]]:
        direct = [(entry, item) for item in entry.links if isinstance(item, dict)]
        if direct:
            return direct
        quote = entry.quote if isinstance(entry.quote, dict) else {}
        if not quote:
            return []
        quote_context = self.ledger_store.lookup_quote_context(entry.conversation_id, quote)
        matched_entry_id = str(quote_context.get("matched_entry_id") or "") if isinstance(quote_context, dict) else ""
        results: list[tuple[LedgerEntry, dict[str, Any]]] = []
        for payload in quote_context.get("entries", []) if isinstance(quote_context, dict) else []:
            if matched_entry_id and str(payload.get("entry_id") or "") != matched_entry_id:
                continue
            target = _entry_from_payload(payload)
            if target is None:
                continue
            for link in target.links:
                if isinstance(link, dict):
                    results.append((target, link))
        return results


def _call_id(entry_id: str, url_id: str) -> str:
    return hashlib.sha256(f"{entry_id}:web.fetch:{url_id}".encode("utf-8")).hexdigest()[:24]


def _has_fresh_link_evidence(entry: LedgerEntry, url_id: str) -> bool:
    for block in entry.text_blocks:
        if not isinstance(block, dict) or str(block.get("kind") or "") != "annotation:web":
            continue
        metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
        if str(metadata.get("url_id") or "") != url_id or str(metadata.get("status") or "") != "completed":
            continue
        expires_at = str(metadata.get("expires_at") or "").strip()
        if not expires_at or not str(block.get("text") or "").strip():
            continue
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry.astimezone(timezone.utc) > datetime.now(timezone.utc):
            return True
    return False


def _explicit_link_read_request(entry: LedgerEntry) -> bool:
    text = _entry_text(entry)
    if not text:
        return False
    normalized = text.lower()
    if "#web" in normalized or "webfetch" in normalized or "web.fetch" in normalized:
        return True
    has_url = bool(_URL_RE.search(text))
    has_quote = bool(entry.quote)
    if not (has_url or has_quote):
        return False
    wants_read = any(pattern.search(text) for pattern in _READ_PATTERNS)
    if has_quote and wants_read:
        return True
    return wants_read and any(marker in normalized for marker in ("链接", "网址", "网页", "网站", "url", "http", "pdf"))


def _entry_text(entry: LedgerEntry) -> str:
    parts: list[str] = []
    for block in entry.text_blocks:
        if not isinstance(block, dict):
            continue
        if not is_authored_block(block):
            continue
        text = str(block.get("text", "")).strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _entry_from_payload(payload: Any) -> LedgerEntry | None:
    if not isinstance(payload, dict):
        return None
    try:
        return LedgerEntry(
            entry_id=str(payload.get("entry_id", "")),
            message_id=str(payload.get("message_id", "")),
            dedupe_key=str(payload.get("dedupe_key", "")),
            message_ids=[
                str(item)
                for item in payload.get("message_ids", [])
                if str(item).strip()
            ]
            or [str(payload.get("message_id", ""))],
            conversation_id=str(payload.get("conversation_id", "")),
            session_id=str(payload.get("session_id", "session_default")),
            conversation_type=str(payload.get("conversation_type", "private")),
            chat_title=str(payload.get("chat_title", "")),
            sender_name=str(payload.get("sender_name", "")),
            sender_wechat_id=str(payload.get("sender_wechat_id", "")),
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
            send=dict(payload.get("send", {})) if isinstance(payload.get("send"), dict) else {},
            created_at=str(payload.get("created_at", "")),
            updated_at=str(payload.get("updated_at", "")),
        )
    except Exception:
        return None


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_READ_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"读(一下|取|这个|这些)?",
        r"阅读",
        r"打开",
        r"看(一下|看)?",
        r"总结",
        r"分析",
        r"提取",
        r"fetch",
        r"read",
        r"summarize",
        r"analyse|analyze",
        r"open",
    )
]

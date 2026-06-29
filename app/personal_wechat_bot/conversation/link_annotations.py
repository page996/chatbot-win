from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any

from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore, LedgerEntry
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
        results: list[dict[str, Any]] = []
        links = [item for item in entry.links if isinstance(item, dict)]
        for link in links[: self.max_links_per_message]:
            url = str(link.get("url", "")).strip()
            url_id = str(link.get("url_id", "")).strip()
            if not url or not url_id:
                continue
            request = ToolCallRequest(
                tool_name="web.fetch",
                call_id=_call_id(entry.entry_id, url_id),
                conversation_id=entry.conversation_id,
                requested_by="link_annotation",
                arguments={"url": url},
            )
            result = self.tools.execute(request)
            source_path = result.output_refs[0] if result.output_refs else ""
            annotation_text = str(result.payload.get("text", "")) if isinstance(result.payload, dict) else ""
            self.ledger_store.annotate_link(
                entry.conversation_id,
                entry.entry_id,
                url_id,
                status=result.status,
                summary=result.summary,
                text=annotation_text,
                source_path=source_path,
                error=result.error or "",
            )
            results.append({"url": url, "url_id": url_id, "tool_result": asdict(result)})
        return results


def _call_id(entry_id: str, url_id: str) -> str:
    return hashlib.sha256(f"{entry_id}:web.fetch:{url_id}".encode("utf-8")).hexdigest()[:24]

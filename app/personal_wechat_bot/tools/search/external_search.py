from __future__ import annotations

import re
from pathlib import Path

from app.personal_wechat_bot.domain.models import ToolCallRequest, ToolCallResult
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.base import ToolManifest
from app.personal_wechat_bot.tools.search.model_relevance_filter import FakeModelRelevanceFilter


class FakeExternalSearchTool:
    manifest = ToolManifest(
        name="search.external_translate",
        description="fake Chrome/Google 外网检索与翻译工具",
        supports_async=False,
    )

    def __init__(self, output_dir: str | Path, file_index: FileIndex, relevance_filter: FakeModelRelevanceFilter):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.file_index = file_index
        self.relevance_filter = relevance_filter

    def run(self, request: ToolCallRequest) -> ToolCallResult:
        query = request.arguments.get("query", "")
        candidates = [
            {
                "title": f"Primary source about {query}",
                "url": "https://example.org/research/source",
                "snippet": f"This source explains the main evidence and context for {query}.",
            },
            {
                "title": "Unrelated shopping result",
                "url": "https://ads.example.com/buy",
                "snippet": "Buy now",
                "irrelevant": True,
            },
        ]
        kept = []
        for item in candidates:
            keep, reason = self.relevance_filter.keep(query, item)
            if keep:
                item["reason"] = reason
                kept.append(item)
        source_text = "\n\n".join(f"{item['title']}\n{item['url']}\n{item['snippet']}" for item in kept)
        slug = _slug(query) or "search"
        source_path = self.output_dir / f"{slug}_source.txt"
        source_path.write_text(source_text, encoding="utf-8")
        file_id = self.file_index.add(source_path, source="search.external_translate", original_name=source_path.name)
        summary = "；".join(f"{item['title']}（{item['url']}）：{item['snippet']}" for item in kept)
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=self.manifest.name,
            status="completed",
            summary=f"检索摘要：{summary}",
            output_refs=[str(source_path)],
            payload={"file_id": file_id, "results": kept},
        )


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return slug[:40]

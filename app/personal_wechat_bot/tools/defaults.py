from __future__ import annotations

from pathlib import Path

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.document.translator import FakeDocumentTranslateTool
from app.personal_wechat_bot.tools.permissions import resolve_allowed_roots
from app.personal_wechat_bot.tools.registry import ToolRegistry
from app.personal_wechat_bot.tools.search.external_search import FakeExternalSearchTool
from app.personal_wechat_bot.tools.search.model_relevance_filter import FakeModelRelevanceFilter
from app.personal_wechat_bot.tools.vision.ocr_tool import OcrImageTool
from app.personal_wechat_bot.tools.web.fetch import WebFetchTool


def register_default_tools(
    registry: ToolRegistry,
    *,
    data_root: Path,
    config: BotConfig,
    file_index: FileIndex,
) -> None:
    relevance_filter = FakeModelRelevanceFilter()
    input_roots = resolve_allowed_roots(data_root, config.file_read_roots)
    workspace_roots = [
        (data_root / "file_workspace").resolve(),
        (data_root / "tool_outputs").resolve(),
    ]
    registry.register(
        FakeDocumentTranslateTool(
            data_root / "tool_outputs",
            file_index,
            allowed_input_roots=input_roots,
            allowed_extensions=config.file_allowed_extensions,
            max_input_bytes=config.file_max_bytes,
        )
    )
    registry.register(FakeExternalSearchTool(data_root / "tool_outputs", file_index, relevance_filter))
    registry.register(
        OcrImageTool(
            data_root / "tool_outputs" / "vision_ocr",
            file_index,
            allowed_input_roots=[*input_roots, *workspace_roots],
            max_input_bytes=config.file_max_bytes,
        )
    )
    registry.register(WebFetchTool(data_root / "tool_outputs" / "web_fetch", file_index))

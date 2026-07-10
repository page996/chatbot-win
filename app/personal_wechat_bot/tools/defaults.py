from __future__ import annotations

from pathlib import Path

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.document.translator import FakeDocumentTranslateTool
from app.personal_wechat_bot.tools.file_read import FileReadTool
from app.personal_wechat_bot.tools.permissions import resolve_allowed_roots
from app.personal_wechat_bot.tools.registry import ToolRegistry
from app.personal_wechat_bot.tools.vision.ocr_tool import OcrImageTool
from app.personal_wechat_bot.tools.voice.asr_tool import LocalAsrTool
from app.personal_wechat_bot.tools.web.fetch import WebFetchTool
from app.personal_wechat_bot.tools.web.search import WebSearchTool
from app.personal_wechat_bot.vision.ocr import build_default_ocr_engine
from app.personal_wechat_bot.voice.asr import LocalAsrSubprocessEngine
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import BackendAttachmentParser
from app.personal_wechat_bot.workspace.file_workspace import FileWorkspace


def register_default_tools(
    registry: ToolRegistry,
    *,
    data_root: Path,
    config: BotConfig,
    file_index: FileIndex,
    file_workspace: FileWorkspace | None = None,
    attachment_parser: BackendAttachmentParser | None = None,
) -> None:
    input_roots = resolve_allowed_roots(data_root, config.file_read_roots)
    workspace_roots = [
        (data_root / "file_workspace").resolve(),
        (data_root / "tool_outputs").resolve(),
    ]
    registry.register(FileReadTool(data_root))
    registry.register(
        FakeDocumentTranslateTool(
            data_root / "tool_outputs",
            file_index,
            allowed_input_roots=input_roots,
            allowed_extensions=config.file_allowed_extensions,
            max_input_bytes=config.file_max_bytes,
        )
    )
    registry.register(
        OcrImageTool(
            data_root / "tool_outputs" / "vision_ocr",
            file_index,
            allowed_input_roots=[*input_roots, *workspace_roots],
            max_input_bytes=config.file_max_bytes,
            ocr_engine=build_default_ocr_engine(mode=config.ocr_mode),
        )
    )
    registry.register(
        LocalAsrTool(
            data_root / "tool_outputs" / "voice_asr",
            file_index,
            allowed_input_roots=[*input_roots, *workspace_roots],
            max_input_bytes=config.file_max_bytes,
            asr_engine=LocalAsrSubprocessEngine(mode=config.asr_mode),
        )
    )
    registry.register(
        WebFetchTool(
            data_root / "tool_outputs" / "web_fetch",
            file_index,
            file_workspace=file_workspace,
            attachment_parser=attachment_parser,
        )
    )
    registry.register(
        WebSearchTool(
            data_root / "tool_outputs" / "web_search",
            file_index,
            blocklist=config.search_blocklist,
        )
    )

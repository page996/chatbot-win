from __future__ import annotations

from pathlib import Path

from app.personal_wechat_bot.domain.models import ToolCallRequest, ToolCallResult
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.base import ToolManifest
from app.personal_wechat_bot.tools.permissions import validate_readable_file


class FakeDocumentTranslateTool:
    manifest = ToolManifest(
        name="document.translate",
        description="fake 文档/文本翻译工具，返回 DOCX 文件引用",
        supports_async=False,
    )

    def __init__(
        self,
        output_dir: str | Path,
        file_index: FileIndex,
        allowed_input_roots: list[Path] | None = None,
        allowed_extensions: list[str] | None = None,
        max_input_bytes: int = 20 * 1024 * 1024,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.file_index = file_index
        self.allowed_input_roots = allowed_input_roots or []
        self.allowed_extensions = allowed_extensions or [".txt", ".md", ".docx", ".pdf"]
        self.max_input_bytes = max_input_bytes

    def run(self, request: ToolCallRequest) -> ToolCallResult:
        input_path = request.arguments.get("input_path")
        input_text = request.arguments.get("input_text")
        if input_path:
            try:
                safe_input = validate_readable_file(
                    input_path,
                    self.allowed_input_roots,
                    self.allowed_extensions,
                    self.max_input_bytes,
                )
            except (FileNotFoundError, PermissionError) as exc:
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=self.manifest.name,
                    status="blocked",
                    summary="文件读取被白名单规则阻止",
                    error=f"{type(exc).__name__}: {exc}",
                )
            original = safe_input.stem
            content = _read_text_preview(safe_input)
        else:
            original = "文本"
            content = input_text or ""
        output = self.output_dir / f"{original}翻译.docx"
        # This is a fake DOCX placeholder for the closed loop. Real DOCX writing
        # with rendered equations is a later module.
        output.write_text(f"FAKE_DOCX\n{content}\n", encoding="utf-8")
        file_id = self.file_index.add(output, source="document.translate", original_name=output.name)
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=self.manifest.name,
            status="completed",
            summary=f"已生成翻译文件引用：{output.name}",
            output_refs=[str(output)],
            payload={"file_id": file_id},
        )


def _read_text_preview(path: Path) -> str:
    if path.suffix.lower() in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="replace")[:20000]
    return f"Fake translation for {path.name}"

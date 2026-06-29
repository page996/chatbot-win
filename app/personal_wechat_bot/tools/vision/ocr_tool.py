from __future__ import annotations

import hashlib
from pathlib import Path

from app.personal_wechat_bot.domain.models import ToolCallRequest, ToolCallResult
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.base import ToolManifest
from app.personal_wechat_bot.tools.permissions import validate_readable_file
from app.personal_wechat_bot.vision.ocr import OcrEngine, RapidOcrSubprocessEngine


class OcrImageTool:
    manifest = ToolManifest(
        name="vision.ocr",
        description="OCR an image file from the isolated file workspace or an allowed file root.",
        supports_async=False,
    )

    def __init__(
        self,
        output_dir: str | Path,
        file_index: FileIndex,
        *,
        allowed_input_roots: list[Path],
        max_input_bytes: int,
        ocr_engine: OcrEngine | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.file_index = file_index
        self.allowed_input_roots = allowed_input_roots
        self.max_input_bytes = max_input_bytes
        self.ocr_engine = ocr_engine or RapidOcrSubprocessEngine()

    def run(self, request: ToolCallRequest) -> ToolCallResult:
        input_path = str(request.arguments.get("input_path") or request.arguments.get("path") or "").strip()
        if not input_path:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="blocked",
                summary="vision.ocr requires input_path",
                error="missing_input_path",
            )
        try:
            safe_input = validate_readable_file(
                input_path,
                self.allowed_input_roots,
                [".png", ".jpg", ".jpeg", ".bmp", ".webp"],
                self.max_input_bytes,
            )
        except (FileNotFoundError, PermissionError) as exc:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="blocked",
                summary="vision.ocr input path is not readable from allowed roots",
                error=f"{type(exc).__name__}: {exc}",
            )

        health = self.ocr_engine.health()
        if not health.available:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="blocked",
                summary="vision.ocr dependency is not available",
                error=health.detail or "ocr_unavailable",
                payload={"backend": health.backend, "gpu_available": health.gpu_available},
            )

        try:
            text = self.ocr_engine.read_text(safe_input)
        except Exception as exc:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="failed",
                summary="vision.ocr failed while reading the image",
                error=f"{type(exc).__name__}: {exc}",
            )

        output = self.output_dir / f"{_slug(safe_input)}_{request.call_id}.md"
        output.write_text(
            f"# OCR Result\n\nsource: {safe_input}\n\n{text.strip()}\n",
            encoding="utf-8",
        )
        file_id = self.file_index.add(output, source=self.manifest.name, original_name=output.name)
        summary_text = text.strip() or "(no text detected)"
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=self.manifest.name,
            status="completed",
            summary=f"OCR completed: {summary_text[:1000]}",
            output_refs=[str(output)],
            payload={"file_id": file_id, "text": text, "source_path": str(safe_input)},
        )


def _slug(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    stem = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in path.stem)[:40]
    return f"{stem or 'image'}_{digest}"

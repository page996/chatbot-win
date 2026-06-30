from __future__ import annotations

import hashlib
from pathlib import Path

from app.personal_wechat_bot.domain.models import ToolCallRequest, ToolCallResult
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.base import ToolManifest
from app.personal_wechat_bot.tools.permissions import validate_readable_file
from app.personal_wechat_bot.voice.asr import AsrEngine, LocalAsrSubprocessEngine
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import AUDIO_SUFFIXES


class LocalAsrTool:
    manifest = ToolManifest(
        name="voice.local_asr",
        description="Transcribe an audio file from the isolated file workspace or an allowed file root.",
        supports_async=False,
    )

    def __init__(
        self,
        output_dir: str | Path,
        file_index: FileIndex,
        *,
        allowed_input_roots: list[Path],
        max_input_bytes: int,
        asr_engine: AsrEngine | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.file_index = file_index
        self.allowed_input_roots = allowed_input_roots
        self.max_input_bytes = max_input_bytes
        self.asr_engine = asr_engine or LocalAsrSubprocessEngine()

    def run(self, request: ToolCallRequest) -> ToolCallResult:
        input_path = str(request.arguments.get("input_path") or request.arguments.get("path") or "").strip()
        if not input_path:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="blocked",
                summary="voice.local_asr requires input_path",
                error="missing_input_path",
            )
        try:
            safe_input = validate_readable_file(
                input_path,
                self.allowed_input_roots,
                sorted(AUDIO_SUFFIXES),
                self.max_input_bytes,
            )
        except (FileNotFoundError, PermissionError) as exc:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="blocked",
                summary="voice.local_asr input path is not readable from allowed roots",
                error=f"{type(exc).__name__}: {exc}",
            )

        transcript = self.asr_engine.transcribe(safe_input)
        if transcript.status != "transcribed":
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="blocked" if transcript.status == "blocked" else "failed",
                summary="voice.local_asr did not produce a transcript",
                error=transcript.error or transcript.status,
                payload={"backend": transcript.backend, "model": transcript.model, "source_path": str(safe_input)},
            )

        output = self.output_dir / f"{_slug(safe_input)}_{request.call_id}.md"
        output.write_text(
            "\n".join(
                [
                    "# Local ASR Result",
                    "",
                    f"source: {safe_input}",
                    f"backend: {transcript.backend}",
                    f"model: {transcript.model}",
                    f"language: {transcript.language}",
                    "",
                    transcript.text,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        file_id = self.file_index.add(output, source=self.manifest.name, original_name=output.name)
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=self.manifest.name,
            status="completed",
            summary=f"Local ASR completed: {transcript.text[:1000]}",
            output_refs=[str(output)],
            payload={"file_id": file_id, "text": transcript.text, "source_path": str(safe_input)},
        )


def _slug(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    stem = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in path.stem)[:40]
    return f"{stem or 'audio'}_{digest}"

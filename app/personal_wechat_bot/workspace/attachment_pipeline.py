from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.permissions import validate_readable_file
from app.personal_wechat_bot.vision.ocr import OcrEngine
from app.personal_wechat_bot.voice.asr import AsrEngine
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import BackendAttachmentParser
from app.personal_wechat_bot.workspace.file_workspace import FileWorkspace


PREVIEW_CHAR_TARGET = 8000


@dataclass(frozen=True)
class IncomingAttachment:
    path: str
    original_name: str = ""
    kind: str = "file"
    source: str = "backend_event_attachment"
    chat_title: str = ""


class AttachmentPipeline:
    """Validate, stage, parse, and index incoming files.

    Drivers should provide structured attachment events and let this pipeline
    own the middle-layer file lifecycle. That keeps WeChat/file-source adapters
    thin and makes future frontend monitor events use the same path.
    """

    def __init__(
        self,
        *,
        file_index: FileIndex,
        file_workspace: FileWorkspace,
        attachment_parser: BackendAttachmentParser,
        allowed_input_roots: list[Path],
        allowed_extensions: list[str],
        max_input_bytes: int,
        embedded_media_ocr: OcrEngine | None = None,
        embedded_media_asr: AsrEngine | None = None,
    ):
        self.file_index = file_index
        self.file_workspace = file_workspace
        self.attachment_parser = attachment_parser
        self.allowed_input_roots = allowed_input_roots
        self.allowed_extensions = allowed_extensions
        self.max_input_bytes = max_input_bytes
        self.embedded_media_ocr = embedded_media_ocr
        self.embedded_media_asr = embedded_media_asr

    def process(
        self,
        attachment: IncomingAttachment,
        *,
        conversation_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        try:
            safe_path = validate_readable_file(
                attachment.path,
                self.allowed_input_roots,
                self.allowed_extensions,
                self.max_input_bytes,
            )
            staged = self.file_workspace.stage_file(
                safe_path,
                conversation_id=conversation_id,
                session_id=session_id,
                original_name=attachment.original_name or safe_path.name,
                kind=attachment.kind,
                source=attachment.source,
                chat_title=attachment.chat_title,
            )
            file_id = self.file_index.add(
                staged.staged_path,
                source="file_workspace",
                original_name=attachment.original_name or safe_path.name,
            )
            parse_result = self.file_workspace.parse_or_get_cached(
                staged,
                self.attachment_parser,
                embedded_media_ocr=self.embedded_media_ocr,
                embedded_media_asr=self.embedded_media_asr,
            )
            artifacts = _artifact_refs(staged)
            context_text = _parse_context_text(parse_result)
            parse_text = _conversation_parse_text(context_text, parse_result.kind, artifacts)
            return {
                "status": "indexed",
                "source": attachment.source,
                "path": attachment.path,
                "file_id": file_id,
                "name": attachment.original_name or safe_path.name,
                "kind": attachment.kind,
                "suffix": safe_path.suffix.lower(),
                "workspace": {
                    "conversation_id": staged.conversation_id,
                    "session_id": staged.session_id,
                    "workspace_dir": staged.workspace_dir,
                    "staged_path": staged.staged_path,
                    "manifest_path": staged.manifest_path,
                    "derived_dir": staged.derived_dir,
                    "outputs_dir": staged.outputs_dir,
                    "sha256": staged.sha256,
                },
                "parse": {
                    "status": parse_result.status,
                    "kind": parse_result.kind,
                    "summary": parse_result.summary,
                    "text": parse_text,
                    "context_text": context_text,
                    "error": parse_result.error,
                },
                "artifacts": artifacts,
            }
        except (FileNotFoundError, PermissionError) as exc:
            return {
                "status": "blocked",
                "source": attachment.source,
                "path": attachment.path,
                "name": attachment.original_name or Path(attachment.path).name,
                "kind": attachment.kind,
                "reason": f"{type(exc).__name__}: {exc}",
            }


def _artifact_refs(staged) -> dict[str, Any]:
    derived_dir = Path(staged.derived_dir)
    analysis = _read_json(derived_dir / "analysis.json", {})
    chunks = analysis.get("chunks", []) if isinstance(analysis, dict) else []
    table_chunks = analysis.get("table_chunks", []) if isinstance(analysis, dict) else []
    media_images = analysis.get("media_images", []) if isinstance(analysis, dict) else []
    media_audio = analysis.get("media_audio", []) if isinstance(analysis, dict) else []
    return {
        "file_id": staged.file_id,
        "content_path": str(derived_dir / "content.md"),
        "full_text_path": str(derived_dir / "full_text.md") if (derived_dir / "full_text.md").is_file() else "",
        "analysis_path": str(derived_dir / "analysis.json"),
        "parse_result_path": str(derived_dir / "parse_result.json"),
        "ai_summary": str(analysis.get("ai_summary", "")) if isinstance(analysis, dict) else "",
        "preview_char_count": int(analysis.get("preview_char_count", 0) or 0) if isinstance(analysis, dict) else 0,
        "char_count": int(analysis.get("char_count", 0) or 0) if isinstance(analysis, dict) else 0,
        "chunks_dir": str(derived_dir / "chunks"),
        "chunk_count": len(chunks) if isinstance(chunks, list) else 0,
        "chunks": [dict(item) for item in chunks if isinstance(item, dict)] if isinstance(chunks, list) else [],
        "tables_dir": str(analysis.get("tables_dir", "")) if isinstance(analysis, dict) else "",
        "table_index_path": str(analysis.get("table_index_path", "")) if isinstance(analysis, dict) else "",
        "table_chunk_count": len(table_chunks) if isinstance(table_chunks, list) else 0,
        "table_chunks": [
            dict(item)
            for item in table_chunks
            if isinstance(item, dict)
        ]
        if isinstance(table_chunks, list)
        else [],
        "media_dir": str(analysis.get("media_dir", "")) if isinstance(analysis, dict) else "",
        "media_index_path": str(analysis.get("media_index_path", "")) if isinstance(analysis, dict) else "",
        "media_extract_count": int(analysis.get("media_extract_count", 0) or 0) if isinstance(analysis, dict) else 0,
        "media_ocr_status": str(analysis.get("media_ocr_status", "")) if isinstance(analysis, dict) else "",
        "media_ocr_dir": str(analysis.get("media_ocr_dir", "")) if isinstance(analysis, dict) else "",
        "media_ocr_index_path": str(analysis.get("media_ocr_index_path", "")) if isinstance(analysis, dict) else "",
        "media_ocr_count": int(analysis.get("media_ocr_count", 0) or 0) if isinstance(analysis, dict) else 0,
        "media_ocr_error_count": int(analysis.get("media_ocr_error_count", 0) or 0) if isinstance(analysis, dict) else 0,
        "media_asr_status": str(analysis.get("media_asr_status", "")) if isinstance(analysis, dict) else "",
        "media_asr_dir": str(analysis.get("media_asr_dir", "")) if isinstance(analysis, dict) else "",
        "media_asr_count": int(analysis.get("media_asr_count", 0) or 0) if isinstance(analysis, dict) else 0,
        "media_asr_error_count": int(analysis.get("media_asr_error_count", 0) or 0) if isinstance(analysis, dict) else 0,
        "media_images": [
            dict(item)
            for item in media_images
            if isinstance(item, dict)
        ]
        if isinstance(media_images, list)
        else [],
        "media_audio": [
            dict(item)
            for item in media_audio
            if isinstance(item, dict)
        ]
        if isinstance(media_audio, list)
        else [],
    }


def _conversation_parse_text(raw_text: str, kind: str, artifacts: dict[str, Any]) -> str:
    if kind != "spreadsheet":
        return _with_artifact_context(raw_text, artifacts)
    table_chunks = artifacts.get("table_chunks", [])
    first_chunk = table_chunks[0] if isinstance(table_chunks, list) and table_chunks else {}
    if not isinstance(first_chunk, dict):
        return _with_artifact_context(raw_text, artifacts)
    chunk_path_raw = str(first_chunk.get("path", "")).strip()
    if not chunk_path_raw:
        return _with_artifact_context(raw_text, artifacts)
    chunk_path = Path(chunk_path_raw)
    chunk_payload = _read_json(chunk_path, {})
    rows = chunk_payload.get("rows", []) if isinstance(chunk_payload, dict) else []
    preview_json = json.dumps(rows, ensure_ascii=False, indent=2) if isinstance(rows, list) else "[]"
    lines = [
        "[structured_table:first_chunk]",
        f"first_chunk_path={chunk_path}",
        "rows_json=",
        _compact(preview_json, 6000),
    ]
    # The flattened raw_text of a spreadsheet just repeats the rows above, so we
    # keep only the structured chunk here and let the index point at the rest.
    return _with_artifact_context("\n".join(lines).strip(), artifacts)


def _with_artifact_context(raw_text: str, artifacts: dict[str, Any]) -> str:
    lines = [raw_text.strip()] if raw_text.strip() else []
    index_line = _file_index_line(artifacts)
    if index_line:
        if lines:
            lines.append("")
        lines.append(index_line)
    return "\n".join(lines).strip()


def _file_index_line(artifacts: dict[str, Any]) -> str:
    # A single compact public index instead of a multi-line local path dump.
    # Full artifacts live in the file workspace and are tracked in JSON metadata.
    parts: list[str] = []
    file_id = str(artifacts.get("file_id", "")).strip()
    if file_id:
        parts.append(f"file_id={file_id}")
    char_count = int(artifacts.get("char_count", 0) or 0)
    if char_count:
        parts.append(f"chars={char_count}")
    counters = (
        ("chunk_count", "chunks"),
        ("table_chunk_count", "tables"),
        ("media_extract_count", "media"),
        ("media_ocr_count", "ocr"),
        ("media_asr_count", "asr"),
    )
    for key, label in counters:
        value = int(artifacts.get(key, 0) or 0)
        if value:
            parts.append(f"{label}={value}")
    if not parts:
        return ""
    index_line = "[file_index] " + " ".join(parts)
    summary = str(artifacts.get("ai_summary", "")).strip()
    if summary:
        # A one-line AI gist right under the index so the reader grasps the file
        # without opening content.md; the full analysis lives in analysis.json.
        index_line += "\n[ai_summary] " + _compact(summary.replace("\n", " "), 400)
    return index_line


def _parse_context_text(parse_result: Any) -> str:
    context = str(getattr(parse_result, "context_text", "") or "").strip()
    if context:
        return context
    return _compact(str(getattr(parse_result, "text", "") or "").strip(), PREVIEW_CHAR_TARGET)


def _compact(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

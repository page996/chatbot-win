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
from app.personal_wechat_bot.workspace.file_visibility import redact_file_internal_urls
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
            oversized = _oversized_attachment_placeholder(
                attachment,
                allowed_input_roots=self.allowed_input_roots,
                allowed_extensions=self.allowed_extensions,
                max_input_bytes=self.max_input_bytes,
            )
            if oversized is not None:
                return oversized
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
                    "ai_analysis_status": artifacts.get("ai_analysis_status", ""),
                    "ai_summary": artifacts.get("ai_summary", ""),
                    "ai_key_points": artifacts.get("ai_key_points", []),
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
    ocr_table_chunks = analysis.get("ocr_table_chunks", []) if isinstance(analysis, dict) else []
    media_images = analysis.get("media_images", []) if isinstance(analysis, dict) else []
    media_audio = analysis.get("media_audio", []) if isinstance(analysis, dict) else []
    return {
        "file_id": staged.file_id,
        "content_path": str(derived_dir / "content.md"),
        "full_text_path": str(derived_dir / "full_text.md") if (derived_dir / "full_text.md").is_file() else "",
        "analysis_path": str(derived_dir / "analysis.json"),
        "parse_result_path": str(derived_dir / "parse_result.json"),
        "ai_analysis_status": str(analysis.get("ai_analysis_status", "")) if isinstance(analysis, dict) else "",
        "ai_summary": str(analysis.get("ai_summary", "")) if isinstance(analysis, dict) else "",
        "ai_key_points": [
            str(item)
            for item in analysis.get("ai_key_points", [])
            if str(item).strip()
        ]
        if isinstance(analysis, dict)
        else [],
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
        "ocr_tables_dir": str(analysis.get("ocr_tables_dir", "")) if isinstance(analysis, dict) else "",
        "ocr_table_index_path": str(analysis.get("ocr_table_index_path", "")) if isinstance(analysis, dict) else "",
        "ocr_table_chunk_count": len(ocr_table_chunks) if isinstance(ocr_table_chunks, list) else 0,
        "ocr_table_chunks": [
            dict(item)
            for item in ocr_table_chunks
            if isinstance(item, dict)
        ]
        if isinstance(ocr_table_chunks, list)
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


def _oversized_attachment_placeholder(
    attachment: IncomingAttachment,
    *,
    allowed_input_roots: list[Path],
    allowed_extensions: list[str],
    max_input_bytes: int,
) -> dict[str, Any] | None:
    candidate = _resolve_candidate(attachment.path, allowed_input_roots)
    if candidate is None or not candidate.exists() or not candidate.is_file():
        return None
    if not _is_within_any(candidate, allowed_input_roots):
        return None
    allowed = {item.lower() for item in allowed_extensions}
    suffix = candidate.suffix.lower()
    if allowed and suffix not in allowed:
        return None
    try:
        size = candidate.stat().st_size
    except OSError:
        return None
    if size <= max_input_bytes:
        return None
    name = attachment.original_name or candidate.name
    reason = f"file_too_large:{size}>{max_input_bytes}"
    human = (
        f"文件过大，已占位但未处理：{name} "
        f"size={_format_bytes(size)} threshold={_format_bytes(max_input_bytes)}。"
        "请调高前端文件处理阈值，或让用户拆分/压缩后重新发送。"
    )
    return {
        "status": "skipped_too_large",
        "source": attachment.source,
        "path": attachment.path,
        "name": name,
        "kind": attachment.kind,
        "suffix": suffix,
        "reason": reason,
        "parse": {
            "status": "skipped_too_large",
            "kind": attachment.kind,
            "summary": human,
            "text": human,
            "context_text": human,
            "error": reason,
            "not_processed": True,
            "size_bytes": size,
            "max_bytes": max_input_bytes,
        },
        "artifacts": {
            "file_id": "",
            "char_count": 0,
            "chunk_count": 0,
            "too_large": True,
            "size_bytes": size,
            "max_bytes": max_input_bytes,
        },
    }


def _resolve_candidate(path: str | Path, roots: list[Path]) -> Path | None:
    raw = Path(path)
    if raw.is_absolute():
        return raw.resolve()
    for root in roots:
        candidate = (root / raw).resolve()
        if candidate.exists():
            return candidate
    if roots:
        return (roots[0] / raw).resolve()
    return raw.resolve()


def _is_within_any(candidate: Path, roots: list[Path]) -> bool:
    resolved = candidate.resolve()
    return any(root.resolve() == resolved or root.resolve() in resolved.parents for root in roots)


def _format_bytes(value: int) -> str:
    number = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if number < 1024 or unit == "GB":
            return f"{number:.1f}{unit}" if unit != "B" else f"{int(number)}B"
        number /= 1024
    return f"{value}B"


def _conversation_parse_text(raw_text: str, kind: str, artifacts: dict[str, Any]) -> str:
    # Keep conversation-visible file content compact. Full table/OCR row schemas
    # live in workspace artifacts and can be opened explicitly with file.read.
    return _with_artifact_context(redact_file_internal_urls(raw_text), artifacts)


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
        ("ocr_table_chunk_count", "ocr_tables"),
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
    summary = _usable_ai_summary(artifacts)
    if summary:
        # A one-line AI gist right under the index so the reader grasps the file
        # without opening content.md; the full analysis lives in analysis.json.
        index_line += "\n[ai_summary] " + _compact(redact_file_internal_urls(summary.replace("\n", " ")), 400)
    return index_line


def _usable_ai_summary(artifacts: dict[str, Any]) -> str:
    if str(artifacts.get("ai_analysis_status", "")).strip() != "analyzed":
        return ""
    summary = str(artifacts.get("ai_summary", "")).strip()
    if "fake_llm.completed" in summary or "PLAN:" in summary or "MONITOR:" in summary:
        return ""
    return summary


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

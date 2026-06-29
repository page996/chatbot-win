from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.permissions import validate_readable_file
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import BackendAttachmentParser
from app.personal_wechat_bot.workspace.file_workspace import FileWorkspace


@dataclass(frozen=True)
class IncomingAttachment:
    path: str
    original_name: str = ""
    kind: str = "file"
    source: str = "backend_event_attachment"


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
    ):
        self.file_index = file_index
        self.file_workspace = file_workspace
        self.attachment_parser = attachment_parser
        self.allowed_input_roots = allowed_input_roots
        self.allowed_extensions = allowed_extensions
        self.max_input_bytes = max_input_bytes

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
            )
            file_id = self.file_index.add(
                staged.staged_path,
                source="file_workspace",
                original_name=attachment.original_name or safe_path.name,
            )
            parse_result = self.file_workspace.parse_or_get_cached(staged, self.attachment_parser)
            artifacts = _artifact_refs(staged)
            parse_text = _conversation_parse_text(parse_result.text, parse_result.kind, artifacts)
            return {
                "status": "indexed",
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
                    "raw_text": parse_result.text,
                    "error": parse_result.error,
                },
                "artifacts": artifacts,
            }
        except (FileNotFoundError, PermissionError) as exc:
            return {
                "status": "blocked",
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
        "content_path": str(derived_dir / "content.md"),
        "analysis_path": str(derived_dir / "analysis.json"),
        "parse_result_path": str(derived_dir / "parse_result.json"),
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
        return raw_text
    table_chunks = artifacts.get("table_chunks", [])
    first_chunk = table_chunks[0] if isinstance(table_chunks, list) and table_chunks else {}
    if not isinstance(first_chunk, dict):
        return raw_text
    chunk_path_raw = str(first_chunk.get("path", "")).strip()
    if not chunk_path_raw:
        return raw_text
    chunk_path = Path(chunk_path_raw)
    chunk_payload = _read_json(chunk_path, {})
    rows = chunk_payload.get("rows", []) if isinstance(chunk_payload, dict) else []
    preview_json = json.dumps(rows, ensure_ascii=False, indent=2) if isinstance(rows, list) else "[]"
    lines = [
        "[structured_table:first_chunk]",
        f"table_index_path={artifacts.get('table_index_path', '')}",
        f"tables_dir={artifacts.get('tables_dir', '')}",
        f"table_chunk_count={artifacts.get('table_chunk_count', 0)}",
        f"first_chunk_path={chunk_path}",
        "rows_json=",
        _compact(preview_json, 6000),
    ]
    if raw_text.strip():
        lines.extend(["", "[spreadsheet_text_preview]", _compact(raw_text, 1200)])
    return "\n".join(lines).strip()


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

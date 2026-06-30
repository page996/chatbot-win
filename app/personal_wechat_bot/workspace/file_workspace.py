from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.tools.document.libreoffice import LibreOfficeRuntime
from app.personal_wechat_bot.vision.ocr import OcrEngine
from app.personal_wechat_bot.voice.asr import AsrEngine
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import (
    AUDIO_SUFFIXES,
    IMAGE_SUFFIXES,
    AttachmentParseResult,
)
from app.personal_wechat_bot.workspace.table_artifacts import SPREADSHEET_SUFFIXES, write_table_artifacts


CHUNK_TOKEN_TARGET = 1600
CHUNK_CHAR_TARGET = CHUNK_TOKEN_TARGET * 4


@dataclass(frozen=True)
class StagedFile:
    file_id: str
    conversation_id: str
    session_id: str
    original_name: str
    kind: str
    sha256: str
    workspace_dir: str
    staged_path: str
    manifest_path: str
    derived_dir: str
    outputs_dir: str
    source: str = "backend_event_attachment"


@dataclass(frozen=True)
class WorkspaceOperationResult:
    status: str
    summary: str
    output_path: str = ""
    error: str = ""


class FileWorkspace:
    """Copy user files into an isolated per-conversation/session workspace.

    The workspace keeps original WeChat files read-only from the bot's point of
    view. Parsers and CLI tools operate on the copied file and write derived
    artifacts next to it.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def stage_file(
        self,
        source_path: str | Path,
        *,
        conversation_id: str,
        session_id: str,
        original_name: str = "",
        kind: str = "file",
        source: str = "backend_event_attachment",
    ) -> StagedFile:
        source_file = Path(source_path).resolve()
        digest = _sha256_file(source_file)
        file_id = digest[:24]
        display_name = original_name or source_file.name
        workspace_dir = self.file_dir(conversation_id, session_id, file_id)
        original_dir = workspace_dir / "original"
        derived_dir = workspace_dir / "derived"
        outputs_dir = workspace_dir / "outputs"
        for child in [original_dir, derived_dir, outputs_dir]:
            child.mkdir(parents=True, exist_ok=True)

        staged_path = original_dir / _safe_filename(display_name, source_file.suffix)
        if not staged_path.exists() or _sha256_file(staged_path) != digest:
            shutil.copy2(source_file, staged_path)

        manifest_path = workspace_dir / "manifest.json"
        previous = _read_json(manifest_path, {})
        source_record = {
            "original_path": str(source_file),
            "original_name": display_name,
            "staged_path": str(staged_path),
            "kind": kind,
            "source": source,
            "observed_at": utc_now_iso(),
        }
        sources = _merge_sources(previous.get("sources", []), source_record)
        manifest = {
            "file_id": file_id,
            "conversation_id": conversation_id,
            "session_id": session_id,
            "original_name": display_name,
            "kind": kind,
            "source": source,
            "sha256": digest,
            "suffix": source_file.suffix.lower(),
            "original_path": str(source_file),
            "staged_path": str(staged_path),
            "workspace_dir": str(workspace_dir),
            "derived_dir": str(derived_dir),
            "outputs_dir": str(outputs_dir),
            "sources": sources,
            "created_at": previous.get("created_at") or utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        _write_json(manifest_path, manifest)
        staged = StagedFile(
            file_id=file_id,
            conversation_id=conversation_id,
            session_id=session_id,
            original_name=display_name,
            kind=kind,
            sha256=digest,
            workspace_dir=str(workspace_dir),
            staged_path=str(staged_path),
            manifest_path=str(manifest_path),
            derived_dir=str(derived_dir),
            outputs_dir=str(outputs_dir),
            source=source,
        )
        self._update_session_index(staged, manifest)
        return staged

    def read_parse_result(self, staged: StagedFile) -> AttachmentParseResult | None:
        payload = _read_json(Path(staged.derived_dir) / "parse_result.json", None)
        if not isinstance(payload, dict) or payload.get("sha256") != staged.sha256:
            return None
        cached_suffix = str(payload.get("staged_suffix", ""))
        if cached_suffix and cached_suffix != Path(staged.staged_path).suffix.lower():
            return None
        result = payload.get("result")
        if not isinstance(result, dict):
            return None
        return AttachmentParseResult(
            status=str(result.get("status", "")),
            kind=str(result.get("kind", "")),
            summary=str(result.get("summary", "")),
            text=str(result.get("text", "")),
            error=str(result.get("error", "")),
        )

    def write_parse_result(
        self,
        staged: StagedFile,
        result: AttachmentParseResult,
        *,
        embedded_media_ocr: OcrEngine | None = None,
        embedded_media_asr: AsrEngine | None = None,
    ) -> None:
        derived_dir = Path(staged.derived_dir)
        derived_dir.mkdir(parents=True, exist_ok=True)
        content_path = derived_dir / "content.md"
        preview_path = derived_dir / "preview.txt"
        analysis_path = derived_dir / "analysis.json"
        chunks = _write_chunks(derived_dir / "chunks", result.text)
        table_artifacts = _write_table_artifacts(staged, result)
        media_artifacts = _write_media_artifacts(
            staged,
            embedded_media_ocr=embedded_media_ocr,
            embedded_media_asr=embedded_media_asr,
        )
        payload = {
            "file_id": staged.file_id,
            "conversation_id": staged.conversation_id,
            "session_id": staged.session_id,
            "sha256": staged.sha256,
            "staged_suffix": Path(staged.staged_path).suffix.lower(),
            "source_path": staged.staged_path,
            "content_path": str(content_path),
            "analysis_path": str(analysis_path),
            "chunks": chunks,
            "table_artifacts": table_artifacts,
            "media_artifacts": media_artifacts,
            "result": asdict(result),
            "updated_at": utc_now_iso(),
        }
        _write_json(derived_dir / "parse_result.json", payload)
        analysis = _analysis_payload(staged, result, chunks, table_artifacts, media_artifacts)
        _write_json(analysis_path, analysis)
        content_path.write_text(_content_markdown(staged, result, analysis), encoding="utf-8")
        if result.text:
            preview_path.write_text(result.text, encoding="utf-8")
        self._update_manifest_parse_artifacts(
            staged,
            result,
            content_path,
            analysis_path,
            chunks,
            table_artifacts,
            media_artifacts,
        )

    def staged_from_manifest(self, manifest_path: str | Path) -> StagedFile:
        safe_manifest_path = _ensure_within(Path(manifest_path).resolve(), self.root)
        manifest = _read_json(safe_manifest_path, None)
        if not isinstance(manifest, dict):
            raise FileNotFoundError(f"missing manifest: {manifest_path}")
        workspace_dir = _ensure_within(Path(str(manifest["workspace_dir"])).resolve(), self.root)
        staged_path = _ensure_within(Path(str(manifest["staged_path"])).resolve(), workspace_dir)
        derived_dir = _ensure_within(Path(str(manifest["derived_dir"])).resolve(), workspace_dir)
        outputs_dir = _ensure_within(Path(str(manifest["outputs_dir"])).resolve(), workspace_dir)
        return StagedFile(
            file_id=str(manifest["file_id"]),
            conversation_id=str(manifest["conversation_id"]),
            session_id=str(manifest["session_id"]),
            original_name=str(manifest.get("original_name", "")),
            kind=str(manifest.get("kind", "file")),
            sha256=str(manifest["sha256"]),
            workspace_dir=str(workspace_dir),
            staged_path=str(staged_path),
            manifest_path=str(safe_manifest_path),
            derived_dir=str(derived_dir),
            outputs_dir=str(outputs_dir),
            source=str(manifest.get("source", "backend_event_attachment")),
        )

    def parse_or_get_cached(
        self,
        staged: StagedFile,
        parser: Any,
        *,
        embedded_media_ocr: OcrEngine | None = None,
        embedded_media_asr: AsrEngine | None = None,
    ) -> AttachmentParseResult:
        cached = self.read_parse_result(staged)
        if cached is not None:
            if _needs_table_artifact_refresh(staged, cached) or _needs_media_artifact_refresh(
                staged,
                embedded_media_ocr=embedded_media_ocr,
                embedded_media_asr=embedded_media_asr,
            ):
                self.write_parse_result(
                    staged,
                    cached,
                    embedded_media_ocr=embedded_media_ocr,
                    embedded_media_asr=embedded_media_asr,
                )
            return cached
        result = parser.parse(staged.staged_path)
        self.write_parse_result(
            staged,
            result,
            embedded_media_ocr=embedded_media_ocr,
            embedded_media_asr=embedded_media_asr,
        )
        return result

    def file_dir(self, conversation_id: str, session_id: str, file_id: str) -> Path:
        return self.root / _safe_segment(conversation_id) / _safe_segment(session_id) / _safe_segment(file_id)

    def list_session_files(self, conversation_id: str, session_id: str) -> list[dict[str, Any]]:
        index = _read_json(self._session_index_path(conversation_id, session_id), {})
        files = index.get("files", []) if isinstance(index, dict) else []
        return [dict(item) for item in files if isinstance(item, dict)]

    def libreoffice_convert_to_pdf(
        self,
        staged: StagedFile,
        runtime: LibreOfficeRuntime | None = None,
    ) -> WorkspaceOperationResult:
        output_dir = Path(staged.outputs_dir) / "libreoffice"
        try:
            output = (runtime or LibreOfficeRuntime()).convert_to_pdf(staged.staged_path, output_dir)
            return WorkspaceOperationResult("completed", "libreoffice.convert_to_pdf completed", str(output))
        except Exception as exc:
            return WorkspaceOperationResult("failed", "libreoffice.convert_to_pdf failed", error=f"{type(exc).__name__}: {exc}")

    def _session_index_path(self, conversation_id: str, session_id: str) -> Path:
        return self.root / _safe_segment(conversation_id) / _safe_segment(session_id) / "index.json"

    def _update_session_index(self, staged: StagedFile, manifest: dict[str, Any]) -> None:
        index_path = self._session_index_path(staged.conversation_id, staged.session_id)
        index = _read_json(index_path, {})
        files = index.get("files", []) if isinstance(index, dict) else []
        kept = [item for item in files if isinstance(item, dict) and item.get("file_id") != staged.file_id]
        kept.append(
            {
                "file_id": staged.file_id,
                "name": staged.original_name,
                "kind": staged.kind,
                "sha256": staged.sha256,
                "manifest_path": staged.manifest_path,
                "workspace_dir": staged.workspace_dir,
                "staged_path": staged.staged_path,
                "derived_dir": staged.derived_dir,
                "outputs_dir": staged.outputs_dir,
                "source_count": len(manifest.get("sources", [])),
                "updated_at": manifest.get("updated_at", utc_now_iso()),
            }
        )
        payload = {
            "conversation_id": staged.conversation_id,
            "session_id": staged.session_id,
            "files": sorted(kept, key=lambda item: str(item.get("updated_at", ""))),
            "updated_at": utc_now_iso(),
        }
        _write_json(index_path, payload)

    def _update_manifest_parse_artifacts(
        self,
        staged: StagedFile,
        result: AttachmentParseResult,
        content_path: Path,
        analysis_path: Path,
        chunks: list[dict[str, Any]],
        table_artifacts: dict[str, Any] | None = None,
        media_artifacts: dict[str, Any] | None = None,
    ) -> None:
        manifest_path = Path(staged.manifest_path)
        manifest = _read_json(manifest_path, {})
        if not isinstance(manifest, dict):
            return
        table_artifacts = table_artifacts if isinstance(table_artifacts, dict) else {}
        media_artifacts = media_artifacts if isinstance(media_artifacts, dict) else {}
        manifest["parse"] = {
            "status": result.status,
            "kind": result.kind,
            "summary": result.summary,
            "error": result.error,
            "content_path": str(content_path),
            "analysis_path": str(analysis_path),
            "chunks_dir": str(Path(staged.derived_dir) / "chunks"),
            "chunk_count": len(chunks),
            "chunks": chunks,
            "tables_dir": str(table_artifacts.get("tables_dir", "")),
            "table_index_path": str(table_artifacts.get("index_path", "")),
            "table_chunk_count": int(table_artifacts.get("chunk_count", 0) or 0),
            "table_chunks": [
                dict(item)
                for item in table_artifacts.get("chunks", [])
                if isinstance(item, dict)
            ],
            "media_dir": str(media_artifacts.get("media_dir", "")),
            "media_index_path": str(media_artifacts.get("index_path", "")),
            "media_extract_count": int(media_artifacts.get("extract_count", 0) or 0),
            "media_ocr_status": str(media_artifacts.get("ocr_status", "")),
            "media_ocr_dir": str(media_artifacts.get("ocr_dir", "")),
            "media_ocr_count": int(media_artifacts.get("ocr_count", 0) or 0),
            "media_ocr_error_count": int(media_artifacts.get("ocr_error_count", 0) or 0),
            "media_asr_status": str(media_artifacts.get("asr_status", "")),
            "media_asr_dir": str(media_artifacts.get("asr_dir", "")),
            "media_asr_count": int(media_artifacts.get("asr_count", 0) or 0),
            "media_asr_error_count": int(media_artifacts.get("asr_error_count", 0) or 0),
            "media_images": [
                dict(item)
                for item in media_artifacts.get("images", [])
                if isinstance(item, dict)
            ],
            "media_audio": [
                dict(item)
                for item in media_artifacts.get("audio", [])
                if isinstance(item, dict)
            ],
            "updated_at": utc_now_iso(),
        }
        manifest["updated_at"] = utc_now_iso()
        _write_json(manifest_path, manifest)


def _safe_filename(name: str, fallback_suffix: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip().strip(".")
    if not cleaned:
        cleaned = "attachment"
    if "." not in cleaned and fallback_suffix:
        cleaned += fallback_suffix
    return cleaned[:180]


def _analysis_payload(
    staged: StagedFile,
    result: AttachmentParseResult,
    chunks: list[dict[str, Any]],
    table_artifacts: dict[str, Any] | None = None,
    media_artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = result.text or ""
    suffix = Path(staged.staged_path).suffix.lower()
    table_artifacts = table_artifacts if isinstance(table_artifacts, dict) else {}
    media_artifacts = media_artifacts if isinstance(media_artifacts, dict) else {}
    media = _document_media_analysis(Path(staged.staged_path), suffix, media_artifacts)
    table_chunks = [
        dict(item)
        for item in table_artifacts.get("chunks", [])
        if isinstance(item, dict)
    ]
    return {
        "file_id": staged.file_id,
        "conversation_id": staged.conversation_id,
        "session_id": staged.session_id,
        "file_type": _file_type(suffix, result.kind),
        "suffix": suffix,
        "kind": result.kind,
        "status": result.status,
        "estimated_tokens": _estimate_tokens(text),
        "char_count": len(text),
        "line_count": len(text.splitlines()) if text else 0,
        "has_images": media["has_images"],
        "has_tables": result.kind == "spreadsheet" or bool(table_chunks),
        "has_audio": media["has_audio"],
        "media": media,
        "blocked_capabilities": _blocked_capabilities(media, media_artifacts),
        "media_status": str(media_artifacts.get("status", "")),
        "media_dir": str(media_artifacts.get("media_dir", "")),
        "media_index_path": str(media_artifacts.get("index_path", "")),
        "media_extract_count": int(media_artifacts.get("extract_count", 0) or 0),
        "media_ocr_status": str(media_artifacts.get("ocr_status", "")),
        "media_ocr_dir": str(media_artifacts.get("ocr_dir", "")),
        "media_ocr_count": int(media_artifacts.get("ocr_count", 0) or 0),
        "media_ocr_error_count": int(media_artifacts.get("ocr_error_count", 0) or 0),
        "media_asr_status": str(media_artifacts.get("asr_status", "")),
        "media_asr_dir": str(media_artifacts.get("asr_dir", "")),
        "media_asr_count": int(media_artifacts.get("asr_count", 0) or 0),
        "media_asr_error_count": int(media_artifacts.get("asr_error_count", 0) or 0),
        "media_images": [
            dict(item)
            for item in media_artifacts.get("images", [])
            if isinstance(item, dict)
        ],
        "media_audio": [
            dict(item)
            for item in media_artifacts.get("audio", [])
            if isinstance(item, dict)
        ],
        "media_error": str(media_artifacts.get("error", "")),
        "external_links": _extract_links(text),
        "chunked": len(chunks) > 1,
        "chunk_count": len(chunks),
        "chunks_dir": str(Path(staged.derived_dir) / "chunks") if chunks else "",
        "chunks": chunks,
        "table_status": str(table_artifacts.get("status", "")),
        "tables_dir": str(table_artifacts.get("tables_dir", "")),
        "table_index_path": str(table_artifacts.get("index_path", "")),
        "table_count": int(table_artifacts.get("table_count", 0) or 0),
        "table_row_count": int(table_artifacts.get("row_count", 0) or 0),
        "table_chunk_count": int(table_artifacts.get("chunk_count", 0) or 0),
        "tables": [
            dict(item)
            for item in table_artifacts.get("tables", [])
            if isinstance(item, dict)
        ],
        "table_chunks": table_chunks,
        "table_error": str(table_artifacts.get("error", "")),
        "created_at": utc_now_iso(),
    }


def _content_markdown(staged: StagedFile, result: AttachmentParseResult, analysis: dict[str, Any]) -> str:
    lines = [
        f"# Parsed Content: {staged.original_name}",
        "",
        f"- file_id: {staged.file_id}",
        f"- source: {staged.staged_path}",
        f"- status: {result.status}",
        f"- kind: {result.kind}",
        f"- estimated_tokens: {analysis.get('estimated_tokens', 0)}",
        "",
        "## Summary",
        "",
        result.summary or "",
        "",
        *_table_content_lines(analysis),
        *_media_content_lines(analysis),
        "## Text",
        "",
        result.text or "",
        "",
    ]
    if result.error:
        lines.extend(["## Error", "", result.error, ""])
    return "\n".join(lines)


def _table_content_lines(analysis: dict[str, Any]) -> list[str]:
    if not analysis.get("has_tables"):
        return []
    lines = [
        "## Tables",
        "",
        f"- status: {analysis.get('table_status', '')}",
        f"- table_count: {analysis.get('table_count', 0)}",
        f"- row_count: {analysis.get('table_row_count', 0)}",
        f"- chunk_count: {analysis.get('table_chunk_count', 0)}",
        f"- index: {analysis.get('table_index_path', '')}",
        f"- chunks_dir: {analysis.get('tables_dir', '')}",
    ]
    error = str(analysis.get("table_error", "")).strip()
    if error:
        lines.append(f"- error: {error}")
    chunks = analysis.get("table_chunks", [])
    if isinstance(chunks, list) and chunks:
        first = chunks[0]
        if isinstance(first, dict):
            lines.append(f"- first_chunk: {first.get('path', '')}")
    lines.append("")
    return lines


def _media_content_lines(analysis: dict[str, Any]) -> list[str]:
    media = analysis.get("media") if isinstance(analysis.get("media"), dict) else {}
    blocked = analysis.get("blocked_capabilities", [])
    has_media = bool(media.get("has_images") or media.get("has_audio"))
    extracted_count = int(analysis.get("media_extract_count", 0) or 0)
    error = str(analysis.get("media_error", "")).strip()
    if not (has_media or extracted_count or error or blocked):
        return []
    lines = [
        "## Embedded Media",
        "",
        f"- has_images: {bool(media.get('has_images', False))}",
        f"- image_count: {int(media.get('image_count', 0) or 0)}",
        f"- has_audio: {bool(media.get('has_audio', False))}",
        f"- audio_count: {int(media.get('audio_count', 0) or 0)}",
        f"- extract_status: {analysis.get('media_status', '')}",
        f"- extract_count: {analysis.get('media_extract_count', 0)}",
        f"- index: {analysis.get('media_index_path', '')}",
        f"- ocr_status: {analysis.get('media_ocr_status', '')}",
        f"- ocr_count: {analysis.get('media_ocr_count', 0)}",
        f"- ocr_dir: {analysis.get('media_ocr_dir', '')}",
        f"- asr_status: {analysis.get('media_asr_status', '')}",
        f"- asr_count: {analysis.get('media_asr_count', 0)}",
        f"- asr_dir: {analysis.get('media_asr_dir', '')}",
    ]
    if error:
        lines.append(f"- error: {error}")
    if blocked:
        lines.append(f"- blocked_capabilities: {', '.join(str(item) for item in blocked)}")
    samples = media.get("samples", [])
    if isinstance(samples, list) and samples:
        lines.append(f"- samples: {', '.join(str(item) for item in samples[:5])}")
    media_images = analysis.get("media_images", [])
    if isinstance(media_images, list) and media_images:
        first = media_images[0]
        if isinstance(first, dict):
            lines.append(f"- first_image: {first.get('path', '')}")
            if first.get("ocr_path"):
                lines.append(f"- first_image_ocr: {first.get('ocr_path', '')}")
    media_audio = analysis.get("media_audio", [])
    if isinstance(media_audio, list) and media_audio:
        first = media_audio[0]
        if isinstance(first, dict):
            lines.append(f"- first_audio: {first.get('path', '')}")
            if first.get("asr_path"):
                lines.append(f"- first_audio_asr: {first.get('asr_path', '')}")
    lines.append("")
    return lines


def _write_table_artifacts(staged: StagedFile, result: AttachmentParseResult) -> dict[str, Any]:
    suffix = Path(staged.staged_path).suffix.lower()
    tables_dir = Path(staged.derived_dir) / "tables"
    if result.kind != "spreadsheet" and suffix not in SPREADSHEET_SUFFIXES:
        if tables_dir.exists():
            shutil.rmtree(tables_dir)
        return {}
    return write_table_artifacts(staged.staged_path, tables_dir)


def _write_media_artifacts(
    staged: StagedFile,
    *,
    embedded_media_ocr: OcrEngine | None = None,
    embedded_media_asr: AsrEngine | None = None,
) -> dict[str, Any]:
    suffix = Path(staged.staged_path).suffix.lower()
    media_dir = Path(staged.derived_dir) / "media"
    if suffix == ".docx":
        return _extract_docx_media(
            Path(staged.staged_path),
            media_dir,
            embedded_media_ocr=embedded_media_ocr,
            embedded_media_asr=embedded_media_asr,
        )
    if suffix == ".pdf":
        return _extract_pdf_media(Path(staged.staged_path), media_dir, embedded_media_ocr=embedded_media_ocr)
    if suffix in AUDIO_SUFFIXES:
        return _write_standalone_audio_artifacts(
            Path(staged.staged_path),
            media_dir,
            embedded_media_asr=embedded_media_asr,
        )
    if media_dir.exists():
        shutil.rmtree(media_dir)
    return {}


def _extract_docx_media(
    path: Path,
    media_dir: Path,
    *,
    embedded_media_ocr: OcrEngine | None = None,
    embedded_media_asr: AsrEngine | None = None,
) -> dict[str, Any]:
    image_suffixes = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".emf", ".wmf"}
    audio_suffixes = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".wma"}
    media_dir.mkdir(parents=True, exist_ok=True)
    images_dir = media_dir / "images"
    audio_dir = media_dir / "audio"
    images_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)
    images: list[dict[str, Any]] = []
    audio: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(path) as docx:
            for name in docx.namelist():
                lowered = name.lower()
                if not lowered.startswith("word/media/"):
                    continue
                suffix = Path(lowered).suffix
                if suffix in image_suffixes:
                    output = images_dir / _safe_filename(Path(name).name, suffix)
                    output.write_bytes(docx.read(name))
                    images.append(_media_item(name, output, suffix))
                elif suffix in audio_suffixes:
                    output = audio_dir / _safe_filename(Path(name).name, suffix)
                    output.write_bytes(docx.read(name))
                    audio.append(_media_item(name, output, suffix))
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        payload = {
            "status": "failed",
            "media_dir": str(media_dir),
            "index_path": str(media_dir / "index.json"),
            "extract_count": 0,
            "images": [],
            "audio": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
        _write_json(media_dir / "index.json", payload)
        return payload

    ocr_payload = _write_media_ocr_artifacts(images, media_dir / "ocr", embedded_media_ocr)
    asr_payload = _write_media_asr_artifacts(audio, media_dir / "asr", embedded_media_asr)
    payload = {
        "status": "completed",
        "media_dir": str(media_dir),
        "index_path": str(media_dir / "index.json"),
        "extract_count": len(images) + len(audio),
        "images": ocr_payload["images"],
        "audio": asr_payload["audio"],
        "ocr_status": ocr_payload["status"],
        "ocr_dir": ocr_payload["ocr_dir"],
        "ocr_count": ocr_payload["ocr_count"],
        "ocr_error_count": ocr_payload["error_count"],
        "asr_status": asr_payload["status"],
        "asr_dir": asr_payload["asr_dir"],
        "asr_count": asr_payload["asr_count"],
        "asr_error_count": asr_payload["error_count"],
        "error": "",
    }
    _write_json(media_dir / "index.json", payload)
    return payload


def _extract_pdf_media(
    path: Path,
    media_dir: Path,
    *,
    embedded_media_ocr: OcrEngine | None = None,
) -> dict[str, Any]:
    media_dir.mkdir(parents=True, exist_ok=True)
    images_dir = media_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    images: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        for page_index, page in enumerate(reader.pages, start=1):
            for image_index, image in enumerate(getattr(page, "images", []) or [], start=1):
                suffix = Path(str(getattr(image, "name", ""))).suffix.lower() or ".bin"
                output = images_dir / _safe_filename(f"page_{page_index:04d}_image_{image_index:04d}{suffix}", suffix)
                data = getattr(image, "data", b"")
                if data:
                    output.write_bytes(data)
                    images.append(_media_item(f"page:{page_index}:image:{image_index}", output, suffix))
    except Exception as exc:
        errors.append(f"pypdf_image_extract:{type(exc).__name__}: {exc}")

    render_payload = _render_pdf_pages(path, media_dir / "pages")
    for item in render_payload.get("images", []):
        if isinstance(item, dict):
            images.append(dict(item))

    ocr_payload = _write_media_ocr_artifacts(images, media_dir / "ocr", embedded_media_ocr)
    payload = {
        "status": "completed" if images or not errors else "failed",
        "media_dir": str(media_dir),
        "index_path": str(media_dir / "index.json"),
        "extract_count": len(images),
        "images": ocr_payload["images"],
        "audio": [],
        "ocr_status": ocr_payload["status"],
        "ocr_dir": ocr_payload["ocr_dir"],
        "ocr_count": ocr_payload["ocr_count"],
        "ocr_error_count": ocr_payload["error_count"],
        "asr_status": "not_needed",
        "asr_dir": "",
        "asr_count": 0,
        "asr_error_count": 0,
        "page_render_status": render_payload.get("status", ""),
        "page_render_dir": render_payload.get("render_dir", ""),
        "page_render_count": render_payload.get("render_count", 0),
        "error": "; ".join(errors + ([str(render_payload.get("error", ""))] if render_payload.get("error") else [])),
    }
    _write_json(media_dir / "index.json", payload)
    return payload


def _render_pdf_pages(path: Path, render_dir: Path) -> dict[str, Any]:
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception:
        return {
            "status": "skipped_missing_pymupdf",
            "render_dir": "",
            "render_count": 0,
            "images": [],
            "error": "PyMuPDF is not installed; install it in vendor/ocr-python for scanned PDF page rendering",
        }
    try:
        document = fitz.open(str(path))
        render_dir.mkdir(parents=True, exist_ok=True)
        images: list[dict[str, Any]] = []
        for page_index in range(min(len(document), 20)):
            page = document[page_index]
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            output = render_dir / f"page_{page_index + 1:04d}.png"
            pix.save(str(output))
            images.append(_media_item(f"page:{page_index + 1}:render", output, ".png"))
        return {
            "status": "completed",
            "render_dir": str(render_dir),
            "render_count": len(images),
            "images": images,
            "error": "",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "render_dir": str(render_dir),
            "render_count": 0,
            "images": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def _media_item(source_name: str, output: Path, suffix: str) -> dict[str, Any]:
    return {
        "source_name": source_name,
        "name": output.name,
        "path": str(output),
        "suffix": suffix,
        "bytes": output.stat().st_size if output.exists() else 0,
    }


def _write_standalone_audio_artifacts(
    path: Path,
    media_dir: Path,
    *,
    embedded_media_asr: AsrEngine | None,
) -> dict[str, Any]:
    media_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = media_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    copied = audio_dir / _safe_filename(path.name, path.suffix)
    if not copied.exists() or _sha256_file(copied) != _sha256_file(path):
        shutil.copy2(path, copied)
    audio = [_media_item(path.name, copied, path.suffix.lower())]
    asr_payload = _write_media_asr_artifacts(audio, media_dir / "asr", embedded_media_asr)
    payload = {
        "status": "completed",
        "media_dir": str(media_dir),
        "index_path": str(media_dir / "index.json"),
        "extract_count": 1,
        "images": [],
        "audio": asr_payload["audio"],
        "ocr_status": "not_needed",
        "ocr_dir": "",
        "ocr_count": 0,
        "ocr_error_count": 0,
        "asr_status": asr_payload["status"],
        "asr_dir": asr_payload["asr_dir"],
        "asr_count": asr_payload["asr_count"],
        "asr_error_count": asr_payload["error_count"],
        "error": "",
    }
    _write_json(media_dir / "index.json", payload)
    return payload


def _write_media_asr_artifacts(
    audio: list[dict[str, Any]],
    asr_dir: Path,
    embedded_media_asr: AsrEngine | None,
) -> dict[str, Any]:
    if not audio:
        return {"status": "not_needed", "asr_dir": "", "asr_count": 0, "error_count": 0, "audio": audio}
    if embedded_media_asr is None:
        updated = [dict(item, asr_status="skipped_no_asr_engine") for item in audio]
        return {"status": "skipped_no_asr_engine", "asr_dir": "", "asr_count": 0, "error_count": 0, "audio": updated}
    asr_dir.mkdir(parents=True, exist_ok=True)
    updated: list[dict[str, Any]] = []
    asr_count = 0
    error_count = 0
    for index, item in enumerate(audio, start=1):
        audio_path = Path(str(item.get("path", "")))
        output_path = asr_dir / f"{index:04d}_{Path(str(item.get('name', 'audio'))).stem}.md"
        current = dict(item)
        transcript = embedded_media_asr.transcribe(audio_path)
        output_path.write_text(_media_asr_markdown(current, transcript.status, text=transcript.text, error=transcript.error), encoding="utf-8")
        current.update(
            {
                "asr_status": transcript.status,
                "asr_path": str(output_path),
                "asr_backend": transcript.backend,
                "asr_model": transcript.model,
                "asr_text": _compact(transcript.text, 2000),
                "asr_error": transcript.error,
            }
        )
        if transcript.status == "transcribed":
            asr_count += 1
        elif transcript.status in {"failed", "blocked"}:
            error_count += 1
        updated.append(current)
    if error_count and asr_count:
        status = "partial"
    elif error_count:
        status = "failed"
    else:
        status = "completed"
    return {
        "status": status,
        "asr_dir": str(asr_dir),
        "asr_count": asr_count,
        "error_count": error_count,
        "audio": updated,
    }


def _media_asr_markdown(item: dict[str, Any], status: str, *, text: str = "", error: str = "") -> str:
    lines = [
        f"# Audio ASR: {item.get('name', '')}",
        "",
        f"- source_name: {item.get('source_name', '')}",
        f"- audio_path: {item.get('path', '')}",
        f"- status: {status}",
        "",
        "## Text",
        "",
        text,
        "",
    ]
    if error:
        lines.extend(["## Error", "", error, ""])
    return "\n".join(lines)


def _write_media_ocr_artifacts(
    images: list[dict[str, Any]],
    ocr_dir: Path,
    embedded_media_ocr: OcrEngine | None,
) -> dict[str, Any]:
    if not images:
        return {"status": "not_needed", "ocr_dir": "", "ocr_count": 0, "error_count": 0, "images": images}
    if embedded_media_ocr is None:
        return {"status": "skipped_no_ocr_engine", "ocr_dir": "", "ocr_count": 0, "error_count": 0, "images": images}
    ocr_dir.mkdir(parents=True, exist_ok=True)
    updated: list[dict[str, Any]] = []
    ocr_count = 0
    error_count = 0
    for index, item in enumerate(images, start=1):
        image_path = Path(str(item.get("path", "")))
        output_path = ocr_dir / f"{index:04d}_{Path(str(item.get('name', 'image'))).stem}.md"
        current = dict(item)
        try:
            text = embedded_media_ocr.read_text(image_path)
            status = "parsed" if text.strip() else "empty"
            output_path.write_text(_media_ocr_markdown(current, status, text=text), encoding="utf-8")
            current.update(
                {
                    "ocr_status": status,
                    "ocr_path": str(output_path),
                    "ocr_text": _compact(text, 2000),
                    "ocr_char_count": len(text),
                }
            )
            ocr_count += 1
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            output_path.write_text(_media_ocr_markdown(current, "failed", error=error), encoding="utf-8")
            current.update({"ocr_status": "failed", "ocr_path": str(output_path), "ocr_error": error})
            error_count += 1
        updated.append(current)
    if error_count and ocr_count:
        status = "partial"
    elif error_count:
        status = "failed"
    else:
        status = "completed"
    return {
        "status": status,
        "ocr_dir": str(ocr_dir),
        "ocr_count": ocr_count,
        "error_count": error_count,
        "images": updated,
    }


def _media_ocr_markdown(item: dict[str, Any], status: str, *, text: str = "", error: str = "") -> str:
    lines = [
        f"# Embedded Image OCR: {item.get('name', '')}",
        "",
        f"- source_name: {item.get('source_name', '')}",
        f"- image_path: {item.get('path', '')}",
        f"- status: {status}",
        "",
        "## Text",
        "",
        text,
        "",
    ]
    if error:
        lines.extend(["## Error", "", error, ""])
    return "\n".join(lines)


def _needs_table_artifact_refresh(staged: StagedFile, result: AttachmentParseResult) -> bool:
    suffix = Path(staged.staged_path).suffix.lower()
    if result.kind != "spreadsheet" and suffix not in SPREADSHEET_SUFFIXES:
        return False
    analysis = _read_json(Path(staged.derived_dir) / "analysis.json", {})
    if not isinstance(analysis, dict):
        return True
    return not analysis.get("table_index_path") or int(analysis.get("table_chunk_count", 0) or 0) <= 0


def _needs_media_artifact_refresh(
    staged: StagedFile,
    *,
    embedded_media_ocr: OcrEngine | None = None,
    embedded_media_asr: AsrEngine | None = None,
) -> bool:
    suffix = Path(staged.staged_path).suffix.lower()
    if suffix not in {".docx", ".pdf", *AUDIO_SUFFIXES}:
        return False
    index = _read_json(Path(staged.derived_dir) / "media" / "index.json", None)
    if not isinstance(index, dict):
        return True
    images = index.get("images", [])
    if not isinstance(images, list):
        return True
    if embedded_media_ocr is not None and any(isinstance(item, dict) and not item.get("ocr_status") for item in images):
        return True
    audio = index.get("audio", [])
    if embedded_media_asr is not None and isinstance(audio, list):
        return any(isinstance(item, dict) and not item.get("asr_status") for item in audio)
    return False


def _document_media_analysis(path: Path, suffix: str, media_artifacts: dict[str, Any] | None = None) -> dict[str, Any]:
    if suffix == ".docx":
        return _docx_media_analysis(path, media_artifacts)
    if suffix == ".pdf":
        return _pdf_media_analysis(path)
    return {
        "has_images": suffix in IMAGE_SUFFIXES,
        "has_audio": suffix in AUDIO_SUFFIXES,
        "image_count": 1 if suffix in IMAGE_SUFFIXES else 0,
        "audio_count": 1 if suffix in AUDIO_SUFFIXES else 0,
        "samples": [],
        "embedded": False,
        "detection": "suffix",
    }


def _docx_media_analysis(path: Path, media_artifacts: dict[str, Any] | None = None) -> dict[str, Any]:
    image_suffixes = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".emf", ".wmf"}
    audio_suffixes = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".wma"}
    images: list[str] = []
    audio: list[str] = []
    extracted_images = [
        str(item.get("path", ""))
        for item in (media_artifacts or {}).get("images", [])
        if isinstance(item, dict) and item.get("path")
    ]
    extracted_audio = [
        str(item.get("path", ""))
        for item in (media_artifacts or {}).get("audio", [])
        if isinstance(item, dict) and item.get("path")
    ]
    try:
        with zipfile.ZipFile(path) as docx:
            for name in docx.namelist():
                lowered = name.lower()
                if not lowered.startswith("word/media/"):
                    continue
                suffix = Path(lowered).suffix
                if suffix in image_suffixes:
                    images.append(name)
                elif suffix in audio_suffixes:
                    audio.append(name)
    except (OSError, zipfile.BadZipFile):
        return {
            "has_images": False,
            "has_audio": False,
            "image_count": 0,
            "audio_count": 0,
            "samples": [],
            "embedded": True,
            "detection": "docx_zip_failed",
        }
    return {
        "has_images": bool(images),
        "has_audio": bool(audio),
        "image_count": len(images),
        "audio_count": len(audio),
        "samples": [*images[:5], *audio[:5]][:5],
        "extracted_image_count": len(extracted_images),
        "extracted_audio_count": len(extracted_audio),
        "extracted_samples": [*extracted_images[:5], *extracted_audio[:5]][:5],
        "embedded": True,
        "detection": "docx_zip_media",
    }


def _pdf_media_analysis(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError:
        data = b""
    image_hits = sum(data.count(token) for token in [b"/Subtype /Image", b"/Image"])
    audio_hits = sum(data.lower().count(token) for token in [b"/sound", b"/movie", b"/richmedia"])
    return {
        "has_images": image_hits > 0,
        "has_audio": audio_hits > 0,
        "image_count": int(image_hits),
        "audio_count": int(audio_hits),
        "samples": [],
        "embedded": True,
        "detection": "pdf_resource_heuristic",
    }


def _blocked_capabilities(media: dict[str, Any], media_artifacts: dict[str, Any] | None = None) -> list[str]:
    artifacts = media_artifacts if isinstance(media_artifacts, dict) else {}
    blocked: list[str] = []
    ocr_count = int(artifacts.get("ocr_count", 0) or 0)
    asr_count = int(artifacts.get("asr_count", 0) or 0)
    if media.get("embedded") and media.get("has_images") and ocr_count <= 0:
        blocked.append("embedded_image_extraction_and_ocr")
    if media.get("has_audio") and asr_count <= 0:
        blocked.append("embedded_audio_extraction_and_asr")
    return blocked


def _write_chunks(chunks_dir: Path, text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    chunks = _split_text(text, CHUNK_CHAR_TARGET)
    if not chunks:
        return []
    chunks_dir.mkdir(parents=True, exist_ok=True)
    refs: list[dict[str, Any]] = []
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        path = chunks_dir / f"chunk_{index:04d}.md"
        token_estimate = _estimate_tokens(chunk)
        path.write_text(
            "\n".join(
                [
                    f"# Chunk {index}/{total}",
                    "",
                    f"- token_estimate: {token_estimate}",
                    "",
                    chunk,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        refs.append(
            {
                "index": index,
                "path": str(path),
                "token_estimate": token_estimate,
                "char_count": len(chunk),
            }
        )
    return refs


def _split_text(text: str, max_chars: int) -> list[str]:
    normalized = text.strip()
    if not normalized:
        return []
    if len(normalized) <= max_chars:
        return [normalized]
    paragraphs = re.split(r"\n\s*\n", normalized)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > max_chars:
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            chunks.extend(_split_long_text(paragraph, max_chars))
            continue
        added_len = len(paragraph) + (2 if current else 0)
        if current and current_len + added_len > max_chars:
            chunks.append("\n\n".join(current))
            current = [paragraph]
            current_len = len(paragraph)
        else:
            current.append(paragraph)
            current_len += added_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _split_long_text(text: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
            if boundary > start + max_chars // 2:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
    return chunks


def _file_type(suffix: str, kind: str) -> str:
    if suffix in {".xlsx", ".xlsm", ".csv"}:
        return "spreadsheet"
    if suffix in {".md", ".txt"}:
        return "text"
    if suffix == ".docx":
        return "word"
    if suffix == ".pdf":
        return "pdf"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    return kind or "file"


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _extract_links(text: str) -> list[str]:
    return re.findall(r"https?://[^\s]+", text, flags=re.IGNORECASE)


def _compact(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "default"


def _merge_sources(existing: Any, new_source: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [dict(item) for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
    key = (
        str(new_source.get("original_path", "")),
        str(new_source.get("original_name", "")),
        str(new_source.get("staged_path", "")),
        str(new_source.get("source", "")),
    )
    deduped = [
        item
        for item in sources
        if (
            str(item.get("original_path", "")),
            str(item.get("original_name", "")),
            str(item.get("staged_path", "")),
            str(item.get("source", "")),
        )
        != key
    ]
    deduped.append(new_source)
    return deduped


def _ensure_within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    resolved_root = root.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise PermissionError(f"path outside workspace root: {resolved}")
    return resolved


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)

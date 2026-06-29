from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.tools.document.libreoffice import LibreOfficeRuntime
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import AttachmentParseResult


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

    def write_parse_result(self, staged: StagedFile, result: AttachmentParseResult) -> None:
        derived_dir = Path(staged.derived_dir)
        derived_dir.mkdir(parents=True, exist_ok=True)
        content_path = derived_dir / "content.md"
        preview_path = derived_dir / "preview.txt"
        analysis_path = derived_dir / "analysis.json"
        chunks = _write_chunks(derived_dir / "chunks", result.text)
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
            "result": asdict(result),
            "updated_at": utc_now_iso(),
        }
        _write_json(derived_dir / "parse_result.json", payload)
        analysis = _analysis_payload(staged, result, chunks)
        _write_json(analysis_path, analysis)
        content_path.write_text(_content_markdown(staged, result, analysis), encoding="utf-8")
        if result.text:
            preview_path.write_text(result.text, encoding="utf-8")
        self._update_manifest_parse_artifacts(staged, result, content_path, analysis_path, chunks)

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

    def parse_or_get_cached(self, staged: StagedFile, parser: Any) -> AttachmentParseResult:
        cached = self.read_parse_result(staged)
        if cached is not None:
            return cached
        result = parser.parse(staged.staged_path)
        self.write_parse_result(staged, result)
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
    ) -> None:
        manifest_path = Path(staged.manifest_path)
        manifest = _read_json(manifest_path, {})
        if not isinstance(manifest, dict):
            return
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


def _analysis_payload(staged: StagedFile, result: AttachmentParseResult, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    text = result.text or ""
    suffix = Path(staged.staged_path).suffix.lower()
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
        "has_images": False,
        "has_tables": result.kind == "spreadsheet",
        "has_audio": False,
        "external_links": _extract_links(text),
        "chunked": len(chunks) > 1,
        "chunk_count": len(chunks),
        "chunks_dir": str(Path(staged.derived_dir) / "chunks") if chunks else "",
        "chunks": chunks,
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
        "## Text",
        "",
        result.text or "",
        "",
    ]
    if result.error:
        lines.extend(["## Error", "", result.error, ""])
    return "\n".join(lines)


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
    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}:
        return "image"
    return kind or "file"


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _extract_links(text: str) -> list[str]:
    return re.findall(r"https?://[^\s]+", text, flags=re.IGNORECASE)


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

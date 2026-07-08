from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.domain.models import ToolCallRequest, ToolCallResult
from app.personal_wechat_bot.tools.base import ToolManifest
from app.personal_wechat_bot.workspace.file_visibility import count_urls, redact_file_internal_urls


class FileReadTool:
    manifest = ToolManifest(
        name="file.read",
        description="Read a parsed file workspace artifact by file_id.",
    )

    def __init__(self, data_root: str | Path, *, max_chars: int = 12000):
        self.data_root = Path(data_root).resolve()
        self.workspace_root = (self.data_root / "file_workspace").resolve()
        self.max_chars = max_chars

    def run(self, request: ToolCallRequest) -> ToolCallResult:
        file_id = str(request.arguments.get("file_id") or request.arguments.get("id") or "").strip()
        if not file_id:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="blocked",
                summary="file_id is required",
                error="missing_file_id",
            )
        manifest_path = self._find_manifest(file_id)
        if manifest_path is None:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="failed",
                summary=f"file_id not found: {file_id}",
                error="file_not_found",
            )
        manifest = _read_json(manifest_path, {})
        if not isinstance(manifest, dict):
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="failed",
                summary=f"invalid manifest for file_id: {file_id}",
                error="invalid_manifest",
            )

        artifact = str(request.arguments.get("artifact") or request.arguments.get("part") or "content").strip()
        chunk_index = _int_value(request.arguments.get("chunk") or request.arguments.get("chunk_index"), 0)
        target = self._artifact_path(manifest, artifact=artifact, chunk_index=chunk_index)
        if target is None:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="failed",
                summary=f"artifact not found for file_id={file_id}",
                error="artifact_not_found",
                payload={"file_id": file_id, "artifact": artifact, "chunk_index": chunk_index},
            )
        try:
            safe_target = _ensure_within(target.resolve(), self.workspace_root)
        except PermissionError as exc:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="blocked",
                summary="file artifact is outside workspace",
                error=str(exc),
            )
        pin_internal_urls = _bool_value(
            request.arguments.get("pin_internal_urls")
            or request.arguments.get("include_internal_urls")
            or request.arguments.get("expose_internal_urls")
        )
        text = _read_text_artifact(safe_target, max_chars=self.max_chars)
        if text is None:
            if artifact == "original":
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=self.manifest.name,
                    status="completed",
                    summary="Original binary artifact is available as an output ref; use parsed artifacts for text.",
                    output_refs=[str(safe_target)],
                    payload={
                        "file_id": file_id,
                        "artifact": artifact,
                        "path": str(safe_target),
                        "text": "",
                        "binary": True,
                        "multimodal_ref_available": True,
                        "readable_alternatives": ["content", "full_text", "analysis", "chunk", "ocr_table", "table"],
                    },
                )
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="blocked",
                summary="artifact is not readable as text; use content, full_text, analysis, or chunk",
                error="non_text_artifact",
                output_refs=[str(safe_target)],
                payload={"file_id": file_id, "artifact": artifact, "path": str(safe_target)},
            )
        original_url_count = count_urls(text)
        internal_urls_hidden = False
        if not pin_internal_urls and original_url_count:
            text = redact_file_internal_urls(text)
            internal_urls_hidden = True
        summary = f"Read file artifact file_id={file_id} artifact={artifact}"
        if chunk_index:
            summary += f" chunk={chunk_index}"
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=self.manifest.name,
            status="completed",
            summary=_compact(text, 900) or summary,
            output_refs=[str(safe_target)],
            payload={
                "file_id": file_id,
                "artifact": artifact,
                "chunk_index": chunk_index,
                "path": str(safe_target),
                "text": text,
                "truncated": len(text) >= self.max_chars,
                "pin_internal_urls": pin_internal_urls,
                "internal_urls_hidden": internal_urls_hidden,
                "internal_url_count": original_url_count,
            },
        )

    def _find_manifest(self, file_id: str) -> Path | None:
        if not self.workspace_root.exists():
            return None
        for manifest_path in self.workspace_root.rglob("manifest.json"):
            manifest = _read_json(manifest_path, {})
            if isinstance(manifest, dict) and str(manifest.get("file_id") or "") == file_id:
                return manifest_path
        return None

    def _artifact_path(self, manifest: dict[str, Any], *, artifact: str, chunk_index: int) -> Path | None:
        derived_dir = Path(str(manifest.get("derived_dir") or ""))
        analysis = _read_json(derived_dir / "analysis.json", {})
        if artifact == "chunk" or (
            chunk_index > 0 and artifact not in {"ocr_table", "ocr_tables", "ocr_layout", "table", "table_chunk"}
        ):
            index = chunk_index if chunk_index > 0 else 1
            chunks = analysis.get("chunks", []) if isinstance(analysis, dict) else []
            for item in chunks if isinstance(chunks, list) else []:
                if isinstance(item, dict) and int(item.get("index", 0) or 0) == index:
                    path = Path(str(item.get("path") or ""))
                    return path if path.is_file() else None
            fallback = derived_dir / "chunks" / f"chunk_{index:04d}.md"
            return fallback if fallback.is_file() else None
        if artifact in {"ocr_table", "ocr_tables", "ocr_layout"}:
            index = chunk_index if chunk_index > 0 else 1
            chunks = analysis.get("ocr_table_chunks", []) if isinstance(analysis, dict) else []
            for item in chunks if isinstance(chunks, list) else []:
                if isinstance(item, dict) and int(item.get("chunk_index", 0) or 0) == index:
                    path = Path(str(item.get("path") or ""))
                    return path if path.is_file() else None
            fallback = derived_dir / "ocr_tables" / f"ocr_source_0001_chunk_{index:04d}.json"
            return fallback if fallback.is_file() else None
        if artifact in {"table", "table_chunk"}:
            index = chunk_index if chunk_index > 0 else 1
            chunks = analysis.get("table_chunks", []) if isinstance(analysis, dict) else []
            for item in chunks if isinstance(chunks, list) else []:
                if isinstance(item, dict) and int(item.get("chunk_index", 0) or 0) == index:
                    path = Path(str(item.get("path") or ""))
                    return path if path.is_file() else None
        if artifact in {"content", "summary"}:
            return derived_dir / "content.md"
        if artifact in {"full", "full_text", "text"}:
            return derived_dir / "full_text.md"
        if artifact == "analysis":
            return derived_dir / "analysis.json"
        if artifact in {"parse", "parse_result"}:
            return derived_dir / "parse_result.json"
        if artifact == "original":
            return Path(str(manifest.get("staged_path") or ""))
        return None


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.is_file():
            return default
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def _read_text_artifact(path: Path, *, max_chars: int) -> str | None:
    suffix = path.suffix.lower()
    if suffix not in {".md", ".txt", ".json", ".csv", ".tsv"}:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _compact(text, max_chars)


def _ensure_within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    resolved_root = root.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise PermissionError(f"path outside workspace root: {resolved}")
    return resolved


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on", "include", "pin", "expose"}


def _compact(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."

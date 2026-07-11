from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from app.personal_wechat_bot.conversation.segment import resolve_segment
from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.runtime.process_lock import (
    pid_lock_file_is_stale,
    scoped_process_lock_path,
    short_process_lock,
)
from app.personal_wechat_bot.tasks.manager import TaskStatusStore
from app.personal_wechat_bot.tools.document.libreoffice import LibreOfficeRuntime
from app.personal_wechat_bot.vision.ocr import OcrEngine, OcrItem, ocr_rows_payload
from app.personal_wechat_bot.voice.asr import AsrEngine
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import (
    AUDIO_SUFFIXES,
    IMAGE_SUFFIXES,
    AttachmentParseResult,
)
from app.personal_wechat_bot.workspace.file_visibility import count_urls, redact_file_internal_urls
from app.personal_wechat_bot.workspace.table_artifacts import SPREADSHEET_SUFFIXES, write_table_artifacts


CHUNK_TOKEN_TARGET = 1800
CHUNK_CHAR_TARGET = 6000
PREVIEW_CHAR_TARGET = 8000
OCR_ROWS_PER_CHUNK = 100
OCR_PARSE_CACHE_VERSION = 3
MEDIA_OCR_CACHE_VERSION = 3
MEDIA_ASR_CACHE_VERSION = 2
AI_ANALYSIS_CACHE_VERSION = 2
AI_ANALYSIS_PENDING_RETRY_SECONDS = 30 * 60
_FILE_ANALYSIS_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="file-analysis")


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
    blob_path: str = ""
    storage_mode: str = ""


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

    def __init__(self, root: str | Path, *, analyzer: Any = None, analysis_async: bool = False):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # Optional LLM-backed analyzer (workspace.file_analysis.FileAnalyzer).
        # When absent, analysis.json holds mechanical metadata only. Set once in
        # bootstrap so the driver + pipeline that share this instance all use it.
        self.analyzer = analyzer
        self.analysis_async = analysis_async
        # Cache conversation_id -> stable directory segment. The chat title is
        # only a bootstrap hint before channel metadata exists.
        self._segment_cache: dict[str, str] = {}

    def _root_lifecycle_lock(self, *, timeout_seconds: float = 120.0):
        return short_process_lock(
            scoped_process_lock_path(
                self.root.parent,
                "file-workspace-root",
                str(self.root),
            ),
            timeout_seconds=timeout_seconds,
            stale_after_seconds=120.0,
            timeout_label="file workspace root lifecycle lock",
        )

    def _conversation_lifecycle_lock(
        self,
        workspace_path: str | Path,
        *,
        timeout_seconds: float = 120.0,
    ):
        resolved = _ensure_within(Path(workspace_path).resolve(), self.root)
        relative = resolved.relative_to(self.root)
        if not relative.parts:
            raise ValueError("file workspace conversation path is required")
        conversation_dir = self.root / relative.parts[0]
        return short_process_lock(
            scoped_process_lock_path(
                self.root.parent,
                "file-workspace-conversation",
                str(conversation_dir),
            ),
            timeout_seconds=timeout_seconds,
            stale_after_seconds=120.0,
            timeout_label="file workspace conversation lifecycle lock",
        )

    def stage_file(
        self,
        source_path: str | Path,
        *,
        conversation_id: str,
        session_id: str,
        original_name: str = "",
        kind: str = "file",
        source: str = "backend_event_attachment",
        chat_title: str = "",
    ) -> StagedFile:
        self._remember_segment(conversation_id, chat_title)
        source_file = Path(source_path).resolve()
        digest = _sha256_file(source_file)
        file_id = digest[:24]
        display_name = original_name or source_file.name
        workspace_dir = self.file_dir(conversation_id, session_id, file_id)
        original_dir = workspace_dir / "original"
        derived_dir = workspace_dir / "derived"
        outputs_dir = workspace_dir / "outputs"
        manifest_path = workspace_dir / "manifest.json"
        # Cleanup or channel deletion must never remove a workspace while its
        # blob, manifest, and session-index projection are being committed.
        # These locks live beside runtime state, outside the deletable tree.
        with self._root_lifecycle_lock():
            with self._conversation_lifecycle_lock(workspace_dir):
                for child in [original_dir, derived_dir, outputs_dir]:
                    child.mkdir(parents=True, exist_ok=True)
                staged_path = original_dir / _safe_filename(display_name, source_file.suffix)
                blob_path = _ensure_content_blob(self.root, source_file, digest)
                storage_mode = "existing"
                if not staged_path.exists() or _sha256_file(staged_path) != digest:
                    storage_mode = _materialize_content_blob(blob_path, staged_path)
                elif _same_file(blob_path, staged_path):
                    storage_mode = "hardlink"
                    _set_read_only(staged_path)
                else:
                    storage_mode = "copy"
                    _set_read_only(staged_path)

                previous = _read_json(manifest_path, {})
                source_record = {
                    "original_path": str(source_file),
                    "original_name": display_name,
                    "staged_path": str(staged_path),
                    "blob_path": str(blob_path),
                    "storage_mode": storage_mode,
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
                    "blob_path": str(blob_path),
                    "storage_mode": storage_mode,
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
                    blob_path=str(blob_path),
                    storage_mode=storage_mode,
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
            context_text=str(result.get("context_text", "")),
            metadata=dict(result.get("metadata", {})) if isinstance(result.get("metadata"), dict) else {},
        )

    def write_parse_result(
        self,
        staged: StagedFile,
        result: AttachmentParseResult,
        *,
        embedded_media_ocr: OcrEngine | None = None,
        embedded_media_asr: AsrEngine | None = None,
    ) -> None:
        with self._conversation_lifecycle_lock(staged.workspace_dir):
            self._require_staged_manifest(staged)
            self._write_parse_result_unlocked(
                staged,
                result,
                embedded_media_ocr=embedded_media_ocr,
                embedded_media_asr=embedded_media_asr,
            )

    def _write_parse_result_unlocked(
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
        full_text_path = derived_dir / "full_text.md"
        analysis_path = derived_dir / "analysis.json"
        table_artifacts = _write_table_artifacts(staged, result)
        media_artifacts = _write_media_artifacts(
            staged,
            result=result,
            embedded_media_ocr=embedded_media_ocr,
            embedded_media_asr=embedded_media_asr,
        )
        ocr_table_artifacts = _write_ocr_table_artifacts(staged, result, media_artifacts)
        chunk_source_text = _readable_text_for_file(result, media_artifacts)
        chunks = _write_chunks(derived_dir / "chunks", chunk_source_text)
        ai_analysis = self._initial_file_analysis(staged, result, media_artifacts)
        payload = {
            "file_id": staged.file_id,
            "conversation_id": staged.conversation_id,
            "session_id": staged.session_id,
            "sha256": staged.sha256,
            "staged_suffix": Path(staged.staged_path).suffix.lower(),
            "source_path": staged.staged_path,
            "content_path": str(content_path),
            "full_text_path": str(full_text_path),
            "analysis_path": str(analysis_path),
            "chunks": chunks,
            "table_artifacts": table_artifacts,
            "ocr_table_artifacts": ocr_table_artifacts,
            "media_artifacts": media_artifacts,
            "ai_analysis": ai_analysis,
            "result": asdict(result),
            "updated_at": utc_now_iso(),
        }
        _write_json(derived_dir / "parse_result.json", payload)
        analysis = _analysis_payload(staged, result, chunks, table_artifacts, media_artifacts, ai_analysis, ocr_table_artifacts)
        _write_json(analysis_path, analysis)
        content_path.write_text(_content_markdown(staged, result, analysis), encoding="utf-8")
        if chunk_source_text:
            full_text_path.write_text(_full_text_markdown(staged, result, chunk_source_text), encoding="utf-8")
        elif full_text_path.exists():
            full_text_path.unlink()
        preview_text = _result_context_text(result) or _compact(chunk_source_text, PREVIEW_CHAR_TARGET)
        if preview_text:
            preview_path.write_text(preview_text, encoding="utf-8")
        elif preview_path.exists():
            preview_path.unlink()
        self._update_manifest_parse_artifacts(
            staged,
            result,
            content_path,
            full_text_path,
            analysis_path,
            chunks,
            table_artifacts,
            media_artifacts,
            ocr_table_artifacts,
            analysis=analysis,
        )
        self._record_file_parse_task(staged, result, chunks)
        if self._should_schedule_file_analysis(ai_analysis):
            self._record_file_ai_task(staged, result, status="queued", progress=0, phase="等待文件 AI 分析")
            _FILE_ANALYSIS_EXECUTOR.submit(
                self._finish_async_file_analysis,
                staged,
                result,
                chunks,
                table_artifacts,
                media_artifacts,
                ocr_table_artifacts,
                content_path,
                analysis_path,
            )

    def _run_file_analysis(
        self,
        staged: StagedFile,
        result: AttachmentParseResult,
        media_artifacts: dict[str, Any],
    ) -> dict[str, Any]:
        """Produce (or reuse) the LLM analysis for this file.

        Cached in parse_result.json keyed on sha256, so a re-render (e.g. media
        artifact refresh) does not re-invoke the LLM. Returns a plain dict so the
        result serializes cleanly; ``{"status": "disabled"}`` when no analyzer is
        wired in. Never raises into the parse path.
        """
        if self.analyzer is None:
            return {"status": "disabled"}
        base_text = (result.text or "") if result.status == "parsed" else ""
        text = redact_file_internal_urls(_dedup_join_text(_augment_analysis_parts(base_text, media_artifacts)))
        cache_meta = _ai_analysis_cache_meta(staged, result, text)
        cached = _read_json(Path(staged.derived_dir) / "parse_result.json", None)
        if isinstance(cached, dict) and cached.get("sha256") == staged.sha256:
            prior = cached.get("ai_analysis")
            if isinstance(prior, dict) and _valid_cached_ai_analysis(prior, cache_meta):
                return prior
        # result.text is a placeholder (not real content) whenever the parse did
        # not actually succeed — empty/skipped/failed images, audio, and
        # unsupported types all emit "[附件占位符]…". Only feed real text to the
        # analyzer: use result.text only when status == "parsed"; otherwise rely
        # on _augment_analysis_text to supply status-filtered OCR/ASR content.
        extra = {
            "has_tables": result.kind == "spreadsheet",
            "media_extract_count": int(media_artifacts.get("extract_count", 0) or 0),
        }
        try:
            analysis = self.analyzer.analyze(
                name=staged.original_name,
                kind=result.kind,
                text=text,
                extra=extra,
            )
        except Exception as exc:  # defense in depth; analyzer already guards
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        to_dict = getattr(analysis, "to_dict", None)
        payload = to_dict() if callable(to_dict) else dict(analysis)
        return {**payload, **cache_meta, "cache_status": "miss"}

    def _initial_file_analysis(
        self,
        staged: StagedFile,
        result: AttachmentParseResult,
        media_artifacts: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.analysis_async:
            return self._run_file_analysis(staged, result, media_artifacts)
        if self.analyzer is None:
            return {"status": "disabled"}
        base_text = (result.text or "") if result.status == "parsed" else ""
        text = redact_file_internal_urls(_dedup_join_text(_augment_analysis_parts(base_text, media_artifacts)))
        cache_meta = _ai_analysis_cache_meta(staged, result, text)
        cached = _read_json(Path(staged.derived_dir) / "parse_result.json", None)
        if isinstance(cached, dict) and cached.get("sha256") == staged.sha256:
            prior = cached.get("ai_analysis")
            if isinstance(prior, dict) and _valid_cached_ai_analysis(prior, cache_meta):
                return prior
            if isinstance(prior, dict) and _pending_ai_analysis_still_fresh(prior, cache_meta):
                return prior
        return {
            "status": "pending",
            "summary": "file summary is being generated in the background",
            "scheduled_at": utc_now_iso(),
            **cache_meta,
        }
        return {"status": "pending", "summary": "文件总结正在后台生成"}

    def _should_schedule_file_analysis(self, ai_analysis: dict[str, Any]) -> bool:
        return bool(self.analysis_async and self.analyzer is not None and ai_analysis.get("status") == "pending")

    def _finish_async_file_analysis(
        self,
        staged: StagedFile,
        result: AttachmentParseResult,
        chunks: list[dict[str, Any]],
        table_artifacts: dict[str, Any],
        media_artifacts: dict[str, Any],
        ocr_table_artifacts: dict[str, Any],
        content_path: Path,
        analysis_path: Path,
    ) -> None:
        task_id = _file_ai_task_id(staged)
        self._transition_file_task(task_id, "start", {"progress": 20, "phase": "正在生成文件 AI 分析"})
        ai_analysis = self._run_file_analysis(staged, result, media_artifacts)
        analysis = _analysis_payload(staged, result, chunks, table_artifacts, media_artifacts, ai_analysis, ocr_table_artifacts)
        try:
            with self._conversation_lifecycle_lock(staged.workspace_dir):
                self._require_staged_manifest(staged)
                _write_json(analysis_path, analysis)
                content_path.write_text(_content_markdown(staged, result, analysis), encoding="utf-8")
                parse_payload = _read_json(Path(staged.derived_dir) / "parse_result.json", {})
                if isinstance(parse_payload, dict):
                    parse_payload["ai_analysis"] = ai_analysis
                    parse_payload["updated_at"] = utc_now_iso()
                    _write_json(Path(staged.derived_dir) / "parse_result.json", parse_payload)
                self._update_manifest_parse_artifacts(
                    staged,
                    result,
                    content_path,
                    Path(staged.derived_dir) / "full_text.md",
                    analysis_path,
                    chunks,
                    table_artifacts,
                    media_artifacts,
                    ocr_table_artifacts,
                    analysis=analysis,
                )
        except Exception as exc:
            self._transition_file_task(
                task_id,
                "fail",
                {
                    "progress": 100,
                    "phase": "文件 AI 分析写回失败",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "last_error": f"{type(exc).__name__}: {exc}",
                },
            )
            return
        if str(ai_analysis.get("status") or "") == "error":
            self._transition_file_task(
                task_id,
                "fail",
                {
                    "progress": 100,
                    "phase": "文件 AI 分析失败",
                    "detail": str(ai_analysis.get("error") or ""),
                    "last_error": str(ai_analysis.get("error") or ""),
                },
            )
        else:
            self._transition_file_task(
                task_id,
                "complete",
                {
                    "progress": 100,
                    "phase": "文件 AI 分析完成",
                    "detail": str(ai_analysis.get("summary") or "")[:300],
                    "actual_cost": 1,
                },
            )
        self._refresh_ledger_file_refs(staged)

    def _refresh_ledger_file_refs(self, staged: StagedFile) -> None:
        data_dir = self._task_data_dir()
        if data_dir is None:
            return
        try:
            from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore

            ConversationLedgerStore(data_dir).refresh_file_refs(staged.conversation_id)
        except Exception:
            return

    def _record_file_parse_task(
        self,
        staged: StagedFile,
        result: AttachmentParseResult,
        chunks: list[dict[str, Any]],
    ) -> None:
        data_dir = self._task_data_dir()
        if data_dir is None:
            return
        task_id = _file_parse_task_id(staged)
        status = "failed" if str(result.status or "") == "failed" else "completed"
        try:
            store = TaskStatusStore(data_dir)
            store.create(
                {
                    "task_id": task_id,
                    "title": f"解析文件：{staged.original_name}",
                    "kind": "file_parse",
                    "conversation_id": staged.conversation_id,
                    "session_id": staged.session_id,
                    "scope": f"conversation:{staged.conversation_id}",
                    "topic_id": f"file:{staged.file_id}",
                    "topic_title": staged.original_name,
                    "resource_class": "media_cpu" if staged.kind in {"image", "audio", "voice"} else "file_io",
                    "priority": 45,
                    "external_id": staged.file_id,
                    "metadata": {
                        "file_id": staged.file_id,
                        "file_name": staged.original_name,
                        "parse_status": result.status,
                        "chunk_count": len(chunks),
                    },
                }
            )
            action = "fail" if status == "failed" else "complete"
            store.transition(
                task_id,
                action,
                {
                    "progress": 100,
                    "phase": "文件解析完成" if status == "completed" else "文件解析失败",
                    "detail": result.error or result.summary,
                    "actual_cost": max(1, len(chunks)),
                },
            )
        except Exception:
            return

    def _record_file_ai_task(
        self,
        staged: StagedFile,
        result: AttachmentParseResult,
        *,
        status: str,
        progress: int,
        phase: str,
    ) -> None:
        data_dir = self._task_data_dir()
        if data_dir is None:
            return
        try:
            TaskStatusStore(data_dir).create(
                {
                    "task_id": _file_ai_task_id(staged),
                    "title": f"AI 分析文件：{staged.original_name}",
                    "kind": "file_ai_analysis",
                    "status": status,
                    "progress": progress,
                    "phase": phase,
                    "conversation_id": staged.conversation_id,
                    "session_id": staged.session_id,
                    "scope": f"conversation:{staged.conversation_id}",
                    "topic_id": f"file:{staged.file_id}",
                    "topic_title": staged.original_name,
                    "resource_class": "llm_background",
                    "priority": 35,
                    "estimated_cost": 2,
                    "external_id": staged.file_id,
                    "metadata": {
                        "file_id": staged.file_id,
                        "file_name": staged.original_name,
                        "file_kind": result.kind,
                    },
                }
            )
        except Exception:
            return

    def _transition_file_task(self, task_id: str, action: str, patch: dict[str, Any]) -> None:
        data_dir = self._task_data_dir()
        if data_dir is None:
            return
        try:
            TaskStatusStore(data_dir).transition(task_id, action, patch)
        except Exception:
            return

    def _task_data_dir(self) -> Path | None:
        # The shared runtime workspace is <data_dir>/file_workspace. Ad hoc
        # isolated workspaces used by tests/tools should not create a scheduler
        # authority beside themselves.
        if self.root.name != "file_workspace":
            return None
        return self.root.parent

    def staged_from_manifest(self, manifest_path: str | Path) -> StagedFile:
        safe_manifest_path = _ensure_within(Path(manifest_path).resolve(), self.root)
        manifest = _read_json(safe_manifest_path, None)
        if not isinstance(manifest, dict):
            raise FileNotFoundError(f"missing manifest: {manifest_path}")
        workspace_dir = _ensure_within(Path(str(manifest["workspace_dir"])).resolve(), self.root)
        staged_path = _ensure_within(Path(str(manifest["staged_path"])).resolve(), workspace_dir)
        derived_dir = _ensure_within(Path(str(manifest["derived_dir"])).resolve(), workspace_dir)
        outputs_dir = _ensure_within(Path(str(manifest["outputs_dir"])).resolve(), workspace_dir)
        blob_value = str(manifest.get("blob_path", "")).strip()
        blob_path = _ensure_within(Path(blob_value).resolve(), self.root / "_blobs") if blob_value else None
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
            blob_path=str(blob_path) if blob_path is not None else "",
            storage_mode=str(manifest.get("storage_mode", "")),
        )

    def _require_staged_manifest(self, staged: StagedFile) -> None:
        workspace_dir = _ensure_within(Path(staged.workspace_dir).resolve(), self.root)
        manifest_path = _ensure_within(Path(staged.manifest_path).resolve(), workspace_dir)
        _ensure_within(Path(staged.staged_path).resolve(), workspace_dir)
        _ensure_within(Path(staged.derived_dir).resolve(), workspace_dir)
        _ensure_within(Path(staged.outputs_dir).resolve(), workspace_dir)
        manifest = _read_json(manifest_path, None)
        if not isinstance(manifest, dict):
            raise FileNotFoundError(f"staged file manifest was removed: {manifest_path}")
        registered_workspace = _ensure_within(
            Path(str(manifest.get("workspace_dir") or "")).resolve(),
            self.root,
        )
        if (
            registered_workspace != workspace_dir
            or str(manifest.get("file_id") or "") != staged.file_id
            or str(manifest.get("sha256") or "") != staged.sha256
        ):
            raise FileNotFoundError(f"staged file manifest no longer matches: {manifest_path}")

    def parse_or_get_cached(
        self,
        staged: StagedFile,
        parser: Any,
        *,
        embedded_media_ocr: OcrEngine | None = None,
        embedded_media_asr: AsrEngine | None = None,
    ) -> AttachmentParseResult:
        with self._conversation_lifecycle_lock(staged.workspace_dir):
            self._require_staged_manifest(staged)
            self._ensure_staged_content(staged)
            cached = self.read_parse_result(staged)
            if cached is not None:
                if _needs_parse_refresh(staged, cached, embedded_media_ocr=embedded_media_ocr):
                    result = parser.parse(staged.staged_path)
                    self._write_parse_result_unlocked(
                        staged,
                        result,
                        embedded_media_ocr=embedded_media_ocr,
                        embedded_media_asr=embedded_media_asr,
                    )
                    return result
                if (
                    _needs_table_artifact_refresh(staged, cached)
                    or _needs_media_artifact_refresh(
                        staged,
                        embedded_media_ocr=embedded_media_ocr,
                        embedded_media_asr=embedded_media_asr,
                    )
                    or _needs_chunk_artifact_refresh(staged, cached)
                    or _needs_ocr_table_artifact_refresh(staged, cached)
                    or _needs_ai_analysis_refresh(staged, analyzer_available=self.analyzer is not None)
                ):
                    self._write_parse_result_unlocked(
                        staged,
                        cached,
                        embedded_media_ocr=embedded_media_ocr,
                        embedded_media_asr=embedded_media_asr,
                    )
                    return self.read_parse_result(staged) or cached
                return cached
            result = parser.parse(staged.staged_path)
            self._write_parse_result_unlocked(
                staged,
                result,
                embedded_media_ocr=embedded_media_ocr,
                embedded_media_asr=embedded_media_asr,
            )
            return result

    def _ensure_staged_content(self, staged: StagedFile) -> None:
        workspace_dir = _ensure_within(Path(staged.workspace_dir).resolve(), self.root)
        staged_path = _ensure_within(Path(staged.staged_path).resolve(), workspace_dir)
        if staged_path.is_file() and _sha256_file(staged_path) == staged.sha256:
            _set_read_only(staged_path)
            return
        blob_value = str(staged.blob_path or "").strip()
        if not blob_value:
            raise PermissionError("staged file checksum mismatch and no content blob is registered")
        blob_path = _ensure_within(Path(blob_value).resolve(), self.root / "_blobs")
        if not blob_path.is_file() or _sha256_file(blob_path) != staged.sha256:
            raise PermissionError("staged file and content blob checksum mismatch")
        _materialize_content_blob(blob_path, staged_path)

    def _conversation_segment(self, conversation_id: str) -> str:
        # Fast path: in-session stable segment. On a miss (cold cache after
        # restart, or outgoing attachments without a chat title), recover from
        # the channel index so the dir matches the channel's advertised path.
        cached_segment = self._segment_cache.get(conversation_id, "")
        if cached_segment:
            return cached_segment
        return resolve_segment(self.root.parent, conversation_id)

    def _remember_segment(self, conversation_id: str, chat_title: str = "") -> str:
        if not chat_title:
            cached_segment = self._segment_cache.get(conversation_id, "")
            if cached_segment:
                return cached_segment
        segment = resolve_segment(self.root.parent, conversation_id, chat_title)
        self._segment_cache[conversation_id] = segment
        return segment

    def file_dir(self, conversation_id: str, session_id: str, file_id: str) -> Path:
        return self.root / self._conversation_segment(conversation_id) / _safe_segment(session_id) / _safe_segment(file_id)

    def cleanup(
        self,
        *,
        max_age_seconds: float | None = None,
        max_total_bytes: int | None = None,
        keep_min: int = 20,
    ) -> dict[str, Any]:
        """Prune old per-file workspace dirs to bound disk growth.

        Each attachment stages an ``original/`` + ``derived/`` (incl. 2x-zoom PDF
        page renders + OCR/media) dir that otherwise accumulates forever. This
        removes whole ``<conv>/<session>/<file_id>/`` dirs, oldest first, when
        they exceed ``max_age_seconds`` or when the workspace total exceeds
        ``max_total_bytes`` — but always retains the newest ``keep_min`` dirs so
        recent context is never dropped. Session ``index.json`` files are updated
        to drop pruned entries. Best-effort and idempotent.
        """
        removed: list[str] = []
        removed_blobs = 0
        with self._root_lifecycle_lock():
            entries = self._all_file_dirs()
            # Oldest first (by mtime); newest kept for keep_min / recency.
            entries.sort(key=lambda item: item["mtime"])
            now = time.time()
            initial_total_bytes = _physical_tree_size(self.root)
            current_total_bytes = initial_total_bytes
            removable_max = max(0, len(entries) - max(0, keep_min))
            for item in entries[:removable_max]:
                too_old = max_age_seconds is not None and (now - item["mtime"]) > max_age_seconds
                over_size = max_total_bytes is not None and current_total_bytes > max_total_bytes
                if not (too_old or over_size):
                    continue
                try:
                    with self._conversation_lifecycle_lock(
                        Path(item["path"]),
                        timeout_seconds=0.1,
                    ):
                        if _has_active_lock(Path(item["path"])):
                            continue
                        _remove_tree(Path(item["path"]))
                except OSError:
                    continue
                removed.append(item["path"])
                self._drop_index_entry(item["conversation_id"], item["session_id"], item["file_id"])
                if max_total_bytes is not None:
                    blob_cleanup = self._prune_unreferenced_blobs()
                    removed_blobs += int(blob_cleanup.get("removed", 0) or 0)
                    current_total_bytes = _physical_tree_size(self.root)
            blob_cleanup = self._prune_unreferenced_blobs()
            removed_blobs += int(blob_cleanup.get("removed", 0) or 0)
            remaining_bytes = _physical_tree_size(self.root)
        return {
            "status": "ok",
            "scanned": len(entries),
            "removed": len(removed),
            "freed_bytes": max(0, initial_total_bytes - remaining_bytes),
            "remaining_bytes": remaining_bytes,
            "removed_blobs": removed_blobs,
            "size_basis": "unique_file_identity_bytes",
        }

    def _all_file_dirs(self) -> list[dict[str, Any]]:
        """Enumerate every staged file dir with its size and mtime."""
        results: list[dict[str, Any]] = []
        if not self.root.exists():
            return results
        for conv_dir in self.root.iterdir():
            if not conv_dir.is_dir():
                continue
            for session_dir in conv_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                for file_dir in session_dir.iterdir():
                    if not file_dir.is_dir():
                        continue
                    manifest = _read_json(file_dir / "manifest.json", None)
                    if not isinstance(manifest, dict):
                        # Not a staged-file dir (e.g. index.json lives at session level).
                        continue
                    results.append(
                        {
                            "path": str(file_dir),
                            "conversation_id": conv_dir.name,
                            "session_id": session_dir.name,
                            "file_id": file_dir.name,
                            "bytes": _dir_size(file_dir),
                            "mtime": file_dir.stat().st_mtime,
                        }
                    )
        return results

    def _prune_unreferenced_blobs(self) -> dict[str, int]:
        blob_root = self.root / "_blobs"
        if not blob_root.exists():
            return {"removed": 0, "freed_bytes": 0}
        referenced = {
            str(item.get("sha256") or "")
            for entry in self._all_file_dirs()
            if isinstance((item := _read_json(Path(entry["path"]) / "manifest.json", {})), dict)
        }
        removed = 0
        freed = 0
        for blob in blob_root.glob("*/*"):
            if not blob.is_file() or _is_lock_metadata_path(blob):
                continue
            if blob.name in referenced:
                _set_read_only(blob)
                continue
            try:
                size = blob.stat().st_size
                _unlink_file(blob)
            except OSError:
                continue
            removed += 1
            freed += size
        for directory in sorted(blob_root.glob("*"), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                continue
        return {"removed": removed, "freed_bytes": freed}

    def _drop_index_entry(self, conversation_id: str, session_id: str, file_id: str) -> None:
        index_path = self.root / conversation_id / session_id / "index.json"
        with _path_lock(index_path.with_suffix(index_path.suffix + ".lock")):
            index = _read_json(index_path, None)
            if not isinstance(index, dict):
                return
            files = index.get("files", [])
            if not isinstance(files, list):
                return
            kept = [item for item in files if not (isinstance(item, dict) and item.get("file_id") == file_id)]
            if len(kept) == len(files):
                return
            index["files"] = kept
            index["updated_at"] = utc_now_iso()
            _write_json(index_path, index)

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
        return self.root / self._conversation_segment(conversation_id) / _safe_segment(session_id) / "index.json"

    def _update_session_index(self, staged: StagedFile, manifest: dict[str, Any]) -> None:
        index_path = self._session_index_path(staged.conversation_id, staged.session_id)
        with _path_lock(index_path.with_suffix(index_path.suffix + ".lock")):
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
        full_text_path: Path,
        analysis_path: Path,
        chunks: list[dict[str, Any]],
        table_artifacts: dict[str, Any] | None = None,
        media_artifacts: dict[str, Any] | None = None,
        ocr_table_artifacts: dict[str, Any] | None = None,
        analysis: dict[str, Any] | None = None,
    ) -> None:
        manifest_path = Path(staged.manifest_path)
        manifest = _read_json(manifest_path, {})
        if not isinstance(manifest, dict):
            return
        table_artifacts = table_artifacts if isinstance(table_artifacts, dict) else {}
        media_artifacts = media_artifacts if isinstance(media_artifacts, dict) else {}
        ocr_table_artifacts = ocr_table_artifacts if isinstance(ocr_table_artifacts, dict) else {}
        analysis = analysis if isinstance(analysis, dict) else {}
        manifest["parse"] = {
            "status": result.status,
            "kind": result.kind,
            "summary": result.summary,
            "error": result.error,
            "ai_analysis_status": str(analysis.get("ai_analysis_status", "")),
            "ai_summary": str(analysis.get("ai_summary", "")),
            "ai_key_points": [
                str(item)
                for item in analysis.get("ai_key_points", [])
                if str(item).strip()
            ],
            "preview_char_count": int(analysis.get("preview_char_count", 0) or 0),
            "char_count": int(analysis.get("char_count", 0) or 0),
            "content_path": str(content_path),
            "full_text_path": str(full_text_path) if full_text_path.exists() else "",
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
            "ocr_tables_dir": str(ocr_table_artifacts.get("ocr_tables_dir", "")),
            "ocr_table_index_path": str(ocr_table_artifacts.get("index_path", "")),
            "ocr_table_chunk_count": int(ocr_table_artifacts.get("chunk_count", 0) or 0),
            "ocr_table_chunks": [
                dict(item)
                for item in ocr_table_artifacts.get("chunks", [])
                if isinstance(item, dict)
            ],
            "media_dir": str(media_artifacts.get("media_dir", "")),
            "media_index_path": str(media_artifacts.get("index_path", "")),
            "media_extract_count": int(media_artifacts.get("extract_count", 0) or 0),
            "media_ocr_status": str(media_artifacts.get("ocr_status", "")),
            "media_ocr_dir": str(media_artifacts.get("ocr_dir", "")),
            "media_ocr_index_path": str(media_artifacts.get("ocr_index_path", "")),
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


def _augment_analysis_text(text: str, media_artifacts: dict[str, Any] | None) -> str:
    """Fold extracted OCR/ASR text into the analysis input for media-only files.

    A pure image/audio file has only a placeholder as its parsed text, so the
    LLM would have nothing to analyze. Append any OCR/ASR text we extracted so
    the analysis reflects the real content.

    Only text from items that actually recognized content is included. When OCR
    is empty or ASR found no speech, ``ocr_text``/``asr_text`` hold a placeholder
    string, not real content; including it would make the analyzer summarize the
    boilerplate as if it were the document, defeating the empty/failed status.
    """
    return _dedup_join_text(_augment_analysis_parts(text, media_artifacts))


def _augment_analysis_parts(text: str, media_artifacts: dict[str, Any] | None) -> list[str]:
    parts = [text.strip()] if text and text.strip() else []
    artifacts = media_artifacts if isinstance(media_artifacts, dict) else {}
    for item in artifacts.get("images", []) or []:
        if isinstance(item, dict) and str(item.get("ocr_status", "")) == "parsed":
            ocr_text = str(item.get("ocr_text", "")).strip()
            if ocr_text:
                parts.append(ocr_text)
    for item in artifacts.get("audio", []) or []:
        if isinstance(item, dict) and str(item.get("asr_status", "")) == "transcribed":
            asr_text = str(item.get("asr_text", "")).strip()
            if asr_text:
                parts.append(asr_text)
    return parts


def _dedup_join_text(parts: list[str]) -> str:
    seen: set[str] = set()
    kept: list[str] = []
    for part in parts:
        body = str(part or "").strip()
        if not body:
            continue
        key = _text_fingerprint(body)
        if key in seen:
            continue
        seen.add(key)
        kept.append(body)
    return "\n\n".join(kept).strip()


def _valid_cached_ai_analysis(analysis: dict[str, Any], expected: dict[str, Any] | None = None) -> bool:
    if str(analysis.get("status", "")) != "analyzed":
        return False
    summary = str(analysis.get("summary", "")).strip()
    if "fake_llm.completed" in summary or "PLAN:" in summary or "MONITOR:" in summary:
        return False
    if expected and not _cache_meta_matches(analysis, expected):
        return False
    return True


def _pending_ai_analysis_still_fresh(analysis: dict[str, Any], expected: dict[str, Any]) -> bool:
    if str(analysis.get("status", "")) != "pending":
        return False
    if not _cache_meta_matches(analysis, expected):
        return False
    scheduled_at = _parse_iso_epoch(analysis.get("scheduled_at"))
    if scheduled_at <= 0:
        return False
    return time.time() - scheduled_at < AI_ANALYSIS_PENDING_RETRY_SECONDS


def _ai_analysis_cache_meta(staged: StagedFile, result: AttachmentParseResult, text: str) -> dict[str, Any]:
    return {
        "cache_version": AI_ANALYSIS_CACHE_VERSION,
        "source_sha256": staged.sha256,
        "staged_suffix": Path(staged.staged_path).suffix.lower(),
        "input_sha256": hashlib.sha256(str(text or "").encode("utf-8")).hexdigest(),
        "input_char_count": len(text or ""),
        "result_status": result.status,
        "result_kind": result.kind,
    }


def _cache_meta_matches(payload: dict[str, Any], expected: dict[str, Any]) -> bool:
    for key, value in expected.items():
        if payload.get(key) != value:
            return False
    return True


def _parse_iso_epoch(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _readable_text_for_file(result: AttachmentParseResult, media_artifacts: dict[str, Any] | None) -> str:
    parts: list[str] = []
    seen: set[str] = set()

    def add(label: str, text: str) -> None:
        body = str(text or "").strip()
        if not body:
            return
        key = _text_fingerprint(body)
        if key in seen:
            return
        seen.add(key)
        parts.append(f"## {label}\n\n{body}" if label else body)

    add("", str(result.text or ""))
    artifacts = media_artifacts if isinstance(media_artifacts, dict) else {}
    for index, item in enumerate(artifacts.get("images", []) or [], start=1):
        if not isinstance(item, dict) or str(item.get("ocr_status", "")) != "parsed":
            continue
        text = _read_text_section(Path(str(item.get("ocr_path", ""))), fallback=str(item.get("ocr_text", "")))
        add(f"OCR image {index}: {item.get('name', '')}", text)
    for index, item in enumerate(artifacts.get("audio", []) or [], start=1):
        if not isinstance(item, dict) or str(item.get("asr_status", "")) != "transcribed":
            continue
        text = _read_text_section(Path(str(item.get("asr_path", ""))), fallback=str(item.get("asr_text", "")))
        add(f"ASR audio {index}: {item.get('name', '')}", text)
    return "\n\n".join(parts).strip()


def _read_text_section(path: Path, *, fallback: str = "") -> str:
    if not str(path):
        return str(fallback or "").strip()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return str(fallback or "").strip()
    marker = "\n## Text\n"
    marker_index = text.find(marker)
    if marker_index < 0:
        return text.strip()
    body = text[marker_index + len(marker) :]
    next_section = body.find("\n## ")
    if next_section >= 0:
        body = body[:next_section]
    return body.strip()


def _text_fingerprint(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:12000]


def _analysis_payload(
    staged: StagedFile,
    result: AttachmentParseResult,
    chunks: list[dict[str, Any]],
    table_artifacts: dict[str, Any] | None = None,
    media_artifacts: dict[str, Any] | None = None,
    ai_analysis: dict[str, Any] | None = None,
    ocr_table_artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = _readable_text_for_file(result, media_artifacts)
    suffix = Path(staged.staged_path).suffix.lower()
    table_artifacts = table_artifacts if isinstance(table_artifacts, dict) else {}
    media_artifacts = media_artifacts if isinstance(media_artifacts, dict) else {}
    ai_analysis = ai_analysis if isinstance(ai_analysis, dict) else {}
    ocr_table_artifacts = ocr_table_artifacts if isinstance(ocr_table_artifacts, dict) else {}
    media = _document_media_analysis(Path(staged.staged_path), suffix, media_artifacts)
    preview_text = redact_file_internal_urls(_result_context_text(result) or _compact(text, PREVIEW_CHAR_TARGET))
    file_internal_url_count = count_urls(text)
    table_chunks = [
        dict(item)
        for item in table_artifacts.get("chunks", [])
        if isinstance(item, dict)
    ]
    ocr_table_chunks = [
        dict(item)
        for item in ocr_table_artifacts.get("chunks", [])
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
        "ai_analysis_status": str(ai_analysis.get("status", "disabled")),
        "ai_summary": str(ai_analysis.get("summary", "")),
        "ai_key_points": [str(item) for item in ai_analysis.get("key_points", []) if str(item).strip()],
        "ai_topics": [str(item) for item in ai_analysis.get("topics", []) if str(item).strip()],
        "ai_model": str(ai_analysis.get("model", "")),
        "ai_error": str(ai_analysis.get("error", "")),
        "ai_cache_version": int(ai_analysis.get("cache_version", 0) or 0),
        "ai_input_sha256": str(ai_analysis.get("input_sha256", "")),
        "ai_source_sha256": str(ai_analysis.get("source_sha256", "")),
        "scheduled_at": str(ai_analysis.get("scheduled_at", "")),
        "estimated_tokens": _estimate_tokens(text),
        "preview_tokens": _estimate_tokens(preview_text),
        "char_count": len(text),
        "preview_char_count": len(preview_text),
        "preview_text": preview_text,
        "line_count": len(text.splitlines()) if text else 0,
        "full_text_path": str(Path(staged.derived_dir) / "full_text.md") if text else "",
        "has_images": media["has_images"],
        "has_tables": result.kind == "spreadsheet" or bool(table_chunks) or bool(ocr_table_chunks),
        "has_audio": media["has_audio"],
        "media": media,
        "blocked_capabilities": _blocked_capabilities(media, media_artifacts),
        "media_status": str(media_artifacts.get("status", "")),
        "media_dir": str(media_artifacts.get("media_dir", "")),
        "media_index_path": str(media_artifacts.get("index_path", "")),
        "media_extract_count": int(media_artifacts.get("extract_count", 0) or 0),
            "media_ocr_status": str(media_artifacts.get("ocr_status", "")),
            "media_ocr_dir": str(media_artifacts.get("ocr_dir", "")),
            "media_ocr_index_path": str(media_artifacts.get("ocr_index_path", "")),
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
        "external_links": [],
        "external_link_count": file_internal_url_count,
        "external_links_hidden": file_internal_url_count > 0,
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
        "ocr_table_status": str(ocr_table_artifacts.get("status", "")),
        "ocr_tables_dir": str(ocr_table_artifacts.get("ocr_tables_dir", "")),
        "ocr_table_index_path": str(ocr_table_artifacts.get("index_path", "")),
        "ocr_table_source_count": int(ocr_table_artifacts.get("source_count", 0) or 0),
        "ocr_table_row_count": int(ocr_table_artifacts.get("row_count", 0) or 0),
        "ocr_table_chunk_count": int(ocr_table_artifacts.get("chunk_count", 0) or 0),
        "ocr_table_chunks": ocr_table_chunks,
        "ocr_table_error": str(ocr_table_artifacts.get("error", "")),
        "created_at": utc_now_iso(),
    }


def _content_markdown(staged: StagedFile, result: AttachmentParseResult, analysis: dict[str, Any]) -> str:
    preview = str(analysis.get("preview_text") or "").strip() or _result_context_text(result)
    lines = [
        f"# File Context: {staged.original_name}",
        "",
        f"- file_id: {staged.file_id}",
        f"- name: {staged.original_name}",
        f"- status: {result.status}",
        f"- kind: {result.kind}",
        f"- estimated_tokens: {analysis.get('estimated_tokens', 0)}",
        f"- chunk_count: {analysis.get('chunk_count', 0)}",
        f"- table_chunk_count: {analysis.get('table_chunk_count', 0)}",
        f"- media_ocr_count: {analysis.get('media_ocr_count', 0)}",
        f"- media_asr_count: {analysis.get('media_asr_count', 0)}",
        "",
        "## Summary",
        "",
        redact_file_internal_urls(result.summary or ""),
        "",
        *_ai_analysis_content_lines(analysis),
        *_table_content_lines(analysis),
        *_ocr_table_content_lines(analysis),
        *_media_content_lines(analysis),
        "## Preview",
        "",
        preview or "",
        "",
    ]
    if result.error:
        lines.extend(["## Error", "", result.error, ""])
    return "\n".join(lines)


def _full_text_markdown(staged: StagedFile, result: AttachmentParseResult, text: str | None = None) -> str:
    body = result.text or "" if text is None else text
    return "\n".join(
        [
            f"# Full Parsed Text: {staged.original_name}",
            "",
            f"- file_id: {staged.file_id}",
            f"- status: {result.status}",
            f"- kind: {result.kind}",
            "",
            "## Text",
            "",
            body,
            "",
        ]
    )


def _result_context_text(result: AttachmentParseResult) -> str:
    context = str(getattr(result, "context_text", "") or "").strip()
    if context:
        return context
    return _compact(str(result.text or "").strip(), PREVIEW_CHAR_TARGET)


def _ai_analysis_content_lines(analysis: dict[str, Any]) -> list[str]:
    if str(analysis.get("ai_analysis_status", "")) != "analyzed":
        return []
    summary = str(analysis.get("ai_summary", "")).strip()
    key_points = [str(item).strip() for item in analysis.get("ai_key_points", []) if str(item).strip()]
    topics = [str(item).strip() for item in analysis.get("ai_topics", []) if str(item).strip()]
    if not (summary or key_points or topics):
        return []
    lines = ["## AI Analysis", ""]
    if summary:
        lines.extend([redact_file_internal_urls(summary), ""])
    if key_points:
        lines.append("### Key Points")
        lines.append("")
        lines.extend(f"- {redact_file_internal_urls(point)}" for point in key_points)
        lines.append("")
    if topics:
        lines.append(f"- topics: {', '.join(topics)}")
        lines.append("")
    return lines


def _table_content_lines(analysis: dict[str, Any]) -> list[str]:
    if not (
        int(analysis.get("table_count", 0) or 0)
        or int(analysis.get("table_chunk_count", 0) or 0)
        or str(analysis.get("table_error", "")).strip()
    ):
        return []
    lines = [
        "## Tables",
        "",
        f"- status: {analysis.get('table_status', '')}",
        f"- table_count: {analysis.get('table_count', 0)}",
        f"- row_count: {analysis.get('table_row_count', 0)}",
        f"- chunk_count: {analysis.get('table_chunk_count', 0)}",
    ]
    error = str(analysis.get("table_error", "")).strip()
    if error:
        lines.append(f"- error: {error}")
    chunks = analysis.get("table_chunks", [])
    if isinstance(chunks, list) and chunks:
        first = chunks[0]
        if isinstance(first, dict):
            lines.append(
                "- first_chunk: "
                f"table={first.get('table_index', '')} "
                f"chunk={first.get('chunk_index', '')} "
                f"rows={first.get('row_count', '')}"
            )
    lines.append("")
    return lines


def _ocr_table_content_lines(analysis: dict[str, Any]) -> list[str]:
    chunk_count = int(analysis.get("ocr_table_chunk_count", 0) or 0)
    row_count = int(analysis.get("ocr_table_row_count", 0) or 0)
    error = str(analysis.get("ocr_table_error", "")).strip()
    if not (chunk_count or row_count or error):
        return []
    lines = [
        "## OCR Layout Rows",
        "",
        f"- status: {analysis.get('ocr_table_status', '')}",
        f"- source_count: {analysis.get('ocr_table_source_count', 0)}",
        f"- row_count: {row_count}",
        f"- chunk_count: {chunk_count}",
    ]
    if error:
        lines.append(f"- error: {error}")
    chunks = analysis.get("ocr_table_chunks", [])
    if isinstance(chunks, list) and chunks:
        first = chunks[0]
        if isinstance(first, dict):
            lines.append(
                "- first_chunk: "
                f"source={first.get('source_index', '')} "
                f"chunk={first.get('chunk_index', '')} "
                f"rows={first.get('row_count', '')}"
            )
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
        f"- ocr_status: {analysis.get('media_ocr_status', '')}",
        f"- ocr_count: {analysis.get('media_ocr_count', 0)}",
        f"- asr_status: {analysis.get('media_asr_status', '')}",
        f"- asr_count: {analysis.get('media_asr_count', 0)}",
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
            lines.append(f"- first_image: name={first.get('name', '')} status={first.get('ocr_status', '')}")
            if first.get("ocr_path"):
                lines.append(f"- first_image_ocr_chars: {first.get('ocr_char_count', '')}")
    media_audio = analysis.get("media_audio", [])
    if isinstance(media_audio, list) and media_audio:
        first = media_audio[0]
        if isinstance(first, dict):
            lines.append(f"- first_audio: name={first.get('name', '')} status={first.get('asr_status', '')}")
            if first.get("asr_path"):
                lines.append(f"- first_audio_asr_backend: {first.get('asr_backend', '')}")
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


def _write_ocr_table_artifacts(
    staged: StagedFile,
    result: AttachmentParseResult,
    media_artifacts: dict[str, Any] | None,
) -> dict[str, Any]:
    ocr_tables_dir = Path(staged.derived_dir) / "ocr_tables"
    sources = _ocr_layout_sources(staged, result, media_artifacts)
    if not sources:
        if ocr_tables_dir.exists():
            shutil.rmtree(ocr_tables_dir)
        return {}
    if ocr_tables_dir.exists():
        shutil.rmtree(ocr_tables_dir)
    ocr_tables_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[dict[str, Any]] = []
    source_refs: list[dict[str, Any]] = []
    total_rows = 0
    try:
        for source_index, source in enumerate(sources, start=1):
            rows = list(source.get("rows", [])) if isinstance(source.get("rows"), list) else []
            if not rows:
                continue
            total_rows += len(rows)
            source_ref = {
                "source_index": source_index,
                "source_type": source.get("source_type", ""),
                "name": source.get("name", ""),
                "path": source.get("path", ""),
                "ocr_path": source.get("ocr_path", ""),
                "ocr_detail_path": source.get("ocr_detail_path", ""),
                "row_count": len(rows),
                "cell_count": sum(int(row.get("cell_count", 0) or 0) for row in rows if isinstance(row, dict)),
                "table_like": any(int(row.get("cell_count", 0) or 0) > 1 for row in rows if isinstance(row, dict)),
            }
            source_refs.append(source_ref)
            for chunk_index, start in enumerate(range(0, len(rows), OCR_ROWS_PER_CHUNK), start=1):
                chunk_rows = rows[start : start + OCR_ROWS_PER_CHUNK]
                chunk_path = ocr_tables_dir / f"ocr_source_{source_index:04d}_chunk_{chunk_index:04d}.json"
                payload = {
                    "artifact_type": "ocr_layout_rows",
                    "source": source_ref,
                    "source_range": {"start_row": start + 1, "end_row": start + len(chunk_rows)},
                    "row_count": len(chunk_rows),
                    "rows": chunk_rows,
                }
                _write_json(chunk_path, payload)
                chunks.append(
                    {
                        "source_index": source_index,
                        "chunk_index": chunk_index,
                        "path": str(chunk_path),
                        "source_range": payload["source_range"],
                        "row_count": len(chunk_rows),
                        "table_like": bool(source_ref["table_like"]),
                    }
                )
        payload = {
            "status": "completed" if chunks else "empty",
            "ocr_tables_dir": str(ocr_tables_dir),
            "index_path": str(ocr_tables_dir / "index.json"),
            "source_count": len(source_refs),
            "row_count": total_rows,
            "chunk_count": len(chunks),
            "sources": source_refs,
            "chunks": chunks,
        }
    except Exception as exc:
        payload = {
            "status": "failed",
            "ocr_tables_dir": str(ocr_tables_dir),
            "index_path": str(ocr_tables_dir / "index.json"),
            "source_count": 0,
            "row_count": 0,
            "chunk_count": 0,
            "sources": [],
            "chunks": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    _write_json(ocr_tables_dir / "index.json", payload)
    return payload


def _ocr_layout_sources(
    staged: StagedFile,
    result: AttachmentParseResult,
    media_artifacts: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    metadata = getattr(result, "metadata", {}) if isinstance(getattr(result, "metadata", {}), dict) else {}
    ocr_payload = metadata.get("ocr") if isinstance(metadata.get("ocr"), dict) else {}
    if result.kind == "image" and result.status == "parsed":
        rows = _ocr_rows_from_payload_or_text(ocr_payload, result.text)
        if rows:
            sources.append(
                {
                    "source_type": "standalone_image",
                    "name": staged.original_name,
                    "path": staged.staged_path,
                    "rows": rows,
                }
            )
    artifacts = media_artifacts if isinstance(media_artifacts, dict) else {}
    for item in artifacts.get("images", []) or []:
        if not isinstance(item, dict) or str(item.get("ocr_status", "")) != "parsed":
            continue
        detail_payload = _read_json(Path(str(item.get("ocr_detail_path", ""))), {})
        text = _read_text_section(Path(str(item.get("ocr_path", ""))), fallback=str(item.get("ocr_text", "")))
        rows = _ocr_rows_from_payload_or_text(detail_payload if isinstance(detail_payload, dict) else {}, text)
        if rows:
            sources.append(
                {
                    "source_type": "embedded_or_rendered_image",
                    "name": item.get("name", ""),
                    "path": item.get("path", ""),
                    "ocr_path": item.get("ocr_path", ""),
                    "ocr_detail_path": item.get("ocr_detail_path", ""),
                    "rows": rows,
                }
            )
    return sources


def _ocr_rows_from_payload_or_text(payload: dict[str, Any], text: str) -> list[dict[str, Any]]:
    raw_items = payload.get("items") or payload.get("detections")
    items = _ocr_items_from_payload(raw_items)
    if items:
        rows = ocr_rows_payload(items)
        if rows:
            return rows
    return _ocr_rows_from_text(text)


def _ocr_items_from_payload(raw_items: Any) -> list[OcrItem]:
    if not isinstance(raw_items, list):
        return []
    items: list[OcrItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text", "")).strip()
        if not text:
            continue
        try:
            score = float(entry.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        box = entry.get("box", [])
        points: list[list[float]] = []
        if isinstance(box, (list, tuple)):
            for point in box:
                if isinstance(point, (list, tuple)) and len(point) >= 2:
                    try:
                        points.append([float(point[0]), float(point[1])])
                    except (TypeError, ValueError):
                        continue
        items.append(OcrItem(text=text, score=score, box=points, backend=str(entry.get("backend", ""))))
    return items


def _ocr_rows_from_text(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row_index, line in enumerate([line.strip() for line in str(text or "").splitlines() if line.strip()], start=1):
        cells = [cell.strip() for cell in line.split("\t") if cell.strip()]
        if not cells:
            cells = [line]
        rows.append(
            {
                "row_index": row_index,
                "cell_count": len(cells),
                "text": "\t".join(cells),
                "cells": [
                    {
                        "column_index": column_index,
                        "text": cell,
                        "score": 0.0,
                        "box": [],
                        "backend": "text_layout_fallback",
                        "bounds": [],
                    }
                    for column_index, cell in enumerate(cells, start=1)
                ],
            }
        )
    return rows


def _write_media_artifacts(
    staged: StagedFile,
    *,
    result: AttachmentParseResult | None = None,
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
            result=result,
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
        "ocr_index_path": ocr_payload["ocr_index_path"],
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
        "ocr_index_path": ocr_payload["ocr_index_path"],
        "ocr_count": ocr_payload["ocr_count"],
        "ocr_error_count": ocr_payload["error_count"],
        "asr_status": "not_needed",
        "asr_dir": "",
        "asr_count": 0,
        "asr_error_count": 0,
        "page_render_status": render_payload.get("status", ""),
        "page_render_dir": render_payload.get("render_dir", ""),
        "page_render_count": render_payload.get("render_count", 0),
        "page_render_page_count": render_payload.get("page_count", 0),
        "page_render_scope": render_payload.get("render_scope", ""),
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
            "page_count": 0,
            "render_scope": "unavailable",
            "images": [],
            "error": "PyMuPDF is not installed; install it in vendor/ocr-python for scanned PDF page rendering",
        }
    try:
        document = fitz.open(str(path))
        render_dir.mkdir(parents=True, exist_ok=True)
        images: list[dict[str, Any]] = []
        page_count = len(document)
        for page_index in range(page_count):
            page = document[page_index]
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            output = render_dir / f"page_{page_index + 1:04d}.png"
            pix.save(str(output))
            images.append(_media_item(f"page:{page_index + 1}:render", output, ".png"))
        return {
            "status": "completed",
            "render_dir": str(render_dir),
            "render_count": len(images),
            "page_count": page_count,
            "render_scope": "all_pages",
            "images": images,
            "error": "",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "render_dir": str(render_dir),
            "render_count": 0,
            "page_count": 0,
            "render_scope": "failed",
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
        "sha256": _sha256_file(output) if output.exists() and output.is_file() else "",
    }


def _write_standalone_audio_artifacts(
    path: Path,
    media_dir: Path,
    *,
    result: AttachmentParseResult | None = None,
    embedded_media_asr: AsrEngine | None,
) -> dict[str, Any]:
    media_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = media_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    copied = audio_dir / _safe_filename(path.name, path.suffix)
    if not copied.exists() or _sha256_file(copied) != _sha256_file(path):
        shutil.copy2(path, copied)
    audio = [_media_item(path.name, copied, path.suffix.lower())]
    if result is not None and result.kind == "audio" and result.status == "parsed" and str(result.text or "").strip():
        asr_payload = _write_seeded_audio_asr_artifacts(audio, media_dir / "asr", result)
    else:
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
        "ocr_index_path": "",
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


def _write_seeded_audio_asr_artifacts(
    audio: list[dict[str, Any]],
    asr_dir: Path,
    result: AttachmentParseResult,
) -> dict[str, Any]:
    if not audio:
        return {"status": "not_needed", "asr_dir": "", "asr_count": 0, "empty_count": 0, "error_count": 0, "audio": audio}
    asr_dir.mkdir(parents=True, exist_ok=True)
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    asr_meta = metadata.get("asr") if isinstance(metadata.get("asr"), dict) else {}
    updated: list[dict[str, Any]] = []
    for index, item in enumerate(audio, start=1):
        current = dict(item)
        output_path = asr_dir / f"{index:04d}_{Path(str(item.get('name', 'audio'))).stem}.md"
        text = str(result.text or "").strip()
        output_path.write_text(_media_asr_markdown(current, "transcribed", text=text), encoding="utf-8")
        cache = {
            "cache_version": MEDIA_ASR_CACHE_VERSION,
            "source_sha256": str(current.get("sha256", "")),
            "engine": "primary_audio_parse",
            "result_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        current.update(
            {
                "asr_status": "transcribed",
                "asr_path": str(output_path),
                "asr_backend": str(asr_meta.get("backend", "primary_audio_parse")),
                "asr_model": str(asr_meta.get("model", "")),
                "asr_text": _compact(text, 2000),
                "asr_error": "",
                "asr_cache": cache,
            }
        )
        updated.append(current)
    return {
        "status": "completed",
        "asr_dir": str(asr_dir),
        "asr_count": len(updated),
        "empty_count": 0,
        "error_count": 0,
        "audio": updated,
    }


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
    previous = _read_json(asr_dir.parent / "index.json", {})
    previous_audio = previous.get("audio", []) if isinstance(previous, dict) else []
    updated: list[dict[str, Any]] = []
    asr_count = 0
    error_count = 0
    empty_count = 0
    for index, item in enumerate(audio, start=1):
        audio_path = Path(str(item.get("path", "")))
        output_path = asr_dir / f"{index:04d}_{Path(str(item.get('name', 'audio'))).stem}.md"
        current = dict(item)
        cache = _media_asr_cache_payload(current, embedded_media_asr)
        cached = _cached_media_item(previous_audio, current, cache_key="asr_cache", expected_cache=cache, output_key="asr_path")
        if cached is not None:
            current.update(cached)
            current["asr_cache_hit"] = True
            status_value = str(current.get("asr_status", ""))
            if status_value == "transcribed":
                asr_count += 1
            elif status_value == "empty":
                empty_count += 1
            elif status_value in {"failed", "blocked"}:
                error_count += 1
            updated.append(current)
            continue
        transcript = embedded_media_asr.transcribe(audio_path)
        text = transcript.text
        # An "empty" transcript means the audio ran through ASR cleanly but held
        # no detectable speech. Record a placeholder so the reader can tell this
        # apart from a transcription failure, and never emit blank content.
        if transcript.status == "empty" and not text.strip():
            text = _audio_asr_placeholder_text(current, "ASR 未识别到语音内容")
        output_path.write_text(_media_asr_markdown(current, transcript.status, text=text, error=transcript.error), encoding="utf-8")
        current.update(
            {
                "asr_status": transcript.status,
                "asr_path": str(output_path),
                "asr_backend": transcript.backend,
                "asr_model": transcript.model,
                "asr_text": _compact(text, 2000),
                "asr_error": transcript.error,
                "asr_cache": cache,
                "asr_cache_hit": False,
            }
        )
        if transcript.status == "transcribed":
            asr_count += 1
        elif transcript.status == "empty":
            empty_count += 1
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
        "empty_count": empty_count,
        "error_count": error_count,
        "audio": updated,
    }


def _audio_asr_placeholder_text(item: dict[str, Any], reason: str) -> str:
    return (
        "[音频 ASR 占位符]\n"
        f"- 文件名: {item.get('name', '')}\n"
        f"- 本地路径: {item.get('path', '')}\n"
        f"- 原因: {reason}\n"
        "- 说明: 该音频已进入文件中间层，但本地 ASR 未转写出文字内容。"
    )


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


def _run_structured_ocr(engine: OcrEngine, image_path: Path):
    """Call read_structured when available, else adapt read_text to OcrResult."""
    from app.personal_wechat_bot.vision.ocr import OcrResult

    read_structured = getattr(engine, "read_structured", None)
    if callable(read_structured):
        return read_structured(image_path)
    return OcrResult(text=engine.read_text(image_path), items=[])


def _write_ocr_detail_sidecar(markdown_path: Path, result: Any) -> int:
    """Write per-detection geometry/confidence next to the OCR markdown.

    Returns the number of detections written. When the engine returned no
    structured items (e.g. a text-only fallback) nothing is written.
    """
    items = getattr(result, "items", None) or []
    if not items:
        return 0
    detail_path = markdown_path.with_suffix(".json")
    payload = {
        "cache_version": OCR_PARSE_CACHE_VERSION,
        "text": getattr(result, "text", ""),
        "detection_count": len(items),
        "detections": [
            {
                "text": item.text,
                "score": round(float(item.score), 4),
                "box": item.box,
                "backend": getattr(item, "backend", ""),
            }
            for item in items
        ],
    }
    payload["items"] = payload["detections"]
    _write_json(detail_path, payload)
    return len(items)


def _media_ocr_cache_payload(item: dict[str, Any], engine: OcrEngine) -> dict[str, Any]:
    return {
        "cache_version": MEDIA_OCR_CACHE_VERSION,
        "source_sha256": str(item.get("sha256", "")),
        "engine": _engine_signature(engine),
    }


def _media_asr_cache_payload(item: dict[str, Any], engine: AsrEngine) -> dict[str, Any]:
    return {
        "cache_version": MEDIA_ASR_CACHE_VERSION,
        "source_sha256": str(item.get("sha256", "")),
        "engine": _engine_signature(engine),
    }


def _engine_signature(engine: Any) -> str:
    if engine is None:
        return "none"
    attrs = []
    for name in ("mode", "model", "language"):
        value = getattr(engine, name, "")
        if value not in ("", None):
            attrs.append(f"{name}={value}")
    return f"{engine.__class__.__module__}.{engine.__class__.__name__}" + (":" + ",".join(attrs) if attrs else "")


def _cached_media_item(
    previous_items: Any,
    current: dict[str, Any],
    *,
    cache_key: str,
    expected_cache: dict[str, Any],
    output_key: str,
) -> dict[str, Any] | None:
    if not isinstance(previous_items, list):
        return None
    source_sha = str(current.get("sha256", ""))
    source_name = str(current.get("source_name", ""))
    name = str(current.get("name", ""))
    for item in previous_items:
        if not isinstance(item, dict):
            continue
        if str(item.get("sha256", "")) != source_sha:
            continue
        if source_name and str(item.get("source_name", "")) != source_name:
            continue
        if name and str(item.get("name", "")) != name:
            continue
        cache = item.get(cache_key) if isinstance(item.get(cache_key), dict) else {}
        if cache != expected_cache:
            continue
        output_path = Path(str(item.get(output_key, "")))
        if not output_path.is_file():
            continue
        return dict(item)
    return None


def _write_media_ocr_artifacts(
    images: list[dict[str, Any]],
    ocr_dir: Path,
    embedded_media_ocr: OcrEngine | None,
) -> dict[str, Any]:
    if not images:
        return {"status": "not_needed", "ocr_dir": "", "ocr_index_path": "", "ocr_count": 0, "error_count": 0, "images": images}
    if embedded_media_ocr is None:
        updated = [dict(item, ocr_status="skipped_no_ocr_engine") for item in images]
        return {"status": "skipped_no_ocr_engine", "ocr_dir": "", "ocr_index_path": "", "ocr_count": 0, "error_count": 0, "images": updated}
    ocr_dir.mkdir(parents=True, exist_ok=True)
    previous = _read_json(ocr_dir.parent / "index.json", {})
    previous_images = previous.get("images", []) if isinstance(previous, dict) else []
    updated: list[dict[str, Any]] = []
    ocr_count = 0
    error_count = 0
    for index, item in enumerate(images, start=1):
        image_path = Path(str(item.get("path", "")))
        output_path = ocr_dir / f"{index:04d}_{Path(str(item.get('name', 'image'))).stem}.md"
        current = dict(item)
        cache = _media_ocr_cache_payload(current, embedded_media_ocr)
        cached = _cached_media_item(previous_images, current, cache_key="ocr_cache", expected_cache=cache, output_key="ocr_path")
        if cached is not None:
            current.update(cached)
            current["ocr_cache_hit"] = True
            if str(current.get("ocr_status", "")) in {"parsed", "empty"}:
                ocr_count += 1
            elif str(current.get("ocr_status", "")) == "failed":
                error_count += 1
            updated.append(current)
            continue
        try:
            result = _run_structured_ocr(embedded_media_ocr, image_path)
            text = result.text
            status = "parsed" if text.strip() else "empty"
            if not text.strip():
                text = _image_ocr_placeholder_text(current, "OCR 未识别到有效文本")
            # Persist the layout-aware markdown plus a JSON sidecar carrying the
            # per-detection geometry/confidence so structure is not lost.
            output_path.write_text(_media_ocr_markdown(current, status, text=text), encoding="utf-8")
            detail_count = _write_ocr_detail_sidecar(output_path, result)
            current.update(
                {
                    "ocr_status": status,
                    "ocr_path": str(output_path),
                    "ocr_text": _compact(text, 2000),
                    "ocr_char_count": len(text),
                    "ocr_detection_count": detail_count,
                    "ocr_detail_path": str(output_path.with_suffix(".json")) if detail_count else "",
                    "ocr_cache": cache,
                    "ocr_cache_hit": False,
                }
            )
            ocr_count += 1
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            output_path.write_text(_media_ocr_markdown(current, "failed", error=error), encoding="utf-8")
            current.update({"ocr_status": "failed", "ocr_path": str(output_path), "ocr_error": error, "ocr_cache": cache, "ocr_cache_hit": False})
            error_count += 1
        updated.append(current)
    if error_count and ocr_count:
        status = "partial"
    elif error_count:
        status = "failed"
    else:
        status = "completed"
    index_path = ocr_dir / "index.md"
    index_path.write_text(_media_ocr_index_markdown(updated), encoding="utf-8")
    return {
        "status": status,
        "ocr_dir": str(ocr_dir),
        "ocr_index_path": str(index_path),
        "ocr_count": ocr_count,
        "error_count": error_count,
        "images": updated,
    }


def _image_ocr_placeholder_text(item: dict[str, Any], reason: str) -> str:
    return (
        "[图片 OCR 占位符]\n"
        f"- 文件名: {item.get('name', '')}\n"
        f"- 本地路径: {item.get('path', '')}\n"
        f"- 原因: {reason}\n"
        "- 说明: 该图片已进入文件中间层，但没有可用文字内容。"
    )


def _media_ocr_index_markdown(images: list[dict[str, Any]]) -> str:
    lines = ["# Embedded Image OCR Index", ""]
    for index, item in enumerate(images, start=1):
        lines.extend(
            [
                f"## Image {index}: {item.get('name', '')}",
                "",
                f"- source_name: {item.get('source_name', '')}",
                f"- image_path: {item.get('path', '')}",
                f"- ocr_path: {item.get('ocr_path', '')}",
                f"- status: {item.get('ocr_status', '')}",
                *(
                    [f"- detections: {item.get('ocr_detection_count', 0)} (detail: {item.get('ocr_detail_path', '')})"]
                    if int(item.get("ocr_detection_count", 0) or 0)
                    else []
                ),
                "",
            ]
        )
        text = str(item.get("ocr_text", "")).strip()
        error = str(item.get("ocr_error", "")).strip()
        if text:
            lines.extend(["### Text", "", text, ""])
        if error:
            lines.extend(["### Error", "", error, ""])
    return "\n".join(lines)


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


def _needs_parse_refresh(
    staged: StagedFile,
    result: AttachmentParseResult,
    *,
    embedded_media_ocr: OcrEngine | None = None,
) -> bool:
    suffix = Path(staged.staged_path).suffix.lower()
    if suffix not in IMAGE_SUFFIXES or result.kind != "image" or embedded_media_ocr is None:
        return False
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    ocr = metadata.get("ocr") if isinstance(metadata.get("ocr"), dict) else {}
    version = int(ocr.get("cache_version", 0) or 0) if ocr else 0
    if version < OCR_PARSE_CACHE_VERSION:
        return True
    if result.status == "parsed" and str(result.text or "").strip() and int(ocr.get("item_count", 0) or 0) <= 0:
        return True
    return False


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
    if suffix == ".pdf" and _pdf_media_cache_is_stale(index):
        return True
    images = index.get("images", [])
    if not isinstance(images, list):
        return True
    if embedded_media_ocr is not None and (
        not str(index.get("ocr_index_path", "")).strip()
        or any(isinstance(item, dict) and not item.get("ocr_status") for item in images)
        or any(isinstance(item, dict) and _media_cache_stale(item, "ocr_cache", MEDIA_OCR_CACHE_VERSION, embedded_media_ocr) for item in images)
    ):
        return True
    audio = index.get("audio", [])
    if embedded_media_asr is not None and isinstance(audio, list):
        return any(
            isinstance(item, dict)
            and (
                not item.get("asr_status")
                or _media_cache_stale(item, "asr_cache", MEDIA_ASR_CACHE_VERSION, embedded_media_asr)
            )
            for item in audio
        )
    return False


def _media_cache_stale(item: dict[str, Any], key: str, version: int, engine: Any) -> bool:
    cache = item.get(key) if isinstance(item.get(key), dict) else {}
    if not cache:
        return True
    if int(cache.get("cache_version", 0) or 0) != version:
        return True
    if str(cache.get("source_sha256", "")) != str(item.get("sha256", "")):
        return True
    if str(cache.get("engine", "")) == "primary_audio_parse":
        return False
    return str(cache.get("engine", "")) != _engine_signature(engine)


def _needs_chunk_artifact_refresh(staged: StagedFile, result: AttachmentParseResult) -> bool:
    analysis = _read_json(Path(staged.derived_dir) / "analysis.json", {})
    if not isinstance(analysis, dict):
        return True
    media = _read_json(Path(staged.derived_dir) / "media" / "index.json", {})
    media_artifacts = media if isinstance(media, dict) else {}
    expected = len(_split_text(_readable_text_for_file(result, media_artifacts), CHUNK_CHAR_TARGET))
    current = int(analysis.get("chunk_count", 0) or 0)
    if expected != current:
        return True
    chunks = analysis.get("chunks", [])
    if expected and not isinstance(chunks, list):
        return True
    for item in chunks if isinstance(chunks, list) else []:
        if not isinstance(item, dict):
            continue
        if not Path(str(item.get("path", ""))).is_file():
            return True
        if int(item.get("token_estimate", 0) or 0) > CHUNK_TOKEN_TARGET:
            return True
    return False


def _needs_ocr_table_artifact_refresh(staged: StagedFile, result: AttachmentParseResult) -> bool:
    analysis = _read_json(Path(staged.derived_dir) / "analysis.json", {})
    if not isinstance(analysis, dict):
        return False
    if int(analysis.get("ocr_table_chunk_count", 0) or 0) > 0:
        return False
    if result.kind == "image" and result.status == "parsed" and str(result.text or "").strip():
        return True
    media_images = analysis.get("media_images", [])
    if isinstance(media_images, list):
        return any(
            isinstance(item, dict)
            and str(item.get("ocr_status", "")) == "parsed"
            and (item.get("ocr_detail_path") or item.get("ocr_path") or item.get("ocr_text"))
            for item in media_images
        )
    return False


def _needs_ai_analysis_refresh(staged: StagedFile, *, analyzer_available: bool) -> bool:
    analysis = _read_json(Path(staged.derived_dir) / "analysis.json", {})
    if not isinstance(analysis, dict):
        return False
    status = str(analysis.get("ai_analysis_status", "")).strip()
    summary = str(analysis.get("ai_summary", "")).strip()
    if status == "analyzed" and ("fake_llm.completed" in summary or "PLAN:" in summary or "MONITOR:" in summary):
        return True
    if analyzer_available and status == "pending":
        scheduled_at = _parse_iso_epoch(analysis.get("scheduled_at") or analysis.get("created_at"))
        return scheduled_at <= 0 or (time.time() - scheduled_at) >= AI_ANALYSIS_PENDING_RETRY_SECONDS
    return False


def _pdf_media_cache_is_stale(index: dict[str, Any]) -> bool:
    render_status = str(index.get("page_render_status", "")).strip()
    if render_status == "completed" and str(index.get("page_render_scope", "")) != "all_pages":
        return True
    page_count = int(index.get("page_render_page_count", 0) or 0)
    render_count = int(index.get("page_render_count", 0) or 0)
    return page_count > 0 and render_count < page_count


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
    # A capability is "blocked" only when OCR/ASR did not actually run (no engine,
    # missing deps, or error) — NOT when it ran cleanly and simply found no text
    # (an "empty" result). OCR already counts empty items into ocr_count; ASR
    # tracks empties separately in empty_count, so fold that in.
    ocr_count = int(artifacts.get("ocr_count", 0) or 0)
    ocr_status = str(artifacts.get("ocr_status", ""))
    asr_ran = int(artifacts.get("asr_count", 0) or 0) + int(artifacts.get("empty_count", 0) or 0)
    asr_status = str(artifacts.get("asr_status", ""))
    ocr_ok = ocr_count > 0 or ocr_status in {"completed", "partial"}
    asr_ok = asr_ran > 0 or asr_status in {"completed", "partial"}
    if media.get("embedded") and media.get("has_images") and not ocr_ok:
        blocked.append("embedded_image_extraction_and_ocr")
    if media.get("has_audio") and not asr_ok:
        blocked.append("embedded_audio_extraction_and_asr")
    return blocked


def _write_chunks(chunks_dir: Path, text: str) -> list[dict[str, Any]]:
    if not text:
        if chunks_dir.exists():
            shutil.rmtree(chunks_dir)
        return []
    chunks = _split_text(text, CHUNK_CHAR_TARGET)
    if not chunks:
        if chunks_dir.exists():
            shutil.rmtree(chunks_dir)
        return []
    chunks_dir.mkdir(parents=True, exist_ok=True)
    for old_chunk in chunks_dir.glob("chunk_*.md"):
        try:
            old_chunk.unlink()
        except OSError:
            pass
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


def _split_text(text: str, max_chars: int, max_tokens: int = CHUNK_TOKEN_TARGET) -> list[str]:
    normalized = text.strip()
    if not normalized:
        return []
    if _fits_chunk(normalized, max_chars, max_tokens):
        return [normalized]
    paragraphs = re.split(r"\n\s*\n", normalized)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if not _fits_chunk(paragraph, max_chars, max_tokens):
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            chunks.extend(_split_long_text(paragraph, max_chars, max_tokens))
            continue
        added_len = len(paragraph) + (2 if current else 0)
        candidate_text = "\n\n".join([*current, paragraph]) if current else paragraph
        if current and not _fits_chunk(candidate_text, max_chars, max_tokens):
            chunks.append("\n\n".join(current))
            current = [paragraph]
            current_len = len(paragraph)
        else:
            current.append(paragraph)
            current_len += added_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _split_long_text(text: str, max_chars: int, max_tokens: int = CHUNK_TOKEN_TARGET) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = _chunk_end_for_limits(text, start, max_chars, max_tokens)
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
            if boundary > start + max(1, (end - start) // 2):
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = max(end, start + 1)
    return chunks


def _chunk_end_for_limits(text: str, start: int, max_chars: int, max_tokens: int) -> int:
    hard_end = min(len(text), start + max(1, max_chars))
    if _fits_chunk(text[start:hard_end], max_chars, max_tokens):
        return hard_end
    low = start + 1
    high = hard_end
    best = low
    while low <= high:
        mid = (low + high) // 2
        if _fits_chunk(text[start:mid], max_chars, max_tokens):
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def _fits_chunk(text: str, max_chars: int, max_tokens: int) -> bool:
    return len(text) <= max_chars and (max_tokens <= 0 or _estimate_tokens(text) <= max_tokens)


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
    if not text:
        return 0
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    non_cjk = max(0, len(text) - cjk)
    return max(1, cjk + non_cjk // 4)


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


def _file_parse_task_id(staged: StagedFile) -> str:
    return f"file-parse-{_task_id_fragment(staged.file_id)}"


def _file_ai_task_id(staged: StagedFile) -> str:
    return f"file-ai-{_task_id_fragment(staged.file_id)}"


def _task_id_fragment(value: str) -> str:
    cleaned = "".join(ch for ch in str(value or "").strip() if ch.isalnum() or ch in {"-", "_", "."})
    return cleaned[:64] or "unknown"




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


def _ensure_content_blob(root: Path, source: Path, digest: str) -> Path:
    blob = root / "_blobs" / digest[:2] / digest
    with _path_lock(blob.with_name(f"{blob.name}.lock")):
        if blob.exists() and _sha256_file(blob) == digest:
            _set_read_only(blob)
            return blob
        blob.parent.mkdir(parents=True, exist_ok=True)
        tmp = blob.with_name(f"{blob.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(source, tmp)
            if _sha256_file(tmp) != digest:
                raise OSError("content blob checksum mismatch")
            if blob.exists():
                _unlink_file(blob)
            tmp.replace(blob)
            _set_read_only(blob)
        finally:
            if tmp.exists():
                _unlink_file(tmp)
    return blob


def _materialize_content_blob(blob: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _unlink_file(target)
    try:
        os.link(blob, target)
        if _set_read_only(target):
            return "hardlink"
        _unlink_file(target)
        _set_read_only(blob)
    except OSError:
        pass
    shutil.copy2(blob, target)
    _set_read_only(target)
    return "copy"


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _set_read_only(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
        path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        return not bool(path.stat().st_mode & stat.S_IWUSR)
    except OSError:
        return False


def _set_writable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    except OSError:
        return


def _unlink_file(path: Path) -> None:
    try:
        path.unlink()
    except PermissionError:
        _set_writable(path)
        path.unlink()


def _remove_tree(path: Path) -> None:
    # Read-only staged originals may be hardlinks to immutable blobs. Clear the
    # directory entry attributes before removal; blob attributes are restored by
    # _prune_unreferenced_blobs for every retained digest.
    try:
        children = list(path.rglob("*"))
    except OSError:
        children = []
    for child in children:
        if not child.is_symlink():
            _set_writable(child)
    _set_writable(path)
    shutil.rmtree(path)


def _physical_tree_size(path: Path) -> int:
    """Count each hardlinked file identity once, excluding transient locks."""
    total = 0
    seen: set[tuple[Any, ...]] = set()
    try:
        children = path.rglob("*")
        for child in children:
            if _is_lock_metadata_path(child) or not child.is_file():
                continue
            try:
                info = child.stat()
            except OSError:
                continue
            inode = int(getattr(info, "st_ino", 0) or 0)
            device = int(getattr(info, "st_dev", 0) or 0)
            identity: tuple[Any, ...] = ("inode", device, inode) if inode else ("path", str(child.resolve()))
            if identity in seen:
                continue
            seen.add(identity)
            total += info.st_size
    except OSError:
        return total
    return total


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file() and not _is_lock_metadata_path(child):
                try:
                    total += child.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _is_lock_metadata_path(path: Path) -> bool:
    name = path.name
    return name.endswith(".lock") or name.endswith(".lock.guard")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


@contextmanager
def _path_lock(path: Path, *, timeout_seconds: float = 30.0) -> Iterator[None]:
    with short_process_lock(
        path,
        timeout_seconds=timeout_seconds,
        stale_after_seconds=120.0,
        timeout_label="file workspace lock",
    ):
        yield


def _has_active_lock(path: Path) -> bool:
    for lock_path in path.rglob("*.lock"):
        if not pid_lock_file_is_stale(lock_path, max_age_seconds=120.0):
            return True
    return False

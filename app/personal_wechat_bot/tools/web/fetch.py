from __future__ import annotations

import hashlib
import re
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from app.personal_wechat_bot.domain.models import ToolCallRequest, ToolCallResult
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.base import ToolManifest
from app.personal_wechat_bot.conversation.session_store import DEFAULT_SESSION_ID


class WebFetchTool:
    manifest = ToolManifest(
        name="web.fetch",
        description="Fetch a public http(s) page and save extracted text for ledger annotation.",
        supports_async=False,
    )

    def __init__(
        self,
        output_dir: str | Path,
        file_index: FileIndex,
        *,
        timeout_seconds: float = 20.0,
        max_bytes: int = 2 * 1024 * 1024,
        file_workspace: Any | None = None,
        attachment_parser: Any | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.file_index = file_index
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.file_workspace = file_workspace
        self.attachment_parser = attachment_parser

    def run(self, request: ToolCallRequest) -> ToolCallResult:
        url = str(request.arguments.get("url") or request.arguments.get("input_url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="blocked",
                summary="web.fetch requires an http(s) url",
                error="invalid_url",
            )

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wechat-agent-local/0.1"})
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read(self.max_bytes + 1)
                content_type = response.headers.get("content-type", "")
        except Exception as exc:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="failed",
                summary="web.fetch failed",
                error=f"{type(exc).__name__}: {exc}",
            )

        truncated = len(raw) > self.max_bytes
        raw = raw[: self.max_bytes]
        url_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        content_kind = _content_kind(url, content_type, raw)
        task = str(request.arguments.get("task") or "").strip()

        if content_kind == "file":
            suffix = _suffix_for_url_or_type(url, content_type) or ".bin"
            output = self.output_dir / f"{url_id}{suffix}"
            output.write_bytes(raw)
            return self._file_result(
                request,
                url=url,
                url_id=url_id,
                content_type=content_type,
                output=output,
                suffix=suffix,
                truncated=truncated,
                task=task,
            )

        if content_kind != "text":
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="blocked",
                summary="Cannot read the requested web resource type yet",
                error=f"unsupported_content_type:{content_type or 'unknown'}",
                payload={"url_id": url_id, "url": url, "content_type": content_type, "content_kind": content_kind},
            )

        charset = _charset(content_type) or "utf-8"
        text = raw.decode(charset, errors="replace")
        if "html" in content_type.lower() or "<html" in text[:1000].lower():
            text = _html_to_text(text)
        else:
            text = _normalize_text(text)

        output = self.output_dir / f"{url_id}.md"
        output.write_text(
            f"# Web Fetch\n\nurl: {url}\ncontent_type: {content_type}\ntruncated: {str(truncated).lower()}\n\n{text}\n",
            encoding="utf-8",
        )
        file_id = self.file_index.add(output, source=self.manifest.name, original_name=output.name)
        note = _task_note(url=url, text=text, task=task, truncated=truncated)
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=self.manifest.name,
            status="completed",
            summary=note,
            output_refs=[str(output)],
            payload={
                "file_id": file_id,
                "url_id": url_id,
                "url": url,
                "content_type": content_type,
                "content_kind": content_kind,
                "truncated": truncated,
                "text": note,
            },
        )

    def _file_result(
        self,
        request: ToolCallRequest,
        *,
        url: str,
        url_id: str,
        content_type: str,
        output: Path,
        suffix: str,
        truncated: bool,
        task: str,
    ) -> ToolCallResult:
        original_name = _download_name(url, suffix=suffix, fallback=f"{url_id}{suffix}")
        file_id = self.file_index.add(output, source=self.manifest.name, original_name=original_name)
        payload: dict[str, Any] = {
            "file_id": file_id,
            "url_id": url_id,
            "url": url,
            "content_type": content_type,
            "content_kind": "file",
            "truncated": truncated,
        }
        output_refs = [str(output)]
        note = _file_note(url=url, content_type=content_type, path=output, truncated=truncated, task=task)
        if self.file_workspace is None or self.attachment_parser is None:
            payload["text"] = note
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="completed",
                summary=note,
                output_refs=output_refs,
                payload=payload,
            )

        try:
            session_id = str(request.arguments.get("session_id") or DEFAULT_SESSION_ID).strip() or DEFAULT_SESSION_ID
            chat_title = str(request.arguments.get("chat_title") or "").strip()
            staged = self.file_workspace.stage_file(
                output,
                conversation_id=request.conversation_id,
                session_id=session_id,
                original_name=original_name,
                kind="file",
                source=self.manifest.name,
                chat_title=chat_title,
            )
            file_id = self.file_index.add(staged.staged_path, source="file_workspace", original_name=original_name)
            parse_result = self.file_workspace.parse_or_get_cached(
                staged,
                self.attachment_parser,
                embedded_media_ocr=getattr(self.attachment_parser, "ocr_engine", None),
                embedded_media_asr=getattr(self.attachment_parser, "asr_engine", None),
            )
            artifacts = _workspace_artifacts(staged)
            note = _file_workflow_note(
                url=url,
                content_type=content_type,
                file_id=file_id,
                parse_result=parse_result,
                artifacts=artifacts,
                task=task,
                truncated=truncated,
            )
            output_refs = [str(artifacts.get("content_path") or output), str(output)]
            payload.update(
                {
                    "file_id": file_id,
                    "workspace": {
                        "conversation_id": staged.conversation_id,
                        "session_id": staged.session_id,
                        "manifest_path": staged.manifest_path,
                        "staged_path": staged.staged_path,
                        "derived_dir": staged.derived_dir,
                    },
                    "parse": {
                        "status": parse_result.status,
                        "kind": parse_result.kind,
                        "summary": parse_result.summary,
                        "error": parse_result.error,
                    },
                    "artifacts": artifacts,
                }
            )
        except Exception as exc:
            note = (
                "Downloaded a file-like URL, but the local file workflow could not parse it.\n"
                f"URL: {url}\n"
                f"File: {output.name}\n"
                f"Error: {type(exc).__name__}: {exc}"
            )
            payload["parse_error"] = f"{type(exc).__name__}: {exc}"
        payload["text"] = note
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=self.manifest.name,
            status="completed",
            summary=note,
            output_refs=output_refs,
            payload=payload,
        )


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() in {"script", "style", "noscript"}:
            self.skip_depth += 1
        if tag.lower() in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str):
        if tag.lower() in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
        if tag.lower() in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str):
        if self.skip_depth:
            return
        stripped = data.strip()
        if stripped:
            self.parts.append(stripped)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return _normalize_text(" ".join(parser.parts))


def _normalize_text(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _charset(content_type: str) -> str:
    match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
    return match.group(1).strip("\"'") if match else ""


def _content_kind(url: str, content_type: str, raw: bytes) -> str:
    lowered_type = content_type.lower()
    suffix = _suffix_for_url_or_type(url, content_type)
    if suffix in _FILE_SUFFIXES or lowered_type.startswith(("application/pdf", "image/", "audio/", "video/")):
        return "file"
    if "html" in lowered_type or lowered_type.startswith("text/") or "json" in lowered_type or "xml" in lowered_type:
        return "text"
    if raw.startswith(b"%PDF"):
        return "file"
    if raw[:1000].lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return "text"
    return "unsupported"


def _suffix_for_url_or_type(url: str, content_type: str) -> str:
    suffix = Path(unquote(urlparse(url).path)).suffix.lower()
    if suffix:
        return suffix[:16]
    lowered = content_type.lower().split(";", 1)[0].strip()
    return {
        "application/pdf": ".pdf",
        "text/csv": ".csv",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }.get(lowered, "")


def _task_note(*, url: str, text: str, task: str, truncated: bool) -> str:
    preview = _compact(text, 1400) or "(empty page text)"
    task_line = f"Task: {_compact(task, 260)}\n" if task else ""
    truncated_line = "\nNote: page text was truncated at the download limit." if truncated else ""
    return f"Read web page for the current task.\n{task_line}URL: {url}\nPreview: {preview}{truncated_line}".strip()


def _file_note(*, url: str, content_type: str, path: Path, truncated: bool, task: str) -> str:
    task_line = f"\nTask: {_compact(task, 260)}" if task else ""
    truncated_line = "\nNote: file bytes were truncated at the download limit." if truncated else ""
    return (
        "Detected a file-like URL and saved it locally."
        f"{task_line}\nURL: {url}\nContent-Type: {content_type or 'unknown'}\nFile: {path.name}{truncated_line}"
    ).strip()


def _file_workflow_note(
    *,
    url: str,
    content_type: str,
    file_id: str,
    parse_result: Any,
    artifacts: dict[str, Any],
    task: str,
    truncated: bool,
) -> str:
    task_line = f"Task: {_compact(task, 260)}\n" if task else ""
    ai_summary = _usable_ai_summary(artifacts)
    summary = ai_summary or str(getattr(parse_result, "summary", "") or "").strip()
    preview = str(getattr(parse_result, "context_text", "") or "").strip()
    if not preview:
        preview = str(getattr(parse_result, "text", "") or "").strip()
    counters = _artifact_counter_line(artifacts)
    truncated_line = "\nNote: file bytes were truncated at the download limit." if truncated else ""
    return (
        "Read file-like URL through the local file workflow.\n"
        f"{task_line}"
        f"Content-Type: {content_type or 'unknown'}\n"
        f"file_id: {file_id}\n"
        f"parse_status: {getattr(parse_result, 'status', '')} kind={getattr(parse_result, 'kind', '')}\n"
        f"{counters}"
        f"Summary: {_compact(summary, 700) or '(no summary)'}\n"
        f"Preview: {_compact(preview, 1400) or '(no readable text preview)'}"
        f"{truncated_line}"
    ).strip()


def _artifact_counter_line(artifacts: dict[str, Any]) -> str:
    parts = []
    for key, label in (
        ("char_count", "chars"),
        ("chunk_count", "chunks"),
        ("table_chunk_count", "tables"),
        ("media_ocr_count", "ocr"),
        ("media_asr_count", "asr"),
    ):
        value = int(artifacts.get(key, 0) or 0)
        if value:
            parts.append(f"{label}={value}")
    return f"Artifacts: {' '.join(parts)}\n" if parts else ""


def _workspace_artifacts(staged: Any) -> dict[str, Any]:
    derived_dir = Path(str(staged.derived_dir))
    analysis = _read_json(derived_dir / "analysis.json", {})
    if not isinstance(analysis, dict):
        analysis = {}
    return {
        "content_path": str(derived_dir / "content.md"),
        "full_text_path": str(derived_dir / "full_text.md") if (derived_dir / "full_text.md").is_file() else "",
        "analysis_path": str(derived_dir / "analysis.json"),
        "parse_result_path": str(derived_dir / "parse_result.json"),
        "ai_analysis_status": str(analysis.get("ai_analysis_status", "")),
        "ai_summary": str(analysis.get("ai_summary", "")),
        "char_count": int(analysis.get("char_count", 0) or 0),
        "preview_char_count": int(analysis.get("preview_char_count", 0) or 0),
        "chunk_count": int(analysis.get("chunk_count", 0) or 0),
        "table_chunk_count": int(analysis.get("table_chunk_count", 0) or 0),
        "media_extract_count": int(analysis.get("media_extract_count", 0) or 0),
        "media_ocr_status": str(analysis.get("media_ocr_status", "")),
        "media_ocr_count": int(analysis.get("media_ocr_count", 0) or 0),
        "media_asr_status": str(analysis.get("media_asr_status", "")),
        "media_asr_count": int(analysis.get("media_asr_count", 0) or 0),
    }


def _usable_ai_summary(artifacts: dict[str, Any]) -> str:
    if str(artifacts.get("ai_analysis_status", "")).strip() != "analyzed":
        return ""
    summary = str(artifacts.get("ai_summary", "")).strip()
    if "fake_llm.completed" in summary or "PLAN:" in summary or "MONITOR:" in summary:
        return ""
    return summary


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _download_name(url: str, *, suffix: str, fallback: str) -> str:
    name = Path(unquote(urlparse(url).path)).name.strip()
    if not name:
        return fallback
    if suffix and not name.lower().endswith(suffix.lower()):
        name += suffix
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name)[:180] or fallback


def _compact(text: str, max_chars: int) -> str:
    normalized = _normalize_text(text)
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "..."


_FILE_SUFFIXES = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".csv",
    ".ppt",
    ".pptx",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".wma",
    ".amr",
    ".silk",
    ".txt",
    ".md",
}

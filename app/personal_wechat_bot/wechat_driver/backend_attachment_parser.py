from __future__ import annotations

import base64
import json
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from app.personal_wechat_bot.vision.ocr import OcrEngine, OcrResult
from app.personal_wechat_bot.voice.asr import AsrEngine, LocalAsrSubprocessEngine


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".wma", ".amr", ".silk"}
PRESENTATION_SUFFIXES = {".ppt", ".pptx"}
ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}
APPLICATION_SUFFIXES = {".exe", ".msi", ".apk", ".app", ".dmg", ".bat", ".cmd", ".ps1", ".scr"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v"}


@dataclass(frozen=True)
class AttachmentParseResult:
    status: str
    kind: str
    summary: str
    text: str = ""
    error: str = ""
    context_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class BackendAttachmentParser:
    def __init__(
        self,
        ocr_engine: OcrEngine | None = None,
        asr_engine: AsrEngine | None = None,
        max_preview_chars: int = 2000,
        worker_python: str | Path | None = None,
    ):
        self.ocr_engine = ocr_engine
        self.asr_engine = asr_engine or LocalAsrSubprocessEngine()
        self.max_preview_chars = max_preview_chars
        self.worker_python = Path(worker_python) if worker_python else _default_worker_python()

    def parse(self, path: str | Path) -> AttachmentParseResult:
        file_path = Path(path)
        suffix = file_path.suffix.lower()
        try:
            if suffix in {".txt", ".md"}:
                return self._parse_text(file_path)
            if suffix == ".docx":
                return self._parse_docx(file_path)
            if suffix == ".pdf":
                return self._parse_worker(file_path, "pdf")
            if suffix in {".xlsx", ".xlsm", ".csv"}:
                return self._parse_worker(file_path, "spreadsheet")
            if suffix in PRESENTATION_SUFFIXES:
                return _unsupported_placeholder(file_path, "presentation", "幻灯片")
            if suffix in ARCHIVE_SUFFIXES:
                return _unsupported_placeholder(file_path, "archive", "压缩文件")
            if suffix in APPLICATION_SUFFIXES:
                return _unsupported_placeholder(file_path, "application", "应用程序")
            if suffix in VIDEO_SUFFIXES:
                return _unsupported_placeholder(file_path, "video", "视频")
            if suffix in IMAGE_SUFFIXES:
                return self._parse_image(file_path)
            if suffix in AUDIO_SUFFIXES:
                return self._parse_audio(file_path)
            return AttachmentParseResult(
                "skipped",
                "file",
                f"暂不解析此类附件：{suffix or 'unknown'}",
                _unsupported_text(file_path, "file", "暂不支持的文件"),
            )
        except Exception as exc:
            return AttachmentParseResult(
                "failed",
                _kind_for_suffix(suffix),
                "附件解析失败",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _parse_text(self, path: Path) -> AttachmentParseResult:
        text = _normalize_text(path.read_text(encoding="utf-8-sig", errors="replace"))
        if not text:
            return AttachmentParseResult("empty", "text", "文本附件为空")
        return AttachmentParseResult("parsed", "text", "已读取文本附件", text, context_text=_preview(text, self.max_preview_chars))

    def _parse_docx(self, path: Path) -> AttachmentParseResult:
        text = _normalize_text(_read_docx_text(path))
        if not text:
            return AttachmentParseResult("empty", "docx", "DOCX 未提取到文本")
        return AttachmentParseResult("parsed", "docx", "已提取 DOCX 文本", text, context_text=_preview(text, self.max_preview_chars))

    def _parse_image(self, path: Path) -> AttachmentParseResult:
        if self.ocr_engine is None:
            return _image_placeholder(path, "OCR 引擎未启用")
        ocr_result = _run_structured_ocr(self.ocr_engine, path)
        text = _normalize_text(ocr_result.text)
        if not text:
            filter_reason = str((getattr(ocr_result, "metadata", {}) or {}).get("filter_reason", "")).strip()
            reason = "likely sticker/emoji image; OCR single-character false positive suppressed" if filter_reason else "OCR produced no usable text"
            placeholder = _image_placeholder(path, reason, status="empty")
            return AttachmentParseResult(
                placeholder.status,
                placeholder.kind,
                placeholder.summary,
                placeholder.text,
                error=placeholder.error,
                context_text=placeholder.context_text,
                metadata={"ocr": _ocr_result_payload(ocr_result)},
            )
            return _image_placeholder(path, "OCR 未识别到有效文本", status="empty")
        return AttachmentParseResult(
            "parsed",
            "image",
            "已完成图片 OCR",
            text,
            context_text=_preview(text, self.max_preview_chars),
            metadata={"ocr": _ocr_result_payload(ocr_result)},
        )

    def _parse_audio(self, path: Path) -> AttachmentParseResult:
        transcript = self.asr_engine.transcribe(path) if self.asr_engine is not None else None
        if transcript is not None and transcript.status == "transcribed" and transcript.text.strip():
            return AttachmentParseResult(
                "parsed",
                "audio",
                f"已完成本地 ASR 转写 backend={transcript.backend} model={transcript.model}".strip(),
                _normalize_text(transcript.text),
                context_text=_preview(transcript.text, self.max_preview_chars),
                metadata={
                    "asr": {
                        "status": transcript.status,
                        "backend": transcript.backend,
                        "model": transcript.model,
                        "language": transcript.language,
                    }
                },
            )
        error = transcript.error if transcript is not None else "local_asr_not_configured"
        backend = transcript.backend if transcript is not None else "local_asr_subprocess"
        bytes_note = f" bytes={path.stat().st_size}" if path.exists() else ""
        status = transcript.status if transcript is not None else "blocked"
        if status == "empty":
            # Ran cleanly, no speech detected: this is a definitive (empty) result,
            # not an error. Surface it as a distinct placeholder.
            return AttachmentParseResult(
                "empty",
                "audio",
                f"音频已保存到文件中间层，本地 ASR 未识别到语音内容 backend={backend}{bytes_note}",
                _audio_placeholder_text(path, "ASR 未识别到语音内容"),
            )
        if status == "failed":
            summary = f"音频已保存到文件中间层，本地 ASR 转写失败 backend={backend}{bytes_note}"
        else:
            summary = f"音频已保存到文件中间层，本地 ASR 暂不可用 backend={backend}{bytes_note}"
        return AttachmentParseResult(
            "skipped",
            "audio",
            summary,
            _audio_placeholder_text(path, summary),
            error=error or "local_asr_not_configured",
        )

    def _parse_worker(self, path: Path, kind: str) -> AttachmentParseResult:
        if self.worker_python is None or not self.worker_python.exists():
            return AttachmentParseResult("skipped", kind, f"{kind} 已登记，解析 worker Python 不可用")
        worker = Path(__file__).resolve().parents[3] / "scripts" / "attachment_extract_worker.py"
        completed = subprocess.run(
            [str(self.worker_python), str(worker), str(path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            return AttachmentParseResult("failed", kind, f"{kind} 解析失败", error=detail)
        payload_line = ""
        for line in completed.stdout.splitlines():
            if line.startswith("ATTACHMENT_EXTRACT_JSON:"):
                payload_line = line.removeprefix("ATTACHMENT_EXTRACT_JSON:")
        if not payload_line:
            return AttachmentParseResult("failed", kind, f"{kind} 解析失败", error="missing worker payload")
        payload = json.loads(payload_line)
        if not payload.get("ok"):
            return AttachmentParseResult("failed", kind, f"{kind} 解析失败", error=str(payload.get("error", "")))
        text = _read_worker_text(payload)
        if not text:
            return AttachmentParseResult("empty", kind, f"{kind} 未提取到文本")
        summary = "已提取 PDF 文本" if kind == "pdf" else "已提取表格文本"
        return AttachmentParseResult("parsed", kind, summary, text, context_text=_preview(text, self.max_preview_chars))


def _read_docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as docx:
        try:
            xml = docx.read("word/document.xml")
        except KeyError:
            return ""
    root = ElementTree.fromstring(xml)
    paragraphs: list[str] = []
    for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        texts = [
            node.text or ""
            for node in paragraph.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
        ]
        line = "".join(texts).strip()
        if line:
            paragraphs.append(line)
    return "\n".join(paragraphs)


def _run_structured_ocr(engine: OcrEngine, image_path: Path) -> OcrResult:
    read_structured = getattr(engine, "read_structured", None)
    if callable(read_structured):
        return read_structured(image_path)
    return OcrResult(text=engine.read_text(image_path), items=[])


def _ocr_result_payload(result: OcrResult) -> dict[str, object]:
    return {
        "cache_version": 3,
        "text": result.text,
        "item_count": result.item_count,
        "metadata": dict(getattr(result, "metadata", {}) or {}),
        "items": [
            {
                "text": item.text,
                "score": round(float(item.score), 4),
                "box": item.box,
                "backend": getattr(item, "backend", ""),
            }
            for item in result.items
        ],
    }


def _normalize_text(text: str) -> str:
    text = text.lstrip("\ufeff")
    return "\n".join(line.strip().lstrip("\ufeff") for line in text.splitlines() if line.strip())


def _preview(text: str, max_chars: int) -> str:
    normalized = _normalize_text(text)
    if max_chars <= 0:
        return ""
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."


def _read_worker_text(payload: dict[str, object]) -> str:
    text_path = str(payload.get("text_path", "") or "").strip()
    if text_path:
        path = Path(text_path)
        try:
            return _normalize_text(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return ""
    text_b64 = str(payload.get("text_b64", "") or "")
    if not text_b64:
        return ""
    return _normalize_text(base64.b64decode(text_b64.encode("ascii")).decode("utf-8", errors="replace"))


def _unsupported_placeholder(path: Path, kind: str, label: str) -> AttachmentParseResult:
    return AttachmentParseResult(
        "skipped",
        kind,
        f"{label}已登记到本地文件中间层；出于稳定性和安全性，当前不会解析、解压或执行此类文件。",
        _unsupported_text(path, kind, label),
    )


def _image_placeholder(path: Path, reason: str, *, status: str = "skipped") -> AttachmentParseResult:
    label = "图片/表情"
    return AttachmentParseResult(
        status,
        "image",
        f"{label}已登记到本地文件中间层；{reason}，当前以占位符进入对话上下文。",
        _unsupported_text(path, "image", label, reason=reason),
    )


def _audio_placeholder_text(path: Path, reason: str) -> str:
    return _unsupported_text(path, "audio", "语音/音频", reason=reason)


def _unsupported_text(path: Path, kind: str, label: str, *, reason: str = "") -> str:
    size = path.stat().st_size if path.exists() else 0
    reason_line = f"\n- 原因: {reason}" if reason else ""
    return (
        "[附件占位符]\n"
        f"- 类型: {label}\n"
        f"- kind: {kind}\n"
        f"- 文件名: {path.name}\n"
        f"- 本地路径: {path}\n"
        f"- 字节数: {size}"
        f"{reason_line}\n"
        "- 说明: agent 当前不会读取、解析、解压或执行该文件；如需处理，请用户提供可解析格式或明确授权新的本地解析策略。"
    )


def _kind_for_suffix(suffix: str) -> str:
    if suffix in {".txt", ".md"}:
        return "text"
    if suffix == ".docx":
        return "docx"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".xlsx", ".xlsm", ".csv"}:
        return "spreadsheet"
    if suffix in PRESENTATION_SUFFIXES:
        return "presentation"
    if suffix in ARCHIVE_SUFFIXES:
        return "archive"
    if suffix in APPLICATION_SUFFIXES:
        return "application"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    return "file"


def _default_worker_python() -> Path | None:
    repo_root = Path(__file__).resolve().parents[3]
    candidates = [
        repo_root / "vendor" / "ocr-python" / "Scripts" / "python.exe",
        Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "python" / "python.exe",
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None

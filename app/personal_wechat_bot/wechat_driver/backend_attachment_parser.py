from __future__ import annotations

import base64
import json
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from app.personal_wechat_bot.vision.ocr import OcrEngine
from app.personal_wechat_bot.voice.asr import AsrEngine, LocalAsrSubprocessEngine


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".wma", ".amr", ".silk"}


@dataclass(frozen=True)
class AttachmentParseResult:
    status: str
    kind: str
    summary: str
    text: str = ""
    error: str = ""


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
            if suffix in IMAGE_SUFFIXES:
                return self._parse_image(file_path)
            if suffix in AUDIO_SUFFIXES:
                return self._parse_audio(file_path)
            return AttachmentParseResult("skipped", "file", f"暂不解析此类附件：{suffix or 'unknown'}")
        except Exception as exc:
            return AttachmentParseResult(
                "failed",
                _kind_for_suffix(suffix),
                "附件解析失败",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _parse_text(self, path: Path) -> AttachmentParseResult:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        preview = _preview(text, self.max_preview_chars)
        if not preview:
            return AttachmentParseResult("empty", "text", "文本附件为空")
        return AttachmentParseResult("parsed", "text", "已读取文本附件预览", preview)

    def _parse_docx(self, path: Path) -> AttachmentParseResult:
        text = _read_docx_text(path)
        preview = _preview(text, self.max_preview_chars)
        if not preview:
            return AttachmentParseResult("empty", "docx", "DOCX 未提取到文本")
        return AttachmentParseResult("parsed", "docx", "已提取 DOCX 文本预览", preview)

    def _parse_image(self, path: Path) -> AttachmentParseResult:
        if self.ocr_engine is None:
            return AttachmentParseResult("skipped", "image", "图片已登记，OCR 引擎未启用")
        text = self.ocr_engine.read_text(path)
        preview = _preview(text, self.max_preview_chars)
        if not preview:
            return AttachmentParseResult("empty", "image", "图片 OCR 未识别到文本")
        return AttachmentParseResult("parsed", "image", "已完成图片 OCR 预览", preview)

    def _parse_audio(self, path: Path) -> AttachmentParseResult:
        transcript = self.asr_engine.transcribe(path) if self.asr_engine is not None else None
        if transcript is not None and transcript.status == "transcribed" and transcript.text.strip():
            return AttachmentParseResult(
                "parsed",
                "audio",
                f"已完成本地 ASR 转写 backend={transcript.backend} model={transcript.model}".strip(),
                _preview(transcript.text, self.max_preview_chars),
            )
        error = transcript.error if transcript is not None else "local_asr_not_configured"
        backend = transcript.backend if transcript is not None else "local_asr_subprocess"
        bytes_note = f" bytes={path.stat().st_size}" if path.exists() else ""
        if transcript is not None and transcript.status == "failed":
            summary = f"音频已保存到文件中间层，本地 ASR 转写失败 backend={backend}{bytes_note}"
        else:
            summary = f"音频已保存到文件中间层，本地 ASR 暂不可用 backend={backend}{bytes_note}"
        return AttachmentParseResult(
            "skipped",
            "audio",
            summary,
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
        text_b64 = str(payload.get("text_b64", ""))
        text = base64.b64decode(text_b64.encode("ascii")).decode("utf-8") if text_b64 else ""
        preview = _preview(text, self.max_preview_chars)
        if not preview:
            return AttachmentParseResult("empty", kind, f"{kind} 未提取到文本")
        summary = "已提取 PDF 文本预览" if kind == "pdf" else "已提取表格文本预览"
        return AttachmentParseResult("parsed", kind, summary, preview)


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


def _preview(text: str, max_chars: int) -> str:
    text = text.lstrip("\ufeff")
    normalized = "\n".join(line.strip().lstrip("\ufeff") for line in text.splitlines() if line.strip())
    if max_chars <= 0:
        return ""
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"


def _kind_for_suffix(suffix: str) -> str:
    if suffix in {".txt", ".md"}:
        return "text"
    if suffix == ".docx":
        return "docx"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".xlsx", ".xlsm", ".csv"}:
        return "spreadsheet"
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

"""LLM-backed analysis of parsed attachments.

The file workspace parses attachments into raw text + structured artifacts, but
that is mechanical: it never *understands* the file. This module adds an optional
analysis pass that asks the chat LLM for a short summary, key points, and topic
labels, which are merged into ``analysis.json`` so an agent reading the ledger's
``[file_index]`` can grasp a file without re-reading the whole content.

The analyzer is optional and degrades gracefully: if no LLM is wired in, or the
call fails, or the file has no extractable text, it returns a ``skipped``/``error``
payload and the workspace falls back to mechanical metadata only. It never raises
into the parse path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol


ANALYSIS_MODEL_MAX_INPUT_CHARS = 12000


class SupportsGenerateReply(Protocol):
    def generate_reply(self, prompt: str) -> str: ...


@dataclass(frozen=True)
class FileAnalysis:
    status: str  # analyzed | skipped | error
    summary: str = ""
    key_points: list[str] = None  # type: ignore[assignment]
    topics: list[str] = None  # type: ignore[assignment]
    model: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "key_points": list(self.key_points or []),
            "topics": list(self.topics or []),
            "model": self.model,
            "error": self.error,
        }


class FileAnalyzer(Protocol):
    def analyze(self, *, name: str, kind: str, text: str, extra: dict[str, Any] | None = None) -> FileAnalysis: ...


class LLMFileAnalyzer:
    """Analyze parsed file text with the chat LLM, returning a structured summary."""

    def __init__(self, llm: SupportsGenerateReply, *, model: str = "", max_input_chars: int = ANALYSIS_MODEL_MAX_INPUT_CHARS):
        self.llm = llm
        self.model = model
        self.max_input_chars = max_input_chars

    def analyze(self, *, name: str, kind: str, text: str, extra: dict[str, Any] | None = None) -> FileAnalysis:
        body = (text or "").strip()
        if not body:
            return FileAnalysis(status="skipped", model=self.model, error="no_text_to_analyze")
        prompt = self._build_prompt(name=name, kind=kind, text=body, extra=extra or {})
        try:
            raw = self.llm.generate_reply(prompt)
        except Exception as exc:  # never break the parse path on an LLM failure
            return FileAnalysis(status="error", model=self.model, error=f"{type(exc).__name__}: {exc}")
        parsed = _parse_analysis_json(raw)
        if not parsed:
            # The model answered but not as JSON: keep its prose as the summary
            # rather than throwing the work away.
            return FileAnalysis(status="analyzed", summary=_compact(raw.strip(), 1500), model=self.model)
        return FileAnalysis(
            status="analyzed",
            summary=_compact(str(parsed.get("summary", "")).strip(), 1500),
            key_points=[str(item).strip() for item in parsed.get("key_points", []) if str(item).strip()][:12],
            topics=[str(item).strip() for item in parsed.get("topics", []) if str(item).strip()][:8],
            model=self.model,
        )

    def _build_prompt(self, *, name: str, kind: str, text: str, extra: dict[str, Any]) -> str:
        clipped = text[: self.max_input_chars]
        truncated = len(text) > self.max_input_chars
        context = {
            "file_name": name,
            "file_kind": kind,
            "truncated": truncated,
            **({"hints": extra} if extra else {}),
        }
        return (
            "你是一个文件分析助手。请阅读下面的文件解析文本，输出该文件的要点分析。"
            "只返回 JSON，不要额外说明，格式：\n"
            '{"summary": "两三句话的中文摘要", '
            '"key_points": ["要点1", "要点2"], '
            '"topics": ["主题标签1", "主题标签2"]}\n'
            f"\n文件元信息：{json.dumps(context, ensure_ascii=False)}\n"
            f"\n文件内容：\n{clipped}"
        )


def _parse_analysis_json(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if not text:
        return {}
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def _compact(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"

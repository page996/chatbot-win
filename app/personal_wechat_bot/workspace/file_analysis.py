"""LLM-backed analysis of parsed attachments.

The file workspace parses attachments into raw text plus structured artifacts.
This module adds an optional analysis pass that asks the chat LLM for a short,
strict JSON summary. It never raises into the parse path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol



ANALYSIS_MODEL_MAX_INPUT_CHARS = 12000


class SupportsGenerateReply(Protocol):
    def generate_reply(self, prompt: str, *, workload: str = "interactive") -> str: ...


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
    """Analyze parsed file text with the chat LLM, returning structured JSON."""

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
            raw = self.llm.generate_reply(prompt, workload="background")
        except Exception as exc:
            return FileAnalysis(status="error", model=self.model, error=f"{type(exc).__name__}: {exc}")
        if _looks_like_fake_chat_reply(raw):
            return FileAnalysis(status="skipped", model=self.model, error="fake_llm_output_ignored")
        parsed = _parse_analysis_json(raw)
        if not parsed:
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
        context = {
            "file_name": name,
            "file_kind": kind,
            "truncated": len(text) > self.max_input_chars,
            **({"hints": extra} if extra else {}),
        }
        return (
            "You are analyzing parsed attachment content for a WeChat agent.\n"
            "Return JSON only. Do not include markdown fences, PLAN, MONITOR, SUMMARY, or chat filler.\n"
            "The JSON object must follow this exact schema:\n"
            '{"summary":"2-3 sentence Chinese summary of this file",'
            '"key_points":["specific point 1","specific point 2"],'
            '"topics":["short topic label 1","short topic label 2"]}\n'
            f"\nfile_metadata={json.dumps(context, ensure_ascii=False, sort_keys=True)}\n"
            "\nparsed_file_text:\n"
            f"{clipped}"
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


def _looks_like_fake_chat_reply(content: str) -> bool:
    text = str(content or "")
    return "fake_llm.completed" in text or ("PLAN:" in text and "MONITOR:" in text and "SUMMARY:" in text)


def _compact(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."

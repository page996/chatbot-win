from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class OcrSnapshotParseResult:
    chat_title: str
    sender_name: str
    message: str
    raw_lines: tuple[str, ...]
    normalized_lines: tuple[str, ...]
    attachments: tuple[dict[str, str], ...] = field(default_factory=tuple)
    voice_transcripts: tuple[dict[str, str], ...] = field(default_factory=tuple)
    status: str = "ok"
    reason: str = ""
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def to_snapshot(self) -> str:
        if self.status != "ok" or not self.chat_title or not self.sender_name or not self.message:
            return ""
        return f"[private] {self.chat_title} | {self.sender_name} |  | {self.message}"

    def to_snapshots(self) -> list[str]:
        if self.status != "ok":
            return []
        snapshots: list[str] = []
        if self.message:
            snapshots.append(self.to_snapshot())
        for transcript in self.voice_transcripts:
            text = str(transcript.get("text", "")).strip()
            if not text:
                continue
            duration = str(transcript.get("duration", "")).strip()
            suffix = f" duration={duration}" if duration else ""
            snapshots.append(
                f"[private] {self.chat_title} | {self.sender_name} |  | "
                f"[OCR_VOICE_TRANSCRIPT] {text}{suffix}"
            )
        for attachment in self.attachments:
            name = str(attachment.get("name", "")).strip()
            if not name:
                continue
            kind = str(attachment.get("kind", "file")).strip() or "file"
            size = str(attachment.get("size", "")).strip()
            suffix = f" size={size}" if size else ""
            snapshots.append(f"[private] {self.chat_title} | {self.sender_name} |  | [OCR_CONTEXT][OCR附件卡片] {name} kind={kind}{suffix}")
        return snapshots


def ocr_text_to_snapshot(
    ocr_text: str,
    preferred_chat_title: str = "",
    ignored_names: Iterable[str] | None = None,
) -> str:
    result = parse_ocr_snapshot(
        ocr_text,
        preferred_chat_title=preferred_chat_title,
        ignored_names=ignored_names,
    )
    return result.to_snapshot() if result is not None else ""


def ocr_text_to_snapshots(
    ocr_text: str,
    preferred_chat_title: str = "",
    ignored_names: Iterable[str] | None = None,
) -> list[str]:
    result = parse_ocr_snapshot(
        ocr_text,
        preferred_chat_title=preferred_chat_title,
        ignored_names=ignored_names,
    )
    return result.to_snapshots() if result is not None else []


def parse_ocr_snapshot(
    ocr_text: str,
    preferred_chat_title: str = "",
    ignored_names: Iterable[str] | None = None,
) -> OcrSnapshotParseResult | None:
    raw_lines = tuple(line.strip() for line in ocr_text.splitlines() if line.strip())
    normalized_lines = tuple(_normalize_line(line) for line in raw_lines)
    normalized_lines = tuple(line for line in normalized_lines if line)
    if not normalized_lines:
        return None

    chat_title = _normalize_line(preferred_chat_title) or _guess_chat_title(normalized_lines)
    if not chat_title:
        return None

    ignored = _ignored_name_set(ignored_names)
    ambiguity = _detect_ambiguous_truncated_preview(normalized_lines, chat_title, ignored)
    if ambiguity is not None:
        return OcrSnapshotParseResult(
            chat_title=chat_title,
            sender_name=chat_title,
            message="",
            attachments=(),
            raw_lines=raw_lines,
            normalized_lines=normalized_lines,
            status="ambiguous_or_truncated",
            reason=ambiguity["reason"],
            evidence=tuple(ambiguity["evidence"]),
        )

    voice_transcripts = _extract_voice_transcripts(normalized_lines, chat_title, ignored)
    message, attachments = _guess_visible_context(normalized_lines, chat_title, ignored_names=ignored_names)
    message = _suppress_message_if_voice_transcript(message, voice_transcripts)
    if not message and not attachments and not voice_transcripts:
        return None

    return OcrSnapshotParseResult(
        chat_title=chat_title,
        sender_name=chat_title,
        message=message,
        attachments=attachments,
        voice_transcripts=voice_transcripts,
        raw_lines=raw_lines,
        normalized_lines=normalized_lines,
    )


_UI_NOISE_EXACT = {
    "Q搜索",
    "O搜索",
    "0搜索",
    "搜索",
    "文件传输助手",
    "微信",
    "通讯录",
    "发现",
    "我",
    "聊天信息",
    "发送",
    "按住说话",
    "表情",
    "更多",
    "PDF",
    "DOC",
    "DOCX",
    "XLS",
    "XLSX",
    "PPT",
    "PPTX",
}

_DEFAULT_IGNORED_NAMES = {
    # Observed account nickname in the current PAGE test window.
    "猪思",
}

_TIME_RE = re.compile(r"^(?:[01]?\d|2[0-3])[:：][0-5]\d$")
_DATE_TIME_RE = re.compile(
    r"^(?:昨天|今天|上午|下午|晚上|凌晨|早上|中午|\d{1,2}月\d{1,2}日|\d{4}[/-]\d{1,2}[/-]\d{1,2}).{0,12}$"
)
_FILE_NAME_RE = re.compile(r"^[\w .()\-\u4e00-\u9fff]+?\.(?:pdf|docx?|xlsx?|csv|pptx?|txt|zip|rar)$", re.I)
_FILE_SIZE_RE = re.compile(r"^\d+(?:\.\d+)?\s*(?:[KMGT]?B|[KMGT])$", re.I)
_VOICE_DURATION_RE = re.compile(r"^\d{1,3}\s*(?:''|\"|秒|s)$", re.I)
_VOICE_INLINE_TRANSCRIPT_RE = re.compile(
    r"^(?:语音转文字|转文字|转换为文字|已转文字|voice transcript|transcription)\s*[:：]\s*(?P<text>.+)$",
    re.I,
)
_VOICE_MARKER_EXACT = {
    "语音",
    "转文字",
    "语音转文字",
    "转换为文字",
    "已转文字",
    "voice",
    "voice message",
    "transcription",
    "voice transcript",
}


def _normalize_line(value: str) -> str:
    value = value.replace("\u3000", " ")
    value = re.sub(r"\s+", " ", value.strip())
    return value


def _guess_chat_title(lines: tuple[str, ...]) -> str:
    for line in lines:
        if _is_noise_line(line, chat_title="", ignored_names=()):
            continue
        return line
    return ""


def _guess_visible_context(
    lines: tuple[str, ...],
    chat_title: str,
    ignored_names: Iterable[str] | None = None,
) -> tuple[str, tuple[dict[str, str], ...]]:
    ignored = _ignored_name_set(ignored_names)
    attachments = _extract_attachment_cards(lines)

    content_start = _content_start_index(lines, chat_title)
    scoped_lines = tuple(enumerate(lines[content_start:], start=content_start))
    blocks = _message_blocks(scoped_lines, chat_title, ignored)

    if not blocks and content_start:
        blocks = _message_blocks(tuple(enumerate(lines)), chat_title, ignored)
    if not blocks:
        return "", attachments

    best = max(blocks, key=lambda block: _block_score(block))
    return _join_message_lines(best[1]), attachments


def _extract_voice_transcripts(
    lines: tuple[str, ...],
    chat_title: str,
    ignored_names: set[str],
) -> tuple[dict[str, str], ...]:
    transcripts: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, line in enumerate(lines):
        inline = _VOICE_INLINE_TRANSCRIPT_RE.match(line)
        if inline:
            _append_voice_transcript(
                transcripts,
                seen,
                text=inline.group("text"),
                duration=_nearby_voice_duration(lines, index),
            )
            continue
        if not _is_voice_marker_line(line):
            continue
        text = _next_voice_transcript_line(lines, index, chat_title, ignored_names)
        if not text:
            continue
        _append_voice_transcript(
            transcripts,
            seen,
            text=text,
            duration=_nearby_voice_duration(lines, index),
        )
    return tuple(transcripts)


def _append_voice_transcript(
    transcripts: list[dict[str, str]],
    seen: set[str],
    *,
    text: str,
    duration: str,
) -> None:
    cleaned = _normalize_line(text)
    key = _compact_for_compare(cleaned)
    if not cleaned or not key or key in seen:
        return
    seen.add(key)
    payload = {
        "text": cleaned,
        "source": "wechat_builtin_voice_to_text_ocr",
        "status": "transcribed",
    }
    if duration:
        payload["duration"] = duration
    transcripts.append(payload)


def _next_voice_transcript_line(
    lines: tuple[str, ...],
    marker_index: int,
    chat_title: str,
    ignored_names: set[str],
) -> str:
    stop = min(len(lines), marker_index + 5)
    for candidate in lines[marker_index + 1 : stop]:
        if _is_voice_marker_line(candidate) or _looks_like_voice_duration(candidate):
            continue
        if _is_noise_line(candidate, chat_title=chat_title, ignored_names=ignored_names):
            continue
        if _is_attachment_card_line(candidate):
            continue
        if _looks_like_message_text(candidate):
            return candidate
    return ""


def _nearby_voice_duration(lines: tuple[str, ...], marker_index: int) -> str:
    start = max(0, marker_index - 2)
    stop = min(len(lines), marker_index + 3)
    for candidate in lines[start:stop]:
        if _looks_like_voice_duration(candidate):
            return candidate
    return ""


def _suppress_message_if_voice_transcript(message: str, voice_transcripts: tuple[dict[str, str], ...]) -> str:
    if not message or not voice_transcripts:
        return message
    normalized_message = _compact_for_compare(message)
    for transcript in voice_transcripts:
        normalized_transcript = _compact_for_compare(str(transcript.get("text", "")))
        if normalized_message and normalized_message == normalized_transcript:
            return ""
    return message


def _guess_last_message(
    lines: tuple[str, ...],
    chat_title: str,
    ignored_names: Iterable[str] | None = None,
) -> str:
    message, _attachments = _guess_visible_context(lines, chat_title, ignored_names=ignored_names)
    return message


def _content_start_index(lines: tuple[str, ...], chat_title: str) -> int:
    title_indexes = [index for index, line in enumerate(lines) if line == chat_title]
    if title_indexes:
        return title_indexes[-1] + 1
    return 0


def _ignored_name_set(ignored_names: Iterable[str] | None = None) -> set[str]:
    ignored = set(_DEFAULT_IGNORED_NAMES)
    ignored.update(_normalize_line(name) for name in (ignored_names or ()) if _normalize_line(name))
    return ignored


def _detect_ambiguous_truncated_preview(
    lines: tuple[str, ...],
    chat_title: str,
    ignored_names: set[str],
) -> dict[str, list[str] | str] | None:
    title_indexes = [index for index, line in enumerate(lines) if line == chat_title]
    for title_index in title_indexes:
        search_stop = min(len(lines), title_index + 8)
        for index in range(title_index + 1, search_stop):
            line = lines[index]
            if _is_attachment_card_line(line):
                break
            if _is_noise_line(line, chat_title=chat_title, ignored_names=ignored_names):
                continue
            if not _looks_like_truncated_preview(line):
                continue
            if _has_fuller_visible_version(lines, index, line, chat_title, ignored_names):
                continue
            evidence = [line]
            for later in lines[index + 1 :]:
                if len(evidence) >= 4:
                    break
                if _is_noise_line(later, chat_title=chat_title, ignored_names=ignored_names):
                    continue
                evidence.append(later)
            return {
                "reason": "ocr only saw a truncated left-list preview; open the chat pane or scroll to the full message bubble before replying",
                "evidence": evidence,
            }
    return None


def _looks_like_truncated_preview(line: str) -> bool:
    if not _contains_cjk(line):
        return False
    compact = _compact_for_compare(line)
    if len(compact) < 4:
        return False
    stripped = line.strip()
    if "..." in stripped or "…" in stripped:
        return True
    return stripped.endswith(".")


def _contains_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def _has_fuller_visible_version(
    lines: tuple[str, ...],
    preview_index: int,
    preview: str,
    chat_title: str,
    ignored_names: set[str],
) -> bool:
    preview_compact = _compact_for_compare(preview)
    if not preview_compact:
        return False
    for line in lines[preview_index + 1 :]:
        if _is_attachment_card_line(line):
            continue
        if _is_noise_line(line, chat_title=chat_title, ignored_names=ignored_names):
            continue
        candidate = _compact_for_compare(line)
        if len(candidate) > len(preview_compact) and candidate.startswith(preview_compact):
            return True
    return False


def _message_blocks(
    indexed_lines: tuple[tuple[int, str], ...],
    chat_title: str,
    ignored_names: set[str],
) -> list[tuple[int, tuple[str, ...]]]:
    blocks: list[tuple[int, tuple[str, ...]]] = []
    current: list[str] = []
    current_last_index = -1

    for index, line in indexed_lines:
        if _is_attachment_card_line(line):
            if current:
                blocks.append((current_last_index, _dedupe_message_lines(tuple(current))))
                current = []
            continue
        if _is_noise_line(line, chat_title=chat_title, ignored_names=ignored_names):
            if current:
                blocks.append((current_last_index, _dedupe_message_lines(tuple(current))))
                current = []
            continue
        current.append(line)
        current_last_index = index

    if current:
        blocks.append((current_last_index, _dedupe_message_lines(tuple(current))))

    return [(last_index, lines) for last_index, lines in blocks if _join_message_lines(lines)]


def _dedupe_message_lines(lines: tuple[str, ...]) -> tuple[str, ...]:
    kept: list[str] = []
    for line in lines:
        if any(line == item for item in kept):
            continue
        kept = [item for item in kept if not _is_redundant_fragment(item, line)]
        if any(_is_redundant_fragment(line, item) for item in kept):
            continue
        kept.append(line)
    return tuple(kept)


def _is_redundant_fragment(shorter: str, longer: str) -> bool:
    if len(shorter) >= len(longer):
        return False
    compact_shorter = _compact_for_compare(shorter)
    compact_longer = _compact_for_compare(longer)
    return bool(compact_shorter) and compact_longer.startswith(compact_shorter)


def _join_message_lines(lines: tuple[str, ...]) -> str:
    meaningful = [line for line in lines if _looks_like_message_text(line)]
    if not meaningful:
        return ""
    if len(meaningful) == 1:
        return meaningful[0]
    return "".join(meaningful)


def _block_score(block: tuple[int, tuple[str, ...]]) -> int:
    last_index, lines = block
    text = _join_message_lines(lines)
    return last_index * 20 + min(len(text), 120)


def _is_noise_line(line: str, chat_title: str, ignored_names: Iterable[str]) -> bool:
    if not line:
        return True
    if chat_title and line == chat_title:
        return True
    if line in _UI_NOISE_EXACT or line in ignored_names:
        return True
    if _is_attachment_card_line(line):
        return True
    if _is_voice_marker_line(line) or _looks_like_voice_duration(line):
        return True
    if "搜索" in line and len(line) <= 4:
        return True
    if _looks_like_time(line):
        return True
    if len(line) == 1:
        return True
    return False


def _looks_like_message_text(line: str) -> bool:
    if len(line) <= 1:
        return False
    return bool(_compact_for_compare(line))


def _is_voice_marker_line(line: str) -> bool:
    normalized = _normalize_line(line).lower()
    if normalized in _VOICE_MARKER_EXACT:
        return True
    compact = _compact_for_compare(normalized)
    return compact in {"语音", "转文字", "语音转文字", "转换为文字", "已转文字"}


def _looks_like_voice_duration(line: str) -> bool:
    return bool(_VOICE_DURATION_RE.match(_normalize_line(line)))


def _compact_for_compare(value: str) -> str:
    return re.sub(r"[\s，。！？、,.!?:：;；~～…]+", "", value)


def _looks_like_time(value: str) -> bool:
    if _TIME_RE.match(value):
        return True
    if _DATE_TIME_RE.match(value):
        return any(char.isdigit() for char in value) or value in {"昨天", "今天"}
    return False


def _is_attachment_card_line(line: str) -> bool:
    return bool(_FILE_NAME_RE.match(line) or _FILE_SIZE_RE.match(line) or line.upper() in _FILE_KIND_LABELS)


def _extract_attachment_cards(lines: tuple[str, ...]) -> tuple[dict[str, str], ...]:
    attachments: list[dict[str, str]] = []
    for index, line in enumerate(lines):
        if not _FILE_NAME_RE.match(line):
            continue
        kind = PathLikeSuffix(line)
        size = ""
        for nearby in lines[index + 1 : index + 4]:
            if _FILE_SIZE_RE.match(nearby):
                size = nearby
                break
        attachments.append({"name": line, "kind": kind, "size": size, "source": "ocr_file_card"})
    return tuple(attachments)


def PathLikeSuffix(name: str) -> str:
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else "file"
    return suffix or "file"


_FILE_KIND_LABELS = {"PDF", "DOC", "DOCX", "XLS", "XLSX", "PPT", "PPTX", "TXT", "CSV", "ZIP", "RAR"}

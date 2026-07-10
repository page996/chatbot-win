from __future__ import annotations

import json
import re
import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.conversation.ledger_context import as_payload, memory_dir_for_conversation
from app.personal_wechat_bot.conversation.session_store import DEFAULT_SESSION_ID
from app.personal_wechat_bot.domain.models import utc_now_iso


_MEMORY_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory-maintainer")


@dataclass(frozen=True)
class MemoryMaintenanceResult:
    conversation_id: str
    session_id: str
    memory_dir: str
    processed_count: int
    last_sequence: int
    summary_path: str
    preferences_path: str
    entities_path: str
    state_path: str
    status: str = "ok"


@dataclass
class MemoryDraft:
    summary_lines: list[str] = field(default_factory=list)
    file_lines: list[str] = field(default_factory=list)
    preferences: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    entities: dict[str, Any] = field(default_factory=dict)


class MemoryMaintainer:
    """Maintains session-scoped memory files derived from the conversation ledger.

    This first implementation is deterministic on purpose: it keeps the memory
    layer stable and testable while leaving room for an LLM summarizer later.
    """

    def __init__(
        self,
        ledger_store: ConversationLedgerStore,
        *,
        max_summary_lines: int = 80,
        max_text_chars_per_entry: int = 260,
        llm: Any | None = None,
        async_llm: bool = False,
    ):
        self.ledger_store = ledger_store
        self.max_summary_lines = max_summary_lines
        self.max_text_chars_per_entry = max_text_chars_per_entry
        self.llm = llm
        self.async_llm = async_llm

    def maintain(self, conversation_id: str, *, session_id: str = DEFAULT_SESSION_ID) -> MemoryMaintenanceResult:
        conversation_dir = self.ledger_store.conversation_markdown_path(conversation_id).parent
        memory_dir = memory_dir_for_conversation(conversation_dir, session_id)
        state = _read_json(memory_dir / "maintenance_state.json")
        last_sequence = int(state.get("last_sequence", 0) or 0)
        entries = [
            as_payload(entry)
            for entry in self.ledger_store.read_entries(conversation_id)
            if _entry_session_id(as_payload(entry)) == session_id
        ]
        active_signature = _active_signature(entries)
        new_entries = [item for item in entries if int(item.get("sequence", 0) or 0) > last_sequence]
        if not new_entries and state.get("active_signature") == active_signature and memory_dir.exists():
            return MemoryMaintenanceResult(
                conversation_id=conversation_id,
                session_id=session_id,
                memory_dir=str(memory_dir),
                processed_count=0,
                last_sequence=last_sequence,
                summary_path=str(memory_dir / "summary.md"),
                preferences_path=str(memory_dir / "preferences.json"),
                entities_path=str(memory_dir / "entities.json"),
                state_path=str(memory_dir / "maintenance_state.json"),
                status="unchanged",
            )

        draft = self._build_draft(entries)
        max_seen_sequence = max([last_sequence, *[int(item.get("sequence", 0) or 0) for item in entries]])
        self._write_memory(
            conversation_id=conversation_id,
            session_id=session_id,
            memory_dir=memory_dir,
            draft=draft,
            state={
                "conversation_id": conversation_id,
                "session_id": session_id,
                "last_sequence": max_seen_sequence,
                "active_signature": active_signature,
                "processed_count": len(entries),
                "updated_at": utc_now_iso(),
            },
        )
        if self.llm is not None:
            if self.async_llm:
                _MEMORY_EXECUTOR.submit(self._write_llm_memory, conversation_id, session_id, memory_dir, entries, draft)
            else:
                self._write_llm_memory(conversation_id, session_id, memory_dir, entries, draft)
        return MemoryMaintenanceResult(
            conversation_id=conversation_id,
            session_id=session_id,
            memory_dir=str(memory_dir),
            processed_count=len(new_entries),
            last_sequence=max_seen_sequence,
            summary_path=str(memory_dir / "summary.md"),
            preferences_path=str(memory_dir / "preferences.json"),
            entities_path=str(memory_dir / "entities.json"),
            state_path=str(memory_dir / "maintenance_state.json"),
        )

    def maintain_all(self) -> list[MemoryMaintenanceResult]:
        results: list[MemoryMaintenanceResult] = []
        root = self.ledger_store.root
        if not root.exists():
            return results
        for conversation_dir in sorted(item for item in root.iterdir() if item.is_dir()):
            conversation_id = _conversation_id_from_ledger_dir(conversation_dir)
            if not conversation_id:
                continue
            session_ids = self._session_ids(conversation_id)
            for session_id in session_ids:
                results.append(self.maintain(conversation_id, session_id=session_id))
        return results

    def _session_ids(self, conversation_id: str) -> list[str]:
        sessions = {
            _entry_session_id(as_payload(entry))
            for entry in self.ledger_store.read_entries(conversation_id)
        }
        return sorted(sessions or {DEFAULT_SESSION_ID})

    def _build_draft(self, entries: list[dict[str, Any]]) -> MemoryDraft:
        draft = MemoryDraft()
        sender_counts: dict[str, int] = {}
        files: list[dict[str, Any]] = []
        urls: list[str] = []
        hashtags: set[str] = set()
        recent_topics: list[str] = []
        chat_title = ""
        conversation_type = ""

        for entry in entries:
            chat_title = chat_title or str(entry.get("chat_title", ""))
            conversation_type = conversation_type or str(entry.get("conversation_type", ""))
            sender = str(entry.get("sender_name") or ("self" if entry.get("is_self") else "unknown"))
            sender_counts[sender] = sender_counts.get(sender, 0) + 1
            text = _entry_text(entry)
            if text:
                line = _summary_line(entry, text, self.max_text_chars_per_entry)
                draft.summary_lines.append(line)
                recent_topics.extend(_topic_candidates(text))
                _collect_preferences(draft.preferences, entry, text)
                hashtags.update(_HASHTAG_RE.findall(text))
            files.extend(_entry_files(entry))
            urls.extend(_entry_urls(entry))

        draft.summary_lines = draft.summary_lines[-self.max_summary_lines :]
        draft.file_lines = [_file_summary_line(item) for item in files[-40:]]
        draft.entities = {
            "conversation": {
                "chat_title": chat_title,
                "conversation_type": conversation_type,
                "entry_count": len(entries),
            },
            "senders": [
                {"name": name, "message_count": count}
                for name, count in sorted(sender_counts.items(), key=lambda item: (-item[1], item[0]))
            ][:30],
            "files": files[-40:],
            "urls": _unique_keep_order(urls)[-40:],
            "hashtags": sorted(hashtags)[:40],
            "recent_topics": _unique_keep_order(recent_topics)[-30:],
        }
        return draft

    def _write_memory(
        self,
        *,
        conversation_id: str,
        session_id: str,
        memory_dir: Path,
        draft: MemoryDraft,
        state: dict[str, Any],
    ) -> None:
        memory_dir.mkdir(parents=True, exist_ok=True)
        summary = _render_summary(conversation_id, session_id, draft.summary_lines, draft.file_lines)
        _write_text_atomic(memory_dir / "summary.md", summary)
        _write_json_atomic(memory_dir / "preferences.json", draft.preferences)
        _write_json_atomic(memory_dir / "entities.json", draft.entities)
        _write_json_atomic(memory_dir / "maintenance_state.json", state)

    def _write_llm_memory(
        self,
        conversation_id: str,
        session_id: str,
        memory_dir: Path,
        entries: list[dict[str, Any]],
        fallback: MemoryDraft,
    ) -> None:
        if self.llm is None or not entries:
            return
        prompt = _memory_prompt(conversation_id, session_id, entries)
        try:
            raw = self.llm.generate_reply(prompt, workload="background")
            parsed = _parse_json_object(raw)
        except Exception:
            return
        if not parsed:
            return
        summary = _render_llm_summary(
            conversation_id,
            session_id,
            parsed,
            fallback.summary_lines,
            fallback.file_lines,
        )
        preferences = parsed.get("preferences") if isinstance(parsed.get("preferences"), dict) else fallback.preferences
        entities = _merge_entity_files(
            parsed.get("entities") if isinstance(parsed.get("entities"), dict) else fallback.entities,
            fallback.entities,
        )
        state = _read_json(memory_dir / "maintenance_state.json")
        state["llm_memory_updated_at"] = utc_now_iso()
        state["llm_memory_status"] = "updated"
        _write_text_atomic(memory_dir / "summary.md", summary)
        _write_json_atomic(memory_dir / "preferences.json", preferences)
        _write_json_atomic(memory_dir / "entities.json", entities)
        _write_json_atomic(memory_dir / "maintenance_state.json", state)


def _entry_session_id(entry: dict[str, Any]) -> str:
    return str(entry.get("session_id") or DEFAULT_SESSION_ID)


def _conversation_id_from_ledger_dir(conversation_dir: Path) -> str:
    messages_path = conversation_dir / "messages.jsonl"
    if not messages_path.exists():
        return ""
    try:
        with messages_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict):
                    return str(payload.get("conversation_id") or "").strip()
                return ""
    except (OSError, json.JSONDecodeError):
        return ""
    return ""


def _entry_text(entry: dict[str, Any]) -> str:
    blocks = []
    for block in entry.get("text_blocks", []):
        if not isinstance(block, dict):
            continue
        metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
        if metadata.get("visible_in_context") is False:
            continue
        text = str(block.get("text", "")).strip()
        if text:
            blocks.append(text)
    return "\n".join(blocks).strip()


def _summary_line(entry: dict[str, Any], text: str, max_chars: int) -> str:
    sequence = int(entry.get("sequence", 0) or 0)
    sender = str(entry.get("sender_name") or ("self" if entry.get("is_self") else "unknown"))
    role = "self" if entry.get("is_self") else str(entry.get("role", "user"))
    received_at = str(entry.get("received_at", ""))
    return f"- #{sequence:06d} {received_at} {sender} role={role}: {_compact(text, max_chars)}"


def _collect_preferences(preferences: dict[str, list[dict[str, Any]]], entry: dict[str, Any], text: str) -> None:
    if entry.get("is_self"):
        return
    for pattern, key in _PREFERENCE_PATTERNS:
        for match in pattern.finditer(text):
            value = _compact(match.group("value").strip(" ，。,.!！?？:：；;"), 180)
            if not value:
                continue
            preferences.setdefault(key, [])
            item = {
                "value": value,
                "sender_name": entry.get("sender_name", ""),
                "sequence": int(entry.get("sequence", 0) or 0),
                "observed_at": entry.get("received_at", ""),
            }
            if item not in preferences[key]:
                preferences[key].append(item)
    for key in list(preferences):
        preferences[key] = preferences[key][-30:]


def _entry_files(entry: dict[str, Any]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    origin = _entry_origin(entry)
    direction = "outgoing" if origin in {"agent", "owner_manual"} else "incoming"
    for attachment in entry.get("attachments", []):
        if not isinstance(attachment, dict):
            continue
        send = attachment.get("send") if isinstance(attachment.get("send"), dict) else {}
        files.append(
            _clean_empty(
                {
                "name": attachment.get("name", ""),
                "file_id": attachment.get("file_id", ""),
                "kind": attachment.get("kind", ""),
                "status": attachment.get("status", ""),
                "source": attachment.get("source", ""),
                "origin": origin,
                "direction": direction,
                "entry_role": str(entry.get("role") or ""),
                "sequence": int(entry.get("sequence", 0) or 0),
                "manifest_path": _nested(attachment, "workspace", "manifest_path"),
                "content_path": _nested(attachment, "artifacts", "content_path"),
                "send_status": send.get("status", ""),
                "send_reason": send.get("reason", ""),
                "bridge_id": send.get("bridge_id") or send.get("message_id") or "",
                "external_message_id": send.get("external_message_id", ""),
                }
            )
        )
    return files


def _file_summary_line(file: dict[str, Any]) -> str:
    sequence = int(file.get("sequence", 0) or 0)
    parts = [f"- #{sequence:06d}" if sequence else "-"]
    for key in (
        "name",
        "kind",
        "origin",
        "direction",
        "status",
        "send_status",
        "send_reason",
        "bridge_id",
        "external_message_id",
        "file_id",
        "manifest_path",
        "content_path",
    ):
        value = str(file.get(key, "")).strip()
        if value:
            parts.append(f"{key}={_compact(value, 180)}")
    return " ".join(parts)


def _entry_origin(entry: dict[str, Any]) -> str:
    role = str(entry.get("role") or "")
    if role == "assistant":
        return "agent"
    if entry.get("is_self") or role == "self":
        return "owner_manual"
    return "user"


def _clean_empty(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if value not in ("", None, [], {})
    }


def _entry_urls(entry: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for link in entry.get("links", []):
        if isinstance(link, dict) and link.get("url"):
            urls.append(str(link.get("url")))
    return urls


def _active_signature(entries: list[dict[str, Any]]) -> str:
    relevant = []
    for entry in entries:
        relevant.append(
            {
                "entry_id": entry.get("entry_id", ""),
                "sequence": entry.get("sequence", 0),
                "updated_at": entry.get("updated_at", ""),
                "text_blocks": entry.get("text_blocks", []),
                "attachments": entry.get("attachments", []),
                "links": entry.get("links", []),
            }
        )
    payload = json.dumps(relevant, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _topic_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for line in text.splitlines():
        cleaned = line.strip(" -#*>\t")
        if 4 <= len(cleaned) <= 48 and any(token in cleaned for token in ["任务", "文件", "分析", "计划", "总结", "需求"]):
            candidates.append(cleaned)
    return candidates


def _nested(payload: dict[str, Any], section: str, key: str) -> str:
    value = payload.get(section)
    if not isinstance(value, dict):
        return ""
    return str(value.get(key, ""))


def _render_summary(conversation_id: str, session_id: str, lines: list[str], file_lines: list[str]) -> str:
    header = [
        "# Session Memory Summary",
        "",
        f"conversation_id: {conversation_id}",
        f"session_id: {session_id}",
        "",
        "The summary is derived from active ledger entries in this session only.",
        "",
        "## Recent Durable Notes",
        "",
    ]
    body = lines or [
        "- No active session text entries yet." if file_lines else "- No active session entries yet."
    ]
    result = [*header, *body, ""]
    if file_lines:
        result.extend(["## Recent Files", "", *file_lines, ""])
    return "\n".join(result)


def _memory_prompt(conversation_id: str, session_id: str, entries: list[dict[str, Any]]) -> str:
    compact_entries = []
    for entry in entries[-120:]:
        compact_entries.append(
            {
                "sequence": int(entry.get("sequence", 0) or 0),
                "sender": entry.get("sender_name", ""),
                "role": "self" if entry.get("is_self") else entry.get("role", "user"),
                "time": entry.get("received_at", ""),
                "text": _compact(_entry_text(entry), 900),
                "files": _entry_files(entry),
                "urls": _entry_urls(entry),
            }
        )
    return (
        "你是一个长期记忆维护器。请从下面的会话 ledger 中提炼对后续真实对话有帮助的记忆，"
        "不要逐条复述。关注：稳定偏好/指令、仍在进行的任务、已完成结论、重要实体、文件引用、用户当前主题。"
        "如果某条用户指令已被后续消息废止，请不要把它作为偏好。只返回 JSON：\n"
        '{"summary": {"conversation_review": "...", "active_tasks": ["..."], "resolved": ["..."], "current_topics": ["..."]}, '
        '"preferences": {"instructions": [{"value": "...", "confidence": 0.0, "source_sequence": 1}], "avoid": [], "likes": [], "needs": []}, '
        '"entities": {"people": [], "files": [], "urls": [], "topics": []}}\n'
        f"\nconversation_id={conversation_id} session_id={session_id}\n"
        f"entries={json.dumps(compact_entries, ensure_ascii=False)}"
    )


def _render_llm_summary(
    conversation_id: str,
    session_id: str,
    parsed: dict[str, Any],
    fallback_lines: list[str],
    fallback_file_lines: list[str],
) -> str:
    summary = parsed.get("summary") if isinstance(parsed.get("summary"), dict) else {}
    lines = [
        "# Session Memory Summary",
        "",
        f"conversation_id: {conversation_id}",
        f"session_id: {session_id}",
        "",
        "The summary is an LLM-maintained memory distilled from active ledger entries.",
        "",
    ]
    review = str(summary.get("conversation_review", "")).strip()
    if review:
        lines.extend(["## Conversation Review", "", review, ""])
    for title, key in (
        ("Active Tasks", "active_tasks"),
        ("Resolved Notes", "resolved"),
        ("Current Topics", "current_topics"),
    ):
        values = summary.get(key, [])
        if isinstance(values, list) and values:
            lines.extend([f"## {title}", ""])
            lines.extend(f"- {str(item).strip()}" for item in values if str(item).strip())
            lines.append("")
    if len(lines) <= 7:
        fallback_body = fallback_lines or [
            "- No active session text entries yet." if fallback_file_lines else "- No active session entries yet."
        ]
        lines.extend(["## Recent Durable Notes", "", *fallback_body, ""])
    if fallback_file_lines:
        lines.extend(["## Recent Files", "", *fallback_file_lines, ""])
    return "\n".join(lines)


def _merge_entity_files(parsed_entities: dict[str, Any], fallback_entities: dict[str, Any]) -> dict[str, Any]:
    result = dict(parsed_entities)
    fallback_files = fallback_entities.get("files") if isinstance(fallback_entities.get("files"), list) else []
    if not fallback_files:
        return result
    parsed_files = result.get("files") if isinstance(result.get("files"), list) else []
    merged = list(parsed_files)
    indexes: dict[tuple[str, ...], int] = {}
    for index, item in enumerate(merged):
        for key in _entity_file_keys(item):
            indexes[key] = index
    for item in fallback_files:
        keys = _entity_file_keys(item)
        matched_index = next((indexes[key] for key in keys if key in indexes), None)
        if matched_index is not None:
            current = merged[matched_index]
            merged[matched_index] = {**current, **item} if isinstance(current, dict) else item
            for key in keys:
                indexes[key] = matched_index
            continue
        merged.append(item)
        for key in keys:
            indexes[key] = len(merged) - 1
    result["files"] = merged[-80:]
    return result


def _entity_file_keys(item: Any) -> list[tuple[str, ...]]:
    if not isinstance(item, dict):
        value = str(item).strip()
        return [("value", value)] if value else []
    keys: list[tuple[str, ...]] = []
    for key in ("bridge_id", "file_id", "content_path", "manifest_path", "name"):
        value = str(item.get(key, "")).strip()
        if value:
            keys.append((key, value))
    return keys


def _parse_json_object(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _compact(text: str, max_chars: int) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "..."


def _unique_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = str(item).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


_HASHTAG_RE = re.compile(r"#([A-Za-z0-9_\-\u4e00-\u9fff]{1,40})")
_PREFERENCE_PATTERNS = [
    (re.compile(r"(?:我希望|希望你|以后请|之后请|请你)(?P<value>[^。！？\n]{2,120})"), "instructions"),
    (re.compile(r"(?:不要|别)(?P<value>[^。！？\n]{2,120})"), "avoid"),
    (re.compile(r"(?:我喜欢|偏好|更喜欢)(?P<value>[^。！？\n]{2,120})"), "likes"),
    (re.compile(r"(?:我需要|需要你)(?P<value>[^。！？\n]{2,120})"), "needs"),
]


def result_payload(result: MemoryMaintenanceResult) -> dict[str, Any]:
    return asdict(result)

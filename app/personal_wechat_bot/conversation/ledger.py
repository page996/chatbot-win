from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from app.personal_wechat_bot.conversation.session_store import DEFAULT_SESSION_ID
from app.personal_wechat_bot.domain.models import NormalizedMessage, ReplyCandidate, SendResult, utc_now_iso


@dataclass(frozen=True)
class LedgerTextBlock:
    kind: str
    text: str
    source_ref: str = ""
    token_estimate: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    message_id: str
    conversation_id: str
    session_id: str
    conversation_type: str
    chat_title: str
    sender_name: str
    sender_wechat_id: str | None
    is_self: bool
    received_at: str
    sequence: int
    status: str
    text_blocks: list[dict[str, Any]]
    quote: dict[str, Any]
    attachments: list[dict[str, Any]]
    links: list[dict[str, Any]]
    source: str
    role: str = "user"
    send: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)


class ConversationLedgerStore:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir) / "conversation_ledgers"
        self.root.mkdir(parents=True, exist_ok=True)

    def append_message(self, message: NormalizedMessage) -> LedgerEntry:
        with self._conversation_lock(message.conversation_id):
            conversation_dir = self._conversation_dir(message.conversation_id)
            entries = self._read_entries(message.conversation_id)
            existing = _find_entry_by_message_id(entries, message.message_id)
            if existing is not None:
                return _entry_from_payload(existing)
            # A pulled-back self message is WeChat echoing the agent's own reply.
            # If it matches a recent role=assistant entry (recorded when the reply
            # was generated), don't append a duplicate role=self line — instead
            # confirm delivery on the existing assistant entry.
            if message.is_self:
                echoed = _find_assistant_entry_for_self_echo(entries, message.text)
                if echoed is not None:
                    self._confirm_self_echo(message, echoed, entries, conversation_dir)
                    return _entry_from_payload(echoed)
            entry = LedgerEntry(
                entry_id=_entry_id(message.message_id, message.conversation_id),
                message_id=message.message_id,
                conversation_id=message.conversation_id,
                session_id=_session_id_from_metadata(message.metadata),
                conversation_type=message.conversation_type,
                chat_title=message.chat_title,
                sender_name=message.sender_name,
                sender_wechat_id=message.sender_wechat_id,
                is_self=message.is_self,
                received_at=message.received_at,
                sequence=self._next_sequence(entries),
                status="active",
                text_blocks=_text_blocks_from_message(message),
                quote=_quote_from_metadata(message.metadata),
                attachments=_attachments_from_metadata(message.metadata),
                links=_links_from_text(message.text),
                source=str(message.metadata.get("source", "")),
                role="self" if message.is_self else "user",
                send={},
            )
            self._append_entry(conversation_dir, entry)
            self._write_state(message.conversation_id, entry)
            self._render_conversation(message.conversation_id)
            return entry

    def _confirm_self_echo(
        self,
        message: NormalizedMessage,
        echoed: dict[str, Any],
        entries: list[dict[str, Any]],
        conversation_dir: Path,
    ) -> None:
        """Record that WeChat echoed back the agent's own reply on the matching
        role=assistant entry, instead of appending a duplicate role=self line."""
        now = utc_now_iso()
        updated: list[dict[str, Any]] = []
        for item in entries:
            if item.get("entry_id") != echoed.get("entry_id"):
                updated.append(item)
                continue
            item = dict(item)
            send = item.get("send") if isinstance(item.get("send"), dict) else {}
            # A self-echo is positive delivery confirmation; only upgrade toward
            # "sent", never downgrade a status the send path already recorded.
            new_send = {**send}
            if str(send.get("status", "")) not in {"sent"}:
                new_send["status"] = "sent"
                if not new_send.get("sent_at"):
                    new_send["sent_at"] = message.received_at or now
            new_send["echo_confirmed_at"] = now
            new_send["echo_message_id"] = message.message_id
            item["send"] = new_send
            item["updated_at"] = now
            echoed.clear()
            echoed.update(item)
            updated.append(item)
        self._rewrite_entries(message.conversation_id, updated)
        self._render_conversation(message.conversation_id)

    def annotate_link(
        self,
        conversation_id: str,
        entry_id: str,
        url_id: str,
        *,
        status: str,
        summary: str = "",
        text: str = "",
        source_path: str = "",
        error: str = "",
    ) -> bool:
        entries = self._read_entries(conversation_id)
        changed = False
        updated: list[dict[str, Any]] = []
        for item in entries:
            if item.get("entry_id") != entry_id:
                updated.append(item)
                continue
            item = dict(item)
            item["links"] = _annotated_links(item.get("links", []), url_id, status, source_path, summary, error)
            if text and status == "completed":
                annotation_path = self._write_annotation(
                    conversation_id,
                    entry_id,
                    url_id,
                    text=text,
                    summary=summary,
                    source_path=source_path,
                )
                item["text_blocks"] = _upsert_text_block(
                    item.get("text_blocks", []),
                    kind="annotation:web",
                    source_ref=str(annotation_path),
                    text=_web_annotation_text(summary, text),
                    metadata={"url_id": url_id, "status": status},
                )
            item["updated_at"] = utc_now_iso()
            updated.append(item)
            changed = True
        if not changed:
            return False
        self._rewrite_entries(conversation_id, updated)
        self._render_conversation(conversation_id)
        return True

    def append_reply(
        self,
        reply: ReplyCandidate,
        *,
        chat_title: str = "",
        sender_name: str = "agent",
        conversation_type: str = "private",
        session_id: str = DEFAULT_SESSION_ID,
    ) -> LedgerEntry:
        with self._conversation_lock(reply.conversation_id):
            entries = self._read_entries(reply.conversation_id)
            attachments = _reply_attachments(reply)
            entry = LedgerEntry(
                entry_id=_entry_id(f"reply:{reply.message_id}:{reply.created_at}", reply.conversation_id),
                message_id=reply.message_id,
                conversation_id=reply.conversation_id,
                session_id=session_id or DEFAULT_SESSION_ID,
                conversation_type=conversation_type,
                chat_title=chat_title,
                sender_name=sender_name,
                sender_wechat_id=None,
                is_self=True,
                received_at=reply.created_at,
                sequence=self._next_sequence(entries),
                status="active",
                text_blocks=_text_blocks_from_reply(reply, attachments),
                quote={},
                attachments=attachments,
                links=_links_from_text(reply.text),
                source="reply_candidate",
                role="assistant",
                send=_reply_send_payload(reply),
                created_at=reply.created_at,
                updated_at=reply.created_at,
            )
            conversation_dir = self._conversation_dir(reply.conversation_id)
            self._append_entry(conversation_dir, entry)
            self._write_state(reply.conversation_id, entry)
            self._render_conversation(reply.conversation_id)
            return entry

    def update_reply_send_result(self, conversation_id: str, entry_id: str, send_result: SendResult | dict[str, Any]) -> bool:
        entries = self._read_entries(conversation_id)
        payload = asdict(send_result) if isinstance(send_result, SendResult) else dict(send_result)
        changed = False
        updated: list[dict[str, Any]] = []
        now = utc_now_iso()
        for item in entries:
            if item.get("entry_id") != entry_id:
                updated.append(item)
                continue
            item = dict(item)
            send = item.get("send") if isinstance(item.get("send"), dict) else {}
            item["send"] = {
                **send,
                "status": str(payload.get("status") or ""),
                "reason": str(payload.get("reason") or ""),
                "message_id": str(payload.get("message_id") or item.get("message_id") or ""),
                "conversation_id": str(payload.get("conversation_id") or conversation_id),
                "sent_at": str(payload.get("sent_at") or ""),
                "updated_at": now,
            }
            item["updated_at"] = now
            updated.append(item)
            changed = True
        if not changed:
            return False
        self._rewrite_entries(conversation_id, updated)
        self._render_conversation(conversation_id)
        return True

    def update_reply_send_result_for_candidate(self, reply: ReplyCandidate, send_result: SendResult | dict[str, Any]) -> bool:
        entries = self._read_entries(reply.conversation_id)
        matches = [
            item
            for item in entries
            if item.get("role") == "assistant"
            and item.get("message_id") == reply.message_id
            and (not reply.created_at or item.get("created_at") == reply.created_at)
        ]
        if not matches:
            matches = [
                item
                for item in entries
                if item.get("role") == "assistant" and item.get("message_id") == reply.message_id
            ]
        if not matches:
            return False
        return self.update_reply_send_result(
            reply.conversation_id,
            str(matches[-1].get("entry_id", "")),
            send_result,
        )

    def update_bridge_send_result(
        self,
        conversation_id: str,
        bridge_id: str,
        *,
        status: str,
        reason: str = "",
        external_message_id: str = "",
    ) -> bool:
        bridge_id = str(bridge_id or "").strip()
        if not bridge_id:
            return False
        entries = self._read_entries(conversation_id)
        changed = False
        updated: list[dict[str, Any]] = []
        now = utc_now_iso()
        for item in entries:
            send = item.get("send") if isinstance(item.get("send"), dict) else {}
            send_reason = str(send.get("reason", ""))
            send_message_id = str(send.get("message_id", ""))
            if item.get("role") == "assistant" and (bridge_id == send_message_id or bridge_id in send_reason):
                item = dict(item)
                next_send = {
                    **send,
                    "status": status,
                    "reason": reason or f"bridge_ack:{status}",
                    "updated_at": now,
                }
                if external_message_id:
                    next_send["external_message_id"] = external_message_id
                if status == "sent":
                    next_send["sent_at"] = now
                item["send"] = next_send
                item["updated_at"] = now
                changed = True
            updated.append(item)
        if not changed:
            return False
        self._rewrite_entries(conversation_id, updated)
        self._render_conversation(conversation_id)
        return True

    def mark_recalled(self, conversation_id: str, message_id: str, *, reason: str = "wechat_recall") -> bool:
        entries = self._read_entries(conversation_id)
        changed = False
        updated: list[dict[str, Any]] = []
        for item in entries:
            if item.get("message_id") == message_id and item.get("status") == "active":
                item = dict(item)
                item["status"] = "recalled"
                item["recall_reason"] = reason
                item["updated_at"] = utc_now_iso()
                changed = True
            updated.append(item)
        if not changed:
            return False
        self._rewrite_entries(conversation_id, updated)
        self._render_conversation(conversation_id)
        return True

    def read_entries(self, conversation_id: str, *, include_removed: bool = False) -> list[LedgerEntry]:
        entries = self._read_entries(conversation_id)
        if not include_removed:
            entries = [item for item in entries if item.get("status") == "active"]
        return [_entry_from_payload(item) for item in entries]

    def lookup_quote_context(
        self,
        conversation_id: str,
        quote: dict[str, Any],
        *,
        neighbor_radius: int = 2,
    ) -> dict[str, Any]:
        entries = self._read_entries(conversation_id)
        active = [item for item in entries if item.get("status") == "active"]
        index = _find_quote_index(active, quote)
        if index is None:
            return {"status": "not_found", "quote": quote, "entries": [], "attachments": []}
        start = max(0, index - neighbor_radius)
        end = min(len(active), index + neighbor_radius + 1)
        window = active[start:end]
        attachments: list[dict[str, Any]] = []
        for item in window:
            for attachment in item.get("attachments", []):
                if isinstance(attachment, dict):
                    attachments.append(dict(attachment))
        return {
            "status": "found",
            "quote": quote,
            "matched_entry_id": active[index].get("entry_id", ""),
            "entries": window,
            "attachments": attachments,
        }

    def conversation_markdown_path(self, conversation_id: str) -> Path:
        return self._conversation_dir(conversation_id) / "conversation.md"

    def annotations_dir(self, conversation_id: str) -> Path:
        return self._conversation_dir(conversation_id) / "annotations"

    def _conversation_dir(self, conversation_id: str) -> Path:
        return self.root / _safe_segment(conversation_id)

    def _messages_path(self, conversation_id: str) -> Path:
        return self._conversation_dir(conversation_id) / "messages.jsonl"

    def _state_path(self, conversation_id: str) -> Path:
        return self._conversation_dir(conversation_id) / "state.json"

    @contextmanager
    def _conversation_lock(self, conversation_id: str) -> Iterator[None]:
        lock_path = self._conversation_dir(conversation_id) / ".ledger.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 30.0
        fd: int | None = None
        while fd is None:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if _stale_lock(lock_path):
                    try:
                        lock_path.unlink()
                        continue
                    except OSError:
                        pass
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for conversation ledger lock: {lock_path}")
                time.sleep(0.025)
        try:
            os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
            yield
        finally:
            if fd is not None:
                os.close(fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def _read_entries(self, conversation_id: str) -> list[dict[str, Any]]:
        path = self._messages_path(conversation_id)
        if not path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    entries.append(payload)
        return entries

    def _append_entry(self, conversation_dir: Path, entry: LedgerEntry) -> None:
        conversation_dir.mkdir(parents=True, exist_ok=True)
        with (conversation_dir / "messages.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def _rewrite_entries(self, conversation_id: str, entries: list[dict[str, Any]]) -> None:
        path = self._messages_path(conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for item in entries:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        tmp.replace(path)

    def _next_sequence(self, entries: list[dict[str, Any]]) -> int:
        if not entries:
            return 1
        return max(int(item.get("sequence", 0) or 0) for item in entries) + 1

    def _write_state(self, conversation_id: str, entry: LedgerEntry) -> None:
        path = self._state_path(conversation_id)
        payload = {
            "conversation_id": conversation_id,
            "last_sequence": entry.sequence,
            "last_entry_id": entry.entry_id,
            "last_session_id": entry.session_id,
            "updated_at": utc_now_iso(),
        }
        _write_json(path, payload)

    def _render_conversation(self, conversation_id: str) -> None:
        entries = self._read_entries(conversation_id)
        path = self.conversation_markdown_path(conversation_id)
        lines = [f"# Conversation {conversation_id}", ""]
        for item in entries:
            lines.extend(_render_entry_markdown(item))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _write_annotation(
        self,
        conversation_id: str,
        entry_id: str,
        url_id: str,
        *,
        text: str,
        summary: str,
        source_path: str,
    ) -> Path:
        path = self.annotations_dir(conversation_id) / f"{_safe_segment(entry_id)}_{_safe_segment(url_id)}.md"
        lines = [
            "# Web Annotation",
            "",
            f"entry_id: {entry_id}",
            f"url_id: {url_id}",
            f"source_path: {source_path}",
            "",
            "## Summary",
            "",
            summary,
            "",
            "## Text",
            "",
            text,
            "",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


def _text_blocks_from_message(message: NormalizedMessage) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    text = _primary_message_text(message).strip()
    if text:
        voice = _voice_from_metadata(message.metadata)
        blocks.append(
            asdict(
                LedgerTextBlock(
                    kind=_primary_text_kind(message),
                    text=text,
                    token_estimate=_estimate_tokens(text),
                    metadata=_primary_text_metadata(voice),
                )
            )
        )
    else:
        voice = _voice_from_metadata(message.metadata)
    if voice and not _voice_duplicate_primary_text(text, voice):
        voice_text = str(voice.get("text", "")).strip()
        if voice_text:
            blocks.append(
                asdict(
                    LedgerTextBlock(
                        kind="voice:transcript",
                        text=voice_text,
                        token_estimate=_estimate_tokens(voice_text),
                        metadata={
                            "visible_in_context": True,
                            "source": voice.get("source", ""),
                            "status": voice.get("status", ""),
                            "duration": voice.get("duration", ""),
                        },
                    )
                )
            )
    for attachment in _attachments_from_metadata(message.metadata):
        parse = attachment.get("parse") if isinstance(attachment.get("parse"), dict) else {}
        parsed_text = str(parse.get("text", "")).strip()
        if parsed_text:
            blocks.append(
                asdict(
                    LedgerTextBlock(
                        kind=f"attachment:{parse.get('kind', attachment.get('kind', 'file'))}",
                        text=parsed_text,
                        source_ref=str(_attachment_source_ref(attachment)),
                        token_estimate=_estimate_tokens(parsed_text),
                        metadata={"file_id": attachment.get("file_id", ""), "name": attachment.get("name", "")},
                    )
                )
            )
    return blocks


def _text_blocks_from_reply(reply: ReplyCandidate, attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks = [
        asdict(
            LedgerTextBlock(
                kind="reply",
                text=reply.text,
                token_estimate=_estimate_tokens(reply.text),
                metadata={"visible_in_context": True},
            )
        )
    ]
    for attachment in attachments:
        parse = attachment.get("parse") if isinstance(attachment.get("parse"), dict) else {}
        parsed_text = str(parse.get("text", "")).strip()
        if not parsed_text:
            continue
        blocks.append(
            asdict(
                LedgerTextBlock(
                    kind=f"attachment:{parse.get('kind', attachment.get('kind', 'file'))}",
                    text=parsed_text,
                    source_ref=str(_attachment_source_ref(attachment)),
                    token_estimate=_estimate_tokens(parsed_text),
                    metadata={
                        "file_id": attachment.get("file_id", ""),
                        "name": attachment.get("name", ""),
                        "source": attachment.get("source", ""),
                        "direction": "outgoing",
                    },
                )
            )
        )
    return blocks


def _primary_message_text(message: NormalizedMessage) -> str:
    original = message.metadata.get("original_text")
    if isinstance(original, str):
        return original
    return message.text


def _primary_text_kind(message: NormalizedMessage) -> str:
    voice = _voice_from_metadata(message.metadata)
    if voice and _voice_duplicate_primary_text(message.text, voice):
        return "voice:transcript"
    return "text"


def _primary_text_metadata(voice: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"visible_in_context": True}
    if voice:
        metadata.update(
            {
                "source": voice.get("source", ""),
                "status": voice.get("status", ""),
                "duration": voice.get("duration", ""),
            }
        )
    return metadata


def _voice_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    raw = metadata.get("voice")
    if not isinstance(raw, dict):
        return {}
    text = str(raw.get("text", "")).strip()
    if not text:
        return {}
    return {
        "status": str(raw.get("status") or "transcribed").strip(),
        "source": str(raw.get("source") or "wechat_builtin_voice_to_text_ocr").strip(),
        "text": text,
        "duration": str(raw.get("duration", "")).strip(),
    }


def _voice_duplicate_primary_text(text: str, voice: dict[str, Any]) -> bool:
    return bool(text.strip()) and _normalize_for_match(text) == _normalize_for_match(str(voice.get("text", "")))


def _attachments_from_metadata(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    raw = metadata.get("attachments", [])
    if not isinstance(raw, list):
        return []
    attachments: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        attachment = dict(item)
        # The same media can be emitted more than once upstream (e.g. a voice
        # backend event plus the generic attachment record). Collapse identical
        # payloads so the ledger keeps a single block per file.
        key = (
            str(attachment.get("file_id", "")).strip(),
            str(attachment.get("path", "")).strip(),
            str(attachment.get("name", "")).strip(),
        )
        if key != ("", "", "") and key in seen:
            continue
        seen.add(key)
        _attach_parse_artifact_refs(attachment)
        attachments.append(attachment)
    return attachments


def _reply_attachments(reply: ReplyCandidate) -> list[dict[str, Any]]:
    attachments = [_normalize_reply_attachment(item, source="reply_candidate") for item in reply.attachments]
    if reply.tool_result is not None:
        for index, ref in enumerate(reply.tool_result.output_refs):
            text = str(ref).strip()
            if not text:
                continue
            attachments.append(
                _normalize_reply_attachment(
                    {
                        "path": text,
                        "name": Path(text).name,
                        "kind": "tool_output",
                        "tool_name": reply.tool_result.tool_name,
                        "call_id": reply.tool_result.call_id,
                        "output_index": index,
                    },
                    source="tool_result",
                )
            )
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in attachments:
        key = (str(item.get("path", "")), str(item.get("name", "")))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _normalize_reply_attachment(item: Any, *, source: str) -> dict[str, Any]:
    if isinstance(item, str):
        payload: dict[str, Any] = {"path": item, "name": Path(item).name}
    elif isinstance(item, dict):
        payload = dict(item)
    else:
        payload = {}
    path = str(payload.get("path") or payload.get("source_ref") or payload.get("output_ref") or "").strip()
    name = str(payload.get("name") or payload.get("filename") or Path(path).name).strip()
    kind = str(payload.get("kind") or payload.get("type") or "file").strip()
    result = {
        "status": str(payload.get("status") or "outgoing").strip(),
        "source": str(payload.get("source") or source).strip(),
        "path": path,
        "name": name,
        "kind": kind,
    }
    for key in ("file_id", "tool_name", "call_id", "output_index", "mime_type", "size", "md5", "suffix", "reason"):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            result[key] = value
    for key in ("workspace", "parse", "artifacts"):
        value = payload.get(key)
        if isinstance(value, dict) and value:
            result[key] = dict(value)
    return {key: value for key, value in result.items() if value not in ("", None)}


def _reply_send_payload(reply: ReplyCandidate) -> dict[str, Any]:
    payload = {
        "status": "candidate",
        "mode": reply.send_mode,
        "model": reply.model,
        "policy_hits": list(reply.policy_hits),
    }
    if reply.send_metadata:
        payload["metadata"] = dict(reply.send_metadata)
    return payload


def _attach_parse_artifact_refs(attachment: dict[str, Any]) -> None:
    workspace = attachment.get("workspace") if isinstance(attachment.get("workspace"), dict) else {}
    parse = attachment.get("parse") if isinstance(attachment.get("parse"), dict) else {}
    derived_dir = str(workspace.get("derived_dir", "")).strip()
    if not derived_dir:
        return
    content_path = parse.get("content_path") or str(Path(derived_dir) / "content.md")
    analysis_path = parse.get("analysis_path") or str(Path(derived_dir) / "analysis.json")
    artifacts = attachment.get("artifacts") if isinstance(attachment.get("artifacts"), dict) else {}
    attachment["artifacts"] = {
        **artifacts,
        "content_path": str(content_path),
        "analysis_path": str(analysis_path),
        "parse_result_path": str(Path(derived_dir) / "parse_result.json"),
        "chunks_dir": artifacts.get("chunks_dir", str(Path(derived_dir) / "chunks")),
        "table_index_path": artifacts.get("table_index_path", ""),
        "table_chunk_count": artifacts.get("table_chunk_count", 0),
        "media_dir": artifacts.get("media_dir", str(Path(derived_dir) / "media")),
        "media_index_path": artifacts.get("media_index_path", str(Path(derived_dir) / "media" / "index.json")),
        "media_extract_count": artifacts.get("media_extract_count", 0),
        "media_ocr_status": artifacts.get("media_ocr_status", ""),
        "media_ocr_dir": artifacts.get("media_ocr_dir", str(Path(derived_dir) / "media" / "ocr")),
        "media_ocr_index_path": artifacts.get("media_ocr_index_path", ""),
        "media_ocr_count": artifacts.get("media_ocr_count", 0),
        "media_ocr_error_count": artifacts.get("media_ocr_error_count", 0),
        "media_asr_status": artifacts.get("media_asr_status", ""),
        "media_asr_dir": artifacts.get("media_asr_dir", str(Path(derived_dir) / "media" / "asr")),
        "media_asr_count": artifacts.get("media_asr_count", 0),
        "media_asr_error_count": artifacts.get("media_asr_error_count", 0),
    }


def _quote_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    raw = metadata.get("quote") or metadata.get("quoted_message") or {}
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict):
        return {}
    quote = {
        "message_id": str(raw.get("message_id") or raw.get("quoted_message_id") or "").strip(),
        "entry_id": str(raw.get("entry_id") or raw.get("quoted_entry_id") or "").strip(),
        "sender_name": str(raw.get("sender_name") or raw.get("quoted_sender_name") or "").strip(),
        "text": str(raw.get("text") or raw.get("quoted_text") or raw.get("content") or "").strip(),
        "received_at": str(raw.get("received_at") or raw.get("quoted_received_at") or "").strip(),
        "source": str(raw.get("source") or "").strip(),
    }
    return {key: value for key, value in quote.items() if value}


def _links_from_text(text: str) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for url in _URL_RE.findall(text):
        links.append({"url": url, "url_id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:16], "status": "pending"})
    return links


def _annotated_links(
    links: Any,
    url_id: str,
    status: str,
    source_path: str,
    summary: str,
    error: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    found = False
    raw_links = links if isinstance(links, list) else []
    for item in raw_links:
        if not isinstance(item, dict):
            continue
        link = dict(item)
        if str(link.get("url_id", "")) == url_id:
            link["status"] = status
            link["annotation_path"] = source_path
            link["summary"] = summary
            link["error"] = error
            link["updated_at"] = utc_now_iso()
            found = True
        result.append(link)
    if not found:
        result.append(
            {
                "url_id": url_id,
                "url": "",
                "status": status,
                "annotation_path": source_path,
                "summary": summary,
                "error": error,
                "updated_at": utc_now_iso(),
            }
        )
    return result


def _upsert_text_block(
    blocks: Any,
    *,
    kind: str,
    source_ref: str,
    text: str,
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    replaced = False
    raw_blocks = blocks if isinstance(blocks, list) else []
    for item in raw_blocks:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == kind and item.get("source_ref") == source_ref:
            result.append(
                asdict(
                    LedgerTextBlock(
                        kind=kind,
                        text=text,
                        source_ref=source_ref,
                        token_estimate=_estimate_tokens(text),
                        metadata=metadata,
                    )
                )
            )
            replaced = True
        else:
            result.append(dict(item))
    if not replaced:
        result.append(
            asdict(
                LedgerTextBlock(
                    kind=kind,
                    text=text,
                    source_ref=source_ref,
                    token_estimate=_estimate_tokens(text),
                    metadata=metadata,
                )
            )
        )
    return result


def _web_annotation_text(summary: str, text: str, max_chars: int = 3000) -> str:
    content = summary.strip() or text.strip()
    if len(content) <= max_chars:
        return content
    return content[: max_chars - 1].rstrip() + "..."


def _render_entry_markdown(item: dict[str, Any]) -> list[str]:
    sequence = int(item.get("sequence", 0) or 0)
    received_at = str(item.get("received_at", ""))
    sender = str(item.get("sender_name", ""))
    status = str(item.get("status", "active"))
    lines = [f"## {sequence:06d} {received_at} {sender}", ""]
    session_id = str(item.get("session_id") or DEFAULT_SESSION_ID)
    lines.append(f"[session:{session_id}]")
    send = item.get("send") if isinstance(item.get("send"), dict) else {}
    if send:
        send_status = str(send.get("status", "")).strip()
        mode = str(send.get("mode", "")).strip()
        reason = str(send.get("reason", "")).strip()
        send_note = " ".join(part for part in (f"status={send_status}" if send_status else "", f"mode={mode}" if mode else "", f"reason={reason}" if reason else "") if part)
        if send_note:
            lines.append(f"[send:{send_note}]")
    lines.append("")
    if status != "active":
        lines.append(f"[{status}] message_id={item.get('message_id', '')}")
        lines.append("")
        return lines
    quote = item.get("quote") if isinstance(item.get("quote"), dict) else {}
    if quote:
        quote_text = str(quote.get("text", "")).strip()
        quote_ref = str(quote.get("message_id") or quote.get("entry_id") or "").strip()
        lines.append(f"> Quote {quote_ref}: {quote_text}".rstrip())
        lines.append("")
    for block in item.get("text_blocks", []):
        if not isinstance(block, dict):
            continue
        kind = str(block.get("kind", "text"))
        text = str(block.get("text", "")).strip()
        source_ref = str(block.get("source_ref", "")).strip()
        if kind != "text":
            lines.append(f"[block:{kind}{' source=' + source_ref if source_ref else ''}]")
        if text:
            lines.append(text)
        lines.append("")
    for attachment in item.get("attachments", []):
        if not isinstance(attachment, dict):
            continue
        name = str(attachment.get("name", ""))
        file_id = str(attachment.get("file_id", ""))
        path = str(attachment.get("path", ""))
        source = str(attachment.get("source", ""))
        workspace = attachment.get("workspace") if isinstance(attachment.get("workspace"), dict) else {}
        manifest = str(workspace.get("manifest_path", ""))
        if file_id or manifest:
            lines.append(f"[file:{file_id} name={name} manifest={manifest}]")
        else:
            lines.append(f"[file:outgoing name={name} path={path} source={source}]")
    if item.get("attachments"):
        lines.append("")
    for link in item.get("links", []):
        if isinstance(link, dict):
            lines.append(f"[link:{link.get('url_id', '')}] {link.get('url', '')}")
    if item.get("links"):
        lines.append("")
    return lines


def _find_entry_by_message_id(entries: list[dict[str, Any]], message_id: str) -> dict[str, Any] | None:
    for item in entries:
        if item.get("message_id") == message_id:
            return item
    return None


def _find_quote_index(entries: list[dict[str, Any]], quote: dict[str, Any]) -> int | None:
    entry_id = str(quote.get("entry_id", "")).strip()
    message_id = str(quote.get("message_id", "")).strip()
    quote_text = str(quote.get("text", "")).strip()
    for index, item in enumerate(entries):
        if entry_id and item.get("entry_id") == entry_id:
            return index
        if message_id and item.get("message_id") == message_id:
            return index
    if quote_text:
        normalized_quote = _normalize_for_match(quote_text)
        matches: list[int] = []
        for index, item in enumerate(entries):
            text = "\n".join(
                str(block.get("text", ""))
                for block in item.get("text_blocks", [])
                if isinstance(block, dict)
            )
            normalized_text = _normalize_for_match(text)
            if normalized_quote and (normalized_quote in normalized_text or normalized_text in normalized_quote):
                matches.append(index)
        if matches:
            return matches[-1]
    return None


def _entry_from_payload(payload: dict[str, Any]) -> LedgerEntry:
    return LedgerEntry(
        entry_id=str(payload.get("entry_id", "")),
        message_id=str(payload.get("message_id", "")),
        conversation_id=str(payload.get("conversation_id", "")),
        session_id=str(payload.get("session_id") or DEFAULT_SESSION_ID),
        conversation_type=str(payload.get("conversation_type", "")),
        chat_title=str(payload.get("chat_title", "")),
        sender_name=str(payload.get("sender_name", "")),
        sender_wechat_id=str(payload.get("sender_wechat_id") or "") or None,
        is_self=bool(payload.get("is_self", False)),
        received_at=str(payload.get("received_at", "")),
        sequence=int(payload.get("sequence", 0) or 0),
        status=str(payload.get("status", "active")),
        text_blocks=[dict(item) for item in payload.get("text_blocks", []) if isinstance(item, dict)],
        quote=dict(payload.get("quote", {})) if isinstance(payload.get("quote"), dict) else {},
        attachments=[dict(item) for item in payload.get("attachments", []) if isinstance(item, dict)],
        links=[dict(item) for item in payload.get("links", []) if isinstance(item, dict)],
        source=str(payload.get("source", "")),
        role=str(payload.get("role", "user")),
        send=dict(payload.get("send", {})) if isinstance(payload.get("send"), dict) else {},
        created_at=str(payload.get("created_at", "")),
        updated_at=str(payload.get("updated_at", "")),
    )


def _attachment_source_ref(attachment: dict[str, Any]) -> str:
    workspace = attachment.get("workspace") if isinstance(attachment.get("workspace"), dict) else {}
    return str(workspace.get("manifest_path") or workspace.get("derived_dir") or workspace.get("workspace_dir") or "")


def _session_id_from_metadata(metadata: dict[str, Any]) -> str:
    session_id = str(metadata.get("session_id") or "").strip()
    return session_id or DEFAULT_SESSION_ID


def _entry_id(message_id: str, conversation_id: str) -> str:
    return hashlib.sha256(f"{conversation_id}:{message_id}".encode("utf-8")).hexdigest()[:24]


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "default"


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _entry_primary_text(entry: dict[str, Any]) -> str:
    """Best-effort plain text of a ledger entry for content-based matching."""
    blocks = entry.get("text_blocks")
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("kind", ""))
        # Only compare authored message text, not derived annotations/attachments.
        if kind.startswith("annotation") or kind.startswith("attachment"):
            continue
        text = str(block.get("text", "")).strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def _find_assistant_entry_for_self_echo(
    entries: list[dict[str, Any]],
    text: str,
    *,
    lookback: int = 40,
) -> dict[str, Any] | None:
    """Find a recent role=assistant entry whose text matches a pulled-back self message.

    When the agent sends a reply, it is recorded as ``role=assistant`` keyed on the
    incoming user's message_id. WeChat later echoes that same text back through the
    pull with a fresh weflow message_id and ``is_self=True``; without this match it
    would be appended a second time as ``role=self`` and the LLM context would show
    the assistant's line twice. Match on normalized text within a recent window.
    """
    normalized = _normalize_for_match(text)
    if not normalized:
        return None
    for item in reversed(entries[-max(1, lookback):]):
        if str(item.get("role", "")) != "assistant":
            continue
        if _normalize_for_match(_entry_primary_text(item)) == normalized:
            return item
    return None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _stale_lock(path: Path, *, max_age_seconds: float = 60.0) -> bool:
    try:
        return time.time() - path.stat().st_mtime > max_age_seconds
    except OSError:
        return False


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

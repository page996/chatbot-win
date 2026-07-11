from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from app.personal_wechat_bot.conversation.segment import (
    chat_title_from_registry,
    conversation_projection_dir,
    resolve_segment,
    validate_conversation_segment,
)
from app.personal_wechat_bot.conversation.ledger_database import ConversationLedgerDatabase
from app.personal_wechat_bot.conversation.session_store import DEFAULT_SESSION_ID
from app.personal_wechat_bot.domain.models import NormalizedMessage, ReplyCandidate, SendResult, utc_now_iso
from app.personal_wechat_bot.runtime.process_lock import scoped_process_lock_path, short_process_lock
from app.personal_wechat_bot.workspace.file_visibility import redact_file_internal_urls


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
    dedupe_key: str
    message_ids: list[str]
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


@dataclass(frozen=True)
class AppendMessageResult:
    entry: LedgerEntry
    status: str


class ConversationLedgerStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "conversation_ledgers"
        self.database = ConversationLedgerDatabase(self.data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        # Cache conversation_id -> stable directory segment. Chat titles can
        # change; the directory carrying history must stay put.
        self._segment_cache: dict[str, str] = {}

    def append_message(self, message: NormalizedMessage) -> LedgerEntry:
        return self.append_message_result(message).entry

    def append_message_result(self, message: NormalizedMessage) -> AppendMessageResult:
        self._remember_segment(message.conversation_id, message.chat_title)
        with self._conversation_lock(message.conversation_id):
            conversation_dir = self._conversation_dir(message.conversation_id)
            entries = self._read_entries(message.conversation_id)
            existing = _find_entry_by_message_id(entries, message.message_id)
            if existing is not None:
                return self._upsert_existing_message(message, existing, entries)
            # A pulled-back self message is WeChat echoing the agent's own reply.
            # If it matches a recent role=assistant entry (recorded when the reply
            # was generated), don't append a duplicate role=self line; instead
            # confirm delivery on the existing assistant entry.
            if message.is_self:
                echoed = _find_assistant_entry_for_self_echo(entries, message.text, message_id=message.message_id)
                if echoed is not None:
                    self._confirm_self_echo(message, echoed, entries, conversation_dir)
                    return AppendMessageResult(_entry_from_payload(echoed), "self_echo_confirmed")
            dedupe_key = _message_dedupe_key(message)
            if dedupe_key:
                semantic = _find_entry_by_dedupe_key(entries, dedupe_key)
                if semantic is not None:
                    return self._upsert_existing_message(message, semantic, entries)
            entry = self._new_message_entry(message, self._next_sequence(entries))
            self._append_entry(conversation_dir, entry)
            self._write_state(message.conversation_id, entry)
            self._render_conversation(message.conversation_id)
            return AppendMessageResult(entry, "created")

    def _upsert_existing_message(
        self,
        message: NormalizedMessage,
        existing: dict[str, Any],
        entries: list[dict[str, Any]],
    ) -> AppendMessageResult:
        updated = self._updated_message_payload(message, existing)
        if not _message_payload_changed(existing, updated):
            return AppendMessageResult(_entry_from_payload(existing), "duplicate")
        rewritten: list[dict[str, Any]] = []
        for item in entries:
            if item.get("entry_id") == existing.get("entry_id"):
                rewritten.append(updated)
            else:
                rewritten.append(item)
        self._rewrite_entries(message.conversation_id, rewritten)
        entry = _entry_from_payload(updated)
        self._write_state(message.conversation_id, entry)
        self._render_conversation(message.conversation_id)
        return AppendMessageResult(entry, "updated")

    def _new_message_entry(self, message: NormalizedMessage, sequence: int) -> LedgerEntry:
        chat_title = _preferred_chat_title(
            chat_title_from_registry(self.data_dir, message.conversation_id),
            message.chat_title,
        )
        return LedgerEntry(
            entry_id=_entry_id(message.message_id, message.conversation_id),
            message_id=message.message_id,
            dedupe_key=_message_dedupe_key(message),
            message_ids=_message_aliases(message),
            conversation_id=message.conversation_id,
            session_id=_session_id_from_metadata(message.metadata),
            conversation_type=message.conversation_type,
            chat_title=chat_title,
            sender_name=message.sender_name,
            sender_wechat_id=message.sender_wechat_id,
            is_self=message.is_self,
            received_at=message.received_at,
            sequence=sequence,
            status="active",
            text_blocks=_text_blocks_from_message(message),
            quote=_quote_from_metadata(message.metadata),
            attachments=_attachments_from_metadata(message.metadata),
            links=_links_from_message(message),
            source=str(message.metadata.get("source", "")),
            role="self" if message.is_self else "user",
            send={},
        )

    def _updated_message_payload(self, message: NormalizedMessage, existing: dict[str, Any]) -> dict[str, Any]:
        sequence = int(existing.get("sequence", 0) or 0)
        candidate = asdict(self._new_message_entry(message, sequence))
        return {
            **candidate,
            "entry_id": str(existing.get("entry_id") or candidate["entry_id"]),
            "message_id": str(existing.get("message_id") or candidate["message_id"]),
            "message_ids": _merge_message_ids(existing, _message_aliases(message)),
            "chat_title": _preferred_chat_title(str(existing.get("chat_title") or ""), candidate["chat_title"]),
            "status": str(existing.get("status") or candidate["status"]),
            "send": dict(existing.get("send", {})) if isinstance(existing.get("send"), dict) else {},
            "created_at": str(existing.get("created_at") or candidate["created_at"]),
            "updated_at": utc_now_iso(),
        }

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
        with self._conversation_lock(conversation_id):
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

    def annotate_entry(
        self,
        conversation_id: str,
        entry_id: str,
        *,
        kind: str,
        annotation_id: str,
        text: str,
        summary: str = "",
        source_path: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        kind = str(kind or "").strip()
        annotation_id = str(annotation_id or "").strip()
        if not kind.startswith("annotation:") or not annotation_id or not text.strip():
            return False
        with self._conversation_lock(conversation_id):
            entries = self._read_entries(conversation_id)
            changed = False
            updated: list[dict[str, Any]] = []
            for item in entries:
                if item.get("entry_id") != entry_id:
                    updated.append(item)
                    continue
                item = dict(item)
                annotation_path = self._write_annotation(
                    conversation_id,
                    entry_id,
                    annotation_id,
                    text=text,
                    summary=summary,
                    source_path=source_path,
                    title=f"{kind} Annotation",
                    metadata=metadata or {},
                )
                block_metadata = {
                    "annotation_id": annotation_id,
                    "status": "completed",
                    "visible_in_context": True,
                    **(metadata or {}),
                }
                item["text_blocks"] = _upsert_text_block(
                    item.get("text_blocks", []),
                    kind=kind,
                    source_ref=str(annotation_path),
                    text=_web_annotation_text("", _combined_annotation_text(summary, text)),
                    metadata=block_metadata,
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
        self._remember_segment(reply.conversation_id, chat_title)
        with self._conversation_lock(reply.conversation_id):
            entries = self._read_entries(reply.conversation_id)
            entry = self._new_reply_entry(
                reply,
                sequence=self._next_sequence(entries),
                chat_title=chat_title,
                sender_name=sender_name,
                conversation_type=conversation_type,
                session_id=session_id,
            )
            self._persist_reply_entry(entry)
            return entry

    def append_reply_if_latest(
        self,
        reply: ReplyCandidate,
        *,
        expected_latest_sequence: int,
        chat_title: str = "",
        sender_name: str = "agent",
        conversation_type: str = "private",
        session_id: str = DEFAULT_SESSION_ID,
    ) -> LedgerEntry | None:
        """Append only while the source snapshot is still the ledger tail."""

        self._remember_segment(reply.conversation_id, chat_title)
        with self._conversation_lock(reply.conversation_id):
            entries = self._read_entries(reply.conversation_id)
            latest_sequence = max((int(item.get("sequence", 0) or 0) for item in entries), default=0)
            if latest_sequence != max(0, int(expected_latest_sequence or 0)):
                return None
            if any(
                str(item.get("role") or "") == "assistant"
                and str(item.get("message_id") or "") == reply.message_id
                for item in entries
            ):
                return None
            entry = self._new_reply_entry(
                reply,
                sequence=self._next_sequence(entries),
                chat_title=chat_title,
                sender_name=sender_name,
                conversation_type=conversation_type,
                session_id=session_id,
            )
            self._persist_reply_entry(entry)
            return entry

    def _new_reply_entry(
        self,
        reply: ReplyCandidate,
        *,
        sequence: int,
        chat_title: str,
        sender_name: str,
        conversation_type: str,
        session_id: str,
    ) -> LedgerEntry:
        attachments = _reply_attachments(reply)
        return LedgerEntry(
            entry_id=_entry_id(f"reply:{reply.message_id}:{reply.created_at}", reply.conversation_id),
            message_id=reply.message_id,
            dedupe_key="",
            message_ids=[reply.message_id],
            conversation_id=reply.conversation_id,
            session_id=session_id or DEFAULT_SESSION_ID,
            conversation_type=conversation_type,
            chat_title=chat_title,
            sender_name=sender_name,
            sender_wechat_id=None,
            is_self=True,
            received_at=reply.created_at,
            sequence=sequence,
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

    def _persist_reply_entry(self, entry: LedgerEntry) -> None:
        conversation_dir = self._conversation_dir(entry.conversation_id)
        self._append_entry(conversation_dir, entry)
        self._write_state(entry.conversation_id, entry)
        self._render_conversation(entry.conversation_id)

    def update_reply_send_result(self, conversation_id: str, entry_id: str, send_result: SendResult | dict[str, Any]) -> bool:
        with self._conversation_lock(conversation_id):
            entries = self._read_entries(conversation_id)
            payload = asdict(send_result) if isinstance(send_result, SendResult) else dict(send_result)
            changed = False
            matched = False
            updated: list[dict[str, Any]] = []
            now = utc_now_iso()
            for item in entries:
                if item.get("entry_id") != entry_id:
                    updated.append(item)
                    continue
                matched = True
                item = dict(item)
                send = item.get("send") if isinstance(item.get("send"), dict) else {}
                current_status = str(send.get("status") or "")
                incoming_status = str(payload.get("status") or "")
                if current_status in {"sent", "accepted", "failed"} and incoming_status in {
                    "",
                    "pending",
                    "queued_for_confirm",
                    "queued_to_bridge",
                }:
                    updated.append(item)
                    continue
                next_send = {
                    **send,
                    "status": str(payload.get("status") or ""),
                    "reason": str(payload.get("reason") or ""),
                    "message_id": str(payload.get("message_id") or item.get("message_id") or ""),
                    "conversation_id": str(payload.get("conversation_id") or conversation_id),
                    "sent_at": str(payload.get("sent_at") or ""),
                    "updated_at": now,
                }
                details = payload.get("details")
                if isinstance(details, dict) and details:
                    next_send["details"] = details
                external_message_id = str(payload.get("external_message_id") or "")
                if external_message_id:
                    next_send["external_message_id"] = external_message_id
                item["send"] = next_send
                item["attachments"] = _attachments_with_send_details(item.get("attachments", []), details)
                item["updated_at"] = now
                updated.append(item)
                changed = True
            if not matched:
                return False
            if changed:
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
        with self._conversation_lock(conversation_id):
            entries = self._read_entries(conversation_id)
            changed = False
            updated: list[dict[str, Any]] = []
            now = utc_now_iso()
            for item in entries:
                send = item.get("send") if isinstance(item.get("send"), dict) else {}
                if item.get("role") == "assistant" and _send_payload_matches_bridge(send, item, bridge_id):
                    item = dict(item)
                    next_send = _send_payload_with_bridge_ack(
                        send,
                        bridge_id,
                        status=status,
                        reason=reason or f"bridge_ack:{status}",
                        external_message_id=external_message_id,
                        now=now,
                    )
                    item["send"] = next_send
                    item["attachments"] = _attachments_with_bridge_ack(
                        item.get("attachments", []),
                        bridge_id,
                        status=status,
                        reason=reason or f"bridge_ack:{status}",
                        external_message_id=external_message_id,
                        now=now,
                    )
                    item["updated_at"] = now
                    changed = True
                updated.append(item)
            if not changed:
                return False
            self._rewrite_entries(conversation_id, updated)
            self._render_conversation(conversation_id)
            return True

    def requeue_bridge_send_result(
        self,
        conversation_id: str,
        old_bridge_id: str,
        new_bridge_id: str,
        *,
        reason: str = "",
        old_bridge_ids: list[str] | None = None,
    ) -> bool:
        old_bridge_id = str(old_bridge_id or "").strip()
        new_bridge_id = str(new_bridge_id or "").strip()
        if not old_bridge_id or not new_bridge_id:
            return False
        retry_candidates = _dedupe_bridge_ids([old_bridge_id, *(old_bridge_ids or [])])
        with self._conversation_lock(conversation_id):
            entries = self._read_entries(conversation_id)
            changed = False
            updated: list[dict[str, Any]] = []
            now = utc_now_iso()
            for item in entries:
                send = item.get("send") if isinstance(item.get("send"), dict) else {}
                if (
                    item.get("role") == "assistant"
                    and any(_send_payload_matches_bridge(send, item, candidate) for candidate in retry_candidates)
                ):
                    item = dict(item)
                    retry_reason = reason or f"retry_to_non_foreground_bridge:{new_bridge_id}"
                    item["send"] = _send_payload_with_bridge_retry(
                        send,
                        set(retry_candidates),
                        new_bridge_id,
                        retry_parent_id=old_bridge_id,
                        reason=retry_reason,
                        now=now,
                    )
                    item["attachments"] = _attachments_with_bridge_retry(
                        item.get("attachments", []),
                        set(retry_candidates),
                        new_bridge_id,
                        retry_parent_id=old_bridge_id,
                        reason=retry_reason,
                        now=now,
                    )
                    item["updated_at"] = now
                    changed = True
                updated.append(item)
            if not changed:
                return False
            self._rewrite_entries(conversation_id, updated)
            self._render_conversation(conversation_id)
            return True

    def mark_recalled(self, conversation_id: str, message_id: str, *, reason: str = "wechat_recall") -> bool:
        with self._conversation_lock(conversation_id):
            entries = self._read_entries(conversation_id)
            changed = False
            updated: list[dict[str, Any]] = []
            for item in entries:
                if message_id in _entry_message_ids(item) and item.get("status") == "active":
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
        if entries and self._readable_projections_missing(conversation_id):
            # Re-read under the lifecycle lock before writing projections. A
            # concurrent channel purge may have deleted the SQLite authority
            # while this reader was waiting; stale pre-lock rows must never
            # recreate a deleted conversation directory.
            with self._conversation_lock(conversation_id):
                entries = self._read_entries(conversation_id)
                self._restore_readable_projections(conversation_id, entries)
        if not include_removed:
            entries = [item for item in entries if item.get("status") == "active"]
        return [_entry_from_payload(item) for item in entries]

    def _readable_projections_missing(self, conversation_id: str) -> bool:
        return not (
            self._messages_path(conversation_id).exists()
            and self.conversation_markdown_path(conversation_id).exists()
        )

    def list_conversation_ids(self) -> list[str]:
        return self.database.list_conversation_ids()

    def refresh_file_refs(self, conversation_id: str) -> bool:
        with self._conversation_lock(conversation_id):
            entries = self._read_entries(conversation_id)
            updated: list[dict[str, Any]] = []
            changed = False
            for item in entries:
                refreshed = _refresh_entry_file_refs(item)
                if refreshed != item:
                    changed = True
                updated.append(refreshed)
            if not changed:
                return False
            self._rewrite_entries(conversation_id, updated)
            self._render_conversation(conversation_id)
            return True

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
            "matched_attachments": _entry_attachments(active[index]),
            "entries": window,
            "attachments": _dedupe_attachments(_entry_attachments(active[index]) + attachments),
        }

    def conversation_markdown_path(self, conversation_id: str) -> Path:
        return self._conversation_dir(conversation_id) / "conversation.md"

    def annotations_dir(self, conversation_id: str) -> Path:
        return self._conversation_dir(conversation_id) / "annotations"

    def _conversation_dir(self, conversation_id: str) -> Path:
        # Fast path: in-session cache. Slow path: recover chat_title from the
        # channel index so writes (messages.jsonl, conversation.md, state.json,
        # lock) land in the same human-readable dir after a restart with a cold
        # cache, matching what read_entries and channel cleanup expect.
        cached_segment = self._segment_cache.get(conversation_id, "")
        if cached_segment:
            return conversation_projection_dir(
                self.root,
                cached_segment,
                label="cached ledger conversation segment",
            )
        database_segment = self.database.segment_for(conversation_id)
        if database_segment:
            database_segment = validate_conversation_segment(
                database_segment,
                label="ledger database conversation segment",
            )
            conversation_dir = conversation_projection_dir(
                self.root,
                database_segment,
                label="ledger database conversation segment",
            )
            self._segment_cache[conversation_id] = database_segment
            return conversation_dir
        segment = resolve_segment(self.data_dir, conversation_id)
        return conversation_projection_dir(
            self.root,
            segment,
            label="resolved ledger conversation segment",
        )

    def _remember_segment(self, conversation_id: str, chat_title: str = "") -> str:
        cached_segment = self._segment_cache.get(conversation_id, "")
        if cached_segment:
            cached_segment = validate_conversation_segment(
                cached_segment,
                label="cached ledger conversation segment",
            )
            conversation_projection_dir(
                self.root,
                cached_segment,
                label="cached ledger conversation segment",
            )
            return cached_segment
        database_segment = self.database.segment_for(conversation_id)
        if database_segment:
            database_segment = validate_conversation_segment(
                database_segment,
                label="ledger database conversation segment",
            )
            conversation_projection_dir(
                self.root,
                database_segment,
                label="ledger database conversation segment",
            )
            self._segment_cache[conversation_id] = database_segment
            return database_segment
        segment = resolve_segment(self.data_dir, conversation_id, chat_title)
        conversation_projection_dir(
            self.root,
            segment,
            label="resolved ledger conversation segment",
        )
        self._segment_cache[conversation_id] = segment
        self.database.set_segment(conversation_id, segment)
        return segment

    def _find_conversation_dir(self, conversation_id: str) -> Path:
        return self._conversation_dir(conversation_id)

    def _messages_path(self, conversation_id: str) -> Path:
        return self._find_conversation_dir(conversation_id) / "messages.jsonl"

    def _state_path(self, conversation_id: str) -> Path:
        return self._find_conversation_dir(conversation_id) / "state.json"

    @contextmanager
    def _conversation_lock(self, conversation_id: str) -> Iterator[None]:
        lock_path = scoped_process_lock_path(
            self.data_dir,
            "conversation-lifecycle",
            conversation_id,
        )
        with short_process_lock(
            lock_path,
            timeout_seconds=30.0,
            stale_after_seconds=60.0,
            timeout_label="conversation ledger lock",
        ):
            yield

    def _read_entries(self, conversation_id: str) -> list[dict[str, Any]]:
        return self.database.list_entries(conversation_id)

    def _append_entry(self, conversation_dir: Path, entry: LedgerEntry) -> None:
        conversation_dir.mkdir(parents=True, exist_ok=True)
        payload = asdict(entry)
        self.database.set_segment(entry.conversation_id, conversation_dir.name)
        self.database.upsert_entry(payload)
        self._write_entries_projection(
            conversation_dir / "messages.jsonl",
            self.database.list_entries(entry.conversation_id),
        )

    def _rewrite_entries(self, conversation_id: str, entries: list[dict[str, Any]]) -> None:
        path = self._messages_path(conversation_id)
        self.database.set_segment(conversation_id, path.parent.name)
        self.database.replace_entries(conversation_id, entries)
        self._write_entries_projection(path, entries)

    def _write_entries_projection(self, path: Path, entries: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for item in entries:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        tmp.replace(path)

    def _restore_readable_projections(self, conversation_id: str, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        messages_path = self._messages_path(conversation_id)
        markdown_path = self.conversation_markdown_path(conversation_id)
        if not messages_path.exists():
            self._write_entries_projection(messages_path, entries)
        if not markdown_path.exists():
            self._render_conversation(conversation_id)

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
        title: str = "Web Annotation",
        metadata: dict[str, Any] | None = None,
    ) -> Path:
        path = self.annotations_dir(conversation_id) / f"{_safe_segment(entry_id)}_{_safe_segment(url_id)}.md"
        lines = [
            f"# {title}",
            "",
            f"entry_id: {entry_id}",
            f"url_id: {url_id}",
            f"source_path: {source_path}",
            "",
        ]
        if metadata:
            lines.extend(["## Metadata", "", json.dumps(metadata, ensure_ascii=False, indent=2), ""])
        lines.extend(
            [
                "## Summary",
                "",
                summary,
                "",
                "## Text",
                "",
                text,
                "",
            ]
        )
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
                    metadata=_primary_text_metadata(voice, message),
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
            metadata = {
                "file_id": attachment.get("file_id", ""),
                "name": attachment.get("name", ""),
                "visible_in_context": False,
                "visibility": "file_content_hidden_use_file_read",
            }
            blocks.append(
                asdict(
                    LedgerTextBlock(
                        kind=f"attachment:{parse.get('kind', attachment.get('kind', 'file'))}",
                        text=parsed_text,
                        source_ref=str(_attachment_source_ref(attachment)),
                        token_estimate=_estimate_tokens(parsed_text),
                        metadata=metadata,
                    )
                )
            )
        brief = _file_analysis_brief_text(attachment)
        if brief:
            blocks.append(
                asdict(
                    LedgerTextBlock(
                        kind="file:analysis",
                        text=brief,
                        source_ref=str(_attachment_source_ref(attachment)),
                        token_estimate=_estimate_tokens(brief),
                        metadata={
                            "file_id": attachment.get("file_id", ""),
                            "name": attachment.get("name", ""),
                            "visible_in_context": True,
                            "source": "content.md::AI Analysis+Key Points",
                        },
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
                        "visible_in_context": False,
                        "visibility": "file_content_hidden_use_file_read",
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
    if str(message.metadata.get("control_event") or "") == "session_reset":
        return "control:session_reset"
    voice = _voice_from_metadata(message.metadata)
    if voice and _voice_duplicate_primary_text(message.text, voice):
        return "voice:transcript"
    return "text"


def _primary_text_metadata(voice: dict[str, Any], message: NormalizedMessage | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"visible_in_context": True}
    if message is not None and str(message.metadata.get("control_event") or "") == "session_reset":
        metadata.update(
            {
                "visible_in_context": False,
                "status": "applied",
                "control_event": "session_reset",
                "reset_session_id": str(message.metadata.get("reset_session_id") or message.metadata.get("session_id") or ""),
            }
        )
    if message is not None:
        for key in (
            "context_only",
            "deferred_reply",
            "deferred_reply_reason",
            "deferred_reply_anchor_raw_id",
            "capture_phase",
            "history_index",
            "source",
        ):
            if key in message.metadata:
                metadata[key] = message.metadata[key]
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
        "source": str(raw.get("source") or "local_asr_fallback").strip(),
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


def _file_analysis_brief_text(attachment: dict[str, Any]) -> str:
    payload = _file_analysis_brief_payload(attachment)
    if not payload:
        return ""
    key_points = payload.get("key_points", []) if isinstance(payload.get("key_points"), list) else []
    lines = [
        "[file_analysis]",
        "AI Analysis:",
        str(payload.get("summary", "")).strip(),
        "Key Points:",
    ]
    lines.extend(f"- {point}" for point in key_points if str(point).strip())
    lines.append("[/file_analysis]")
    return "\n".join(lines)


def _file_analysis_brief_payload(attachment: dict[str, Any]) -> dict[str, Any]:
    parse = attachment.get("parse") if isinstance(attachment.get("parse"), dict) else {}
    artifacts = attachment.get("artifacts") if isinstance(attachment.get("artifacts"), dict) else {}
    status = str(artifacts.get("ai_analysis_status") or parse.get("ai_analysis_status") or "").strip()
    if status != "analyzed":
        return {}
    summary = str(artifacts.get("ai_summary") or parse.get("ai_summary") or "").strip()
    if not summary or "fake_llm.completed" in summary or "PLAN:" in summary or "MONITOR:" in summary:
        return {}
    raw_points = artifacts.get("ai_key_points") or parse.get("ai_key_points") or []
    key_points = [redact_file_internal_urls(str(item).strip()) for item in raw_points if str(item).strip()][:12]
    file_id = str(attachment.get("file_id", "")).strip()
    return {
        "file_id": file_id,
        "name": str(attachment.get("name", "")).strip(),
        "kind": str(parse.get("kind") or attachment.get("kind") or "").strip(),
        "summary": redact_file_internal_urls(summary),
        "key_points": key_points,
    }


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


def _attachments_with_send_details(attachments: Any, details: Any) -> list[Any]:
    raw = attachments if isinstance(attachments, list) else []
    if not isinstance(details, dict):
        return [dict(item) if isinstance(item, dict) else item for item in raw]
    files = details.get("files") if isinstance(details.get("files"), list) else []
    result: list[Any] = []
    for attachment in raw:
        if not isinstance(attachment, dict):
            result.append(attachment)
            continue
        item = dict(attachment)
        match = _send_file_detail_for_attachment(item, files)
        if match:
            item["send"] = _attachment_send_payload(match)
        result.append(item)
    return result


def _attachments_with_bridge_ack(
    attachments: Any,
    bridge_id: str,
    *,
    status: str,
    reason: str,
    external_message_id: str,
    now: str,
) -> list[Any]:
    raw = attachments if isinstance(attachments, list) else []
    result: list[Any] = []
    for attachment in raw:
        if not isinstance(attachment, dict):
            result.append(attachment)
            continue
        item = dict(attachment)
        send = item.get("send") if isinstance(item.get("send"), dict) else {}
        if not _bridge_id_matches_send_payload(send, bridge_id):
            result.append(item)
            continue
        updated_send = _send_part_with_ack(
            send,
            status=status,
            reason=reason,
            external_message_id=external_message_id,
            now=now,
        )
        item["send"] = updated_send
        result.append(item)
    return result


def _attachments_with_bridge_retry(
    attachments: Any,
    old_bridge_ids: set[str],
    new_bridge_id: str,
    *,
    retry_parent_id: str,
    reason: str,
    now: str,
) -> list[Any]:
    raw = attachments if isinstance(attachments, list) else []
    result: list[Any] = []
    for attachment in raw:
        if not isinstance(attachment, dict):
            result.append(attachment)
            continue
        item = dict(attachment)
        send = item.get("send") if isinstance(item.get("send"), dict) else {}
        if any(_bridge_id_matches_send_payload(send, candidate) for candidate in old_bridge_ids):
            item["send"] = _send_part_with_bridge_retry(
                send,
                old_bridge_ids,
                new_bridge_id,
                retry_parent_id=retry_parent_id,
                reason=reason,
                now=now,
            )
        result.append(item)
    return result


def _send_payload_matches_bridge(send: dict[str, Any], item: dict[str, Any], bridge_id: str) -> bool:
    if _bridge_id_matches_send_payload(send, bridge_id):
        return True
    details = send.get("details") if isinstance(send.get("details"), dict) else {}
    if details:
        bridge_ids = details.get("bridge_ids") if isinstance(details.get("bridge_ids"), list) else []
        if bridge_id in {str(value) for value in bridge_ids}:
            return True
        text = details.get("text") if isinstance(details.get("text"), dict) else {}
        if _bridge_id_matches_send_payload(text, bridge_id):
            return True
        files = details.get("files") if isinstance(details.get("files"), list) else []
        for file_detail in files:
            if isinstance(file_detail, dict) and _bridge_id_matches_send_payload(file_detail, bridge_id):
                return True
    attachments = item.get("attachments") if isinstance(item.get("attachments"), list) else []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        attachment_send = attachment.get("send") if isinstance(attachment.get("send"), dict) else {}
        if _bridge_id_matches_send_payload(attachment_send, bridge_id):
            return True
    return False


def _send_payload_with_bridge_ack(
    send: dict[str, Any],
    bridge_id: str,
    *,
    status: str,
    reason: str,
    external_message_id: str,
    now: str,
) -> dict[str, Any]:
    updated = dict(send)
    details = send.get("details") if isinstance(send.get("details"), dict) else {}
    if details:
        next_details = dict(details)
        text = next_details.get("text") if isinstance(next_details.get("text"), dict) else {}
        if text and _bridge_id_matches_send_payload(text, bridge_id):
            next_details["text"] = _send_part_with_ack(
                text,
                status=status,
                reason=reason,
                external_message_id=external_message_id,
                now=now,
            )
        files = next_details.get("files") if isinstance(next_details.get("files"), list) else []
        next_files: list[Any] = []
        for file_detail in files:
            if isinstance(file_detail, dict) and _bridge_id_matches_send_payload(file_detail, bridge_id):
                next_files.append(
                    _send_part_with_ack(
                        file_detail,
                        status=status,
                        reason=reason,
                        external_message_id=external_message_id,
                        now=now,
                    )
                )
            else:
                next_files.append(dict(file_detail) if isinstance(file_detail, dict) else file_detail)
        if files:
            next_details["files"] = next_files
        updated["details"] = next_details
        aggregate_status = _aggregate_send_status(_send_part_statuses(next_details))
        if aggregate_status:
            updated["status"] = aggregate_status
        updated["last_bridge_ack"] = {
            "bridge_id": bridge_id,
            "status": status,
            "reason": reason,
            "external_message_id": external_message_id,
            "updated_at": now,
        }
        if updated.get("status") == "sent" and not updated.get("sent_at"):
            updated["sent_at"] = now
    else:
        updated["status"] = status
        updated["reason"] = reason
        if external_message_id:
            updated["external_message_id"] = external_message_id
        if status == "sent":
            updated["sent_at"] = now
    updated["updated_at"] = now
    return updated


def _send_payload_with_bridge_retry(
    send: dict[str, Any],
    old_bridge_ids: set[str],
    new_bridge_id: str,
    *,
    retry_parent_id: str,
    reason: str,
    now: str,
) -> dict[str, Any]:
    updated = dict(send)
    top_matches = any(_bridge_id_matches_send_payload(send, candidate) for candidate in old_bridge_ids)
    for key in ("message_id", "bridge_id", "external_id"):
        if str(updated.get(key) or "") in old_bridge_ids:
            updated[key] = new_bridge_id
    details = send.get("details") if isinstance(send.get("details"), dict) else {}
    if details:
        next_details = dict(details)
        part_requeued = False
        bridge_ids = details.get("bridge_ids") if isinstance(details.get("bridge_ids"), list) else []
        next_bridge_ids = _replace_bridge_ids(bridge_ids, old_bridge_ids, new_bridge_id)
        if new_bridge_id not in next_bridge_ids:
            next_bridge_ids.append(new_bridge_id)
        next_details["bridge_ids"] = next_bridge_ids
        text = details.get("text") if isinstance(details.get("text"), dict) else {}
        if text and any(_bridge_id_matches_send_payload(text, candidate) for candidate in old_bridge_ids):
            part_requeued = True
            next_details["text"] = _send_part_with_bridge_retry(
                text,
                old_bridge_ids,
                new_bridge_id,
                retry_parent_id=retry_parent_id,
                reason=reason,
                now=now,
            )
        files = details.get("files") if isinstance(details.get("files"), list) else []
        if files:
            part_requeued = part_requeued or any(
                isinstance(file_detail, dict)
                and any(_bridge_id_matches_send_payload(file_detail, candidate) for candidate in old_bridge_ids)
                for file_detail in files
            )
            next_details["files"] = [
                _send_part_with_bridge_retry(
                    file_detail,
                    old_bridge_ids,
                    new_bridge_id,
                    retry_parent_id=retry_parent_id,
                    reason=reason,
                    now=now,
                )
                if isinstance(file_detail, dict)
                and any(_bridge_id_matches_send_payload(file_detail, candidate) for candidate in old_bridge_ids)
                else (dict(file_detail) if isinstance(file_detail, dict) else file_detail)
                for file_detail in files
            ]
        detail_acks = next_details.get("bridge_acks") if isinstance(next_details.get("bridge_acks"), dict) else {}
        if detail_acks:
            next_details["bridge_acks"] = {
                str(key): dict(value)
                for key, value in detail_acks.items()
                if isinstance(value, dict) and str(key) not in old_bridge_ids and str(key) != new_bridge_id
            }
        next_details.pop("last_bridge_ack", None)
        updated["details"] = next_details
        aggregate_status = _aggregate_send_status(_send_part_statuses(next_details))
        if not part_requeued and aggregate_status in {"sent", "accepted"}:
            aggregate_status = "queued_to_bridge"
        updated["status"] = aggregate_status or "queued_to_bridge"
    else:
        updated["status"] = "queued_to_bridge"
    updated["reason"] = reason
    updated["sent_at"] = ""
    updated["retry_of"] = retry_parent_id
    updated["retry_at"] = now
    updated["updated_at"] = now
    if top_matches:
        updated.pop("external_message_id", None)
    bridge_acks = updated.get("bridge_acks") if isinstance(updated.get("bridge_acks"), dict) else {}
    if bridge_acks:
        updated["bridge_acks"] = {
            str(key): dict(value)
            for key, value in bridge_acks.items()
            if isinstance(value, dict) and str(key) not in old_bridge_ids and str(key) != new_bridge_id
        }
    updated.pop("last_bridge_ack", None)
    return updated


def _send_part_with_bridge_retry(
    payload: dict[str, Any],
    old_bridge_ids: set[str],
    new_bridge_id: str,
    *,
    retry_parent_id: str,
    reason: str,
    now: str,
) -> dict[str, Any]:
    updated = dict(payload)
    for key in ("message_id", "bridge_id", "external_id"):
        if str(updated.get(key) or "") in old_bridge_ids:
            updated[key] = new_bridge_id
    updated.update(
        {
            "status": "queued_to_bridge",
            "reason": reason,
            "sent_at": "",
            "retry_of": retry_parent_id,
            "retry_at": now,
            "updated_at": now,
        }
    )
    updated.pop("external_message_id", None)
    updated.pop("last_bridge_ack", None)
    return updated


def _replace_bridge_ids(values: list[Any], old_bridge_ids: set[str], new_bridge_id: str) -> list[str]:
    return _dedupe_bridge_ids(
        [new_bridge_id if str(value) in old_bridge_ids else str(value) for value in values]
    )


def _dedupe_bridge_ids(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        bridge_id = str(value or "").strip()
        if not bridge_id or bridge_id in seen:
            continue
        seen.add(bridge_id)
        result.append(bridge_id)
    return result


def _send_file_detail_for_attachment(attachment: dict[str, Any], files: list[Any]) -> dict[str, Any]:
    attachment_path = str(attachment.get("path") or "").strip()
    attachment_name = str(attachment.get("name") or "").strip()
    for file_detail in files:
        if not isinstance(file_detail, dict):
            continue
        detail_path = str(file_detail.get("path") or "").strip()
        detail_name = str(file_detail.get("name") or "").strip()
        if attachment_path and detail_path and attachment_path == detail_path:
            return file_detail
        if not attachment_path and attachment_name and detail_name and attachment_name == detail_name:
            return file_detail
        if attachment_name and detail_name and attachment_name == detail_name and not detail_path:
            return file_detail
    return {}


def _attachment_send_payload(payload: dict[str, Any]) -> dict[str, Any]:
    message_id = str(payload.get("message_id") or "").strip()
    result = {
        "status": str(payload.get("status") or "").strip(),
        "reason": str(payload.get("reason") or "").strip(),
        "message_id": message_id,
        "bridge_id": message_id,
    }
    return {key: value for key, value in result.items() if value not in ("", None)}


def _bridge_id_matches_send_payload(payload: dict[str, Any], bridge_id: str) -> bool:
    bridge_id = str(bridge_id or "").strip()
    if not bridge_id:
        return False
    for key in ("bridge_id", "message_id", "external_id"):
        if str(payload.get(key) or "").strip() == bridge_id:
            return True
    return bridge_id in str(payload.get("reason") or "")


def _send_part_with_ack(
    payload: dict[str, Any],
    *,
    status: str,
    reason: str,
    external_message_id: str,
    now: str,
) -> dict[str, Any]:
    updated = dict(payload)
    updated["status"] = status
    updated["reason"] = reason
    if external_message_id:
        updated["external_message_id"] = external_message_id
    if status == "sent":
        updated["sent_at"] = now
    updated["updated_at"] = now
    return updated


def _send_part_statuses(details: dict[str, Any]) -> list[str]:
    statuses: list[str] = []
    text = details.get("text") if isinstance(details.get("text"), dict) else {}
    if text.get("status"):
        statuses.append(str(text.get("status")))
    files = details.get("files") if isinstance(details.get("files"), list) else []
    for file_detail in files:
        if isinstance(file_detail, dict) and file_detail.get("status"):
            statuses.append(str(file_detail.get("status")))
    return statuses


def _aggregate_send_status(statuses: list[str]) -> str:
    cleaned = [str(item or "").strip() for item in statuses if str(item or "").strip()]
    if not cleaned:
        return ""
    if any(item == "failed" for item in cleaned):
        return "failed"
    if any(item in {"queued_to_bridge", "queued_for_confirm"} for item in cleaned):
        return "queued_to_bridge"
    if any(item == "accepted" for item in cleaned):
        return "accepted"
    if all(item == "sent" for item in cleaned):
        return "sent"
    return cleaned[-1]


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
        "ocr_tables_dir": artifacts.get("ocr_tables_dir", str(Path(derived_dir) / "ocr_tables")),
        "ocr_table_index_path": artifacts.get("ocr_table_index_path", ""),
        "ocr_table_chunk_count": artifacts.get("ocr_table_chunk_count", 0),
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


def _refresh_entry_file_refs(item: dict[str, Any]) -> dict[str, Any]:
    updated = dict(item)
    changed = False

    refreshed_blocks, blocks_changed = _refresh_text_block_visibility(updated.get("text_blocks", []))
    if blocks_changed:
        updated["text_blocks"] = refreshed_blocks
        changed = True

    refreshed_links = _refreshed_links_from_visible_text(updated)
    if refreshed_links != (updated.get("links") if isinstance(updated.get("links"), list) else []):
        updated["links"] = refreshed_links
        changed = True

    attachments = item.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        if changed:
            updated["updated_at"] = utc_now_iso()
        return updated
    refreshed_attachments: list[Any] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            refreshed_attachments.append(attachment)
            continue
        refreshed = _refresh_attachment_file_refs(attachment)
        if refreshed != attachment:
            changed = True
        refreshed_attachments.append(refreshed)
    refreshed_blocks, analysis_blocks_changed = _sync_file_analysis_blocks(updated.get("text_blocks", []), refreshed_attachments)
    if analysis_blocks_changed:
        updated["text_blocks"] = refreshed_blocks
        changed = True
    if not changed:
        return dict(item)
    updated["attachments"] = refreshed_attachments
    updated["updated_at"] = utc_now_iso()
    return updated


def _refresh_text_block_visibility(blocks: Any) -> tuple[list[Any], bool]:
    if not isinstance(blocks, list):
        return [], bool(blocks)
    changed = False
    refreshed: list[Any] = []
    for block in blocks:
        if not isinstance(block, dict):
            refreshed.append(block)
            continue
        kind = str(block.get("kind", ""))
        if not kind.startswith("attachment:"):
            refreshed.append(block)
            continue
        metadata = dict(block.get("metadata", {})) if isinstance(block.get("metadata"), dict) else {}
        if metadata.get("visible_in_context") is False and metadata.get("visibility"):
            refreshed.append(block)
            continue
        updated = dict(block)
        metadata["visible_in_context"] = False
        metadata.setdefault("visibility", "file_content_hidden_use_file_read")
        updated["metadata"] = metadata
        refreshed.append(updated)
        changed = True
    return refreshed, changed


def _sync_file_analysis_blocks(blocks: Any, attachments: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    raw_blocks = [dict(item) for item in blocks if isinstance(item, dict)] if isinstance(blocks, list) else []
    refreshed = [
        block
        for block in raw_blocks
        if str(block.get("kind", "")) != "file:analysis"
    ]
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        brief = _file_analysis_brief_text(attachment)
        if not brief:
            continue
        refreshed.append(
            asdict(
                LedgerTextBlock(
                    kind="file:analysis",
                    text=brief,
                    source_ref=str(_attachment_source_ref(attachment)),
                    token_estimate=_estimate_tokens(brief),
                    metadata={
                        "file_id": attachment.get("file_id", ""),
                        "name": attachment.get("name", ""),
                        "visible_in_context": True,
                        "source": "content.md::AI Analysis+Key Points",
                    },
                )
            )
        )
    return refreshed, refreshed != raw_blocks


def _entry_link_source_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in item.get("text_blocks", []):
        if not isinstance(block, dict):
            continue
        metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
        kind = str(block.get("kind", ""))
        if metadata.get("visible_in_context") is False or kind.startswith("attachment:"):
            continue
        text = str(block.get("text", "")).strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _refreshed_links_from_visible_text(item: dict[str, Any]) -> list[dict[str, Any]]:
    fresh = _links_from_text(_entry_link_source_text(item))
    existing = item.get("links", [])
    existing_by_id = {
        str(link.get("url_id", "")): dict(link)
        for link in existing
        if isinstance(link, dict) and str(link.get("url_id", ""))
    } if isinstance(existing, list) else {}
    merged: list[dict[str, Any]] = []
    for link in fresh:
        prior = existing_by_id.get(str(link.get("url_id", "")))
        if prior and prior.get("url") == link.get("url"):
            merged.append({**link, **prior})
        else:
            merged.append(link)
    return merged


def _refresh_attachment_file_refs(attachment: dict[str, Any]) -> dict[str, Any]:
    workspace = attachment.get("workspace") if isinstance(attachment.get("workspace"), dict) else {}
    manifest_raw = str(workspace.get("manifest_path", "")).strip()
    if not manifest_raw:
        return dict(attachment)
    manifest_path = Path(manifest_raw)
    if not manifest_path.is_file():
        return dict(attachment)
    manifest = _read_json_file(manifest_path, {})
    if not isinstance(manifest, dict):
        return dict(attachment)
    parse_manifest = manifest.get("parse") if isinstance(manifest.get("parse"), dict) else {}
    if not parse_manifest:
        return dict(attachment)
    updated = dict(attachment)
    parse = dict(updated.get("parse", {})) if isinstance(updated.get("parse"), dict) else {}
    for key in ("status", "kind", "summary", "error", "ai_analysis_status", "ai_summary", "ai_key_points"):
        value = parse_manifest.get(key)
        if value not in (None, "", [], {}):
            parse[key] = value
    updated["parse"] = parse
    artifacts = dict(updated.get("artifacts", {})) if isinstance(updated.get("artifacts"), dict) else {}
    artifacts.update(_artifact_snapshot_from_manifest(parse_manifest))
    updated["artifacts"] = artifacts
    return updated


def _artifact_snapshot_from_manifest(parse_manifest: dict[str, Any]) -> dict[str, Any]:
    analysis_path = Path(str(parse_manifest.get("analysis_path", "")).strip())
    analysis = _read_json_file(analysis_path, {}) if analysis_path.is_file() else {}
    if not isinstance(analysis, dict):
        analysis = {}
    snapshot = {
        "content_path": str(parse_manifest.get("content_path", "")),
        "full_text_path": str(parse_manifest.get("full_text_path", "")),
        "analysis_path": str(parse_manifest.get("analysis_path", "")),
        "parse_result_path": str(Path(str(parse_manifest.get("analysis_path", ""))).with_name("parse_result.json"))
        if parse_manifest.get("analysis_path")
        else "",
        "ai_analysis_status": str(analysis.get("ai_analysis_status", parse_manifest.get("ai_analysis_status", ""))),
        "ai_summary": str(analysis.get("ai_summary", parse_manifest.get("ai_summary", ""))),
        "ai_key_points": _string_list(analysis.get("ai_key_points", parse_manifest.get("ai_key_points", []))),
        "preview_char_count": int(analysis.get("preview_char_count", parse_manifest.get("preview_char_count", 0)) or 0),
        "char_count": int(analysis.get("char_count", parse_manifest.get("char_count", 0)) or 0),
        "chunks_dir": str(analysis.get("chunks_dir", parse_manifest.get("chunks_dir", ""))),
        "chunk_count": int(analysis.get("chunk_count", parse_manifest.get("chunk_count", 0)) or 0),
        "chunks": _dict_list(analysis.get("chunks", parse_manifest.get("chunks", []))),
        "tables_dir": str(analysis.get("tables_dir", parse_manifest.get("tables_dir", ""))),
        "table_index_path": str(analysis.get("table_index_path", parse_manifest.get("table_index_path", ""))),
        "table_chunk_count": int(analysis.get("table_chunk_count", parse_manifest.get("table_chunk_count", 0)) or 0),
        "table_chunks": _dict_list(analysis.get("table_chunks", parse_manifest.get("table_chunks", []))),
        "ocr_tables_dir": str(analysis.get("ocr_tables_dir", parse_manifest.get("ocr_tables_dir", ""))),
        "ocr_table_index_path": str(analysis.get("ocr_table_index_path", parse_manifest.get("ocr_table_index_path", ""))),
        "ocr_table_chunk_count": int(analysis.get("ocr_table_chunk_count", parse_manifest.get("ocr_table_chunk_count", 0)) or 0),
        "ocr_table_chunks": _dict_list(analysis.get("ocr_table_chunks", parse_manifest.get("ocr_table_chunks", []))),
        "media_dir": str(analysis.get("media_dir", parse_manifest.get("media_dir", ""))),
        "media_index_path": str(analysis.get("media_index_path", parse_manifest.get("media_index_path", ""))),
        "media_extract_count": int(analysis.get("media_extract_count", parse_manifest.get("media_extract_count", 0)) or 0),
        "media_ocr_status": str(analysis.get("media_ocr_status", parse_manifest.get("media_ocr_status", ""))),
        "media_ocr_dir": str(analysis.get("media_ocr_dir", parse_manifest.get("media_ocr_dir", ""))),
        "media_ocr_index_path": str(analysis.get("media_ocr_index_path", parse_manifest.get("media_ocr_index_path", ""))),
        "media_ocr_count": int(analysis.get("media_ocr_count", parse_manifest.get("media_ocr_count", 0)) or 0),
        "media_ocr_error_count": int(analysis.get("media_ocr_error_count", parse_manifest.get("media_ocr_error_count", 0)) or 0),
        "media_asr_status": str(analysis.get("media_asr_status", parse_manifest.get("media_asr_status", ""))),
        "media_asr_dir": str(analysis.get("media_asr_dir", parse_manifest.get("media_asr_dir", ""))),
        "media_asr_count": int(analysis.get("media_asr_count", parse_manifest.get("media_asr_count", 0)) or 0),
        "media_asr_error_count": int(analysis.get("media_asr_error_count", parse_manifest.get("media_asr_error_count", 0)) or 0),
        "media_images": _dict_list(analysis.get("media_images", parse_manifest.get("media_images", []))),
        "media_audio": _dict_list(analysis.get("media_audio", parse_manifest.get("media_audio", []))),
    }
    return {key: value for key, value in snapshot.items() if value not in ("", None, [], {})}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _read_json_file(path: Path, default: Any) -> Any:
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _quote_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    raw = metadata.get("quote") or metadata.get("quoted_message") or {}
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict):
        return {}
    aliases = _quote_aliases(raw)
    quote = {
        "message_id": str(raw.get("message_id") or raw.get("quoted_message_id") or "").strip(),
        "entry_id": str(raw.get("entry_id") or raw.get("quoted_entry_id") or "").strip(),
        "sender_name": str(raw.get("sender_name") or raw.get("quoted_sender_name") or "").strip(),
        "text": str(raw.get("text") or raw.get("quoted_text") or raw.get("content") or "").strip(),
        "received_at": str(raw.get("received_at") or raw.get("quoted_received_at") or "").strip(),
        "source": str(raw.get("source") or "").strip(),
    }
    if aliases:
        quote["message_ids"] = aliases  # type: ignore[assignment]
    return {key: value for key, value in quote.items() if value}


def _links_from_text(text: str) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for url in _URL_RE.findall(text):
        links.append({"url": url, "url_id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:16], "status": "pending"})
    return links


def _links_from_message(message: NormalizedMessage) -> list[dict[str, Any]]:
    if str(message.metadata.get("control_event") or ""):
        return []
    return _links_from_text(_primary_message_text(message))


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


def _combined_annotation_text(summary: str, text: str) -> str:
    parts = [part.strip() for part in (summary, text) if str(part or "").strip()]
    return "\n\n".join(parts)


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
            metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
            if kind.startswith("attachment:"):
                file_id = str(metadata.get("file_id", "")).strip()
                name = str(metadata.get("name", "")).strip()
                note = " ".join(
                    part
                    for part in (
                        f"file_id={file_id}" if file_id else "",
                        f"name={name}" if name else "",
                        "hidden=true",
                        "read=file.read" if file_id else "",
                    )
                    if part
                )
                lines.append(f"[block:{kind}{' ' + note if note else ''}]")
                lines.append("")
                continue
            elif kind == "file:analysis":
                pass
            elif kind.startswith("control:"):
                lines.append(_render_control_block(kind, metadata, item))
                lines.append("")
                continue
            else:
                status = str(metadata.get("status", "")).strip()
                url_id = str(metadata.get("url_id", "")).strip()
                note = " ".join(part for part in (f"url_id={url_id}" if url_id else "", f"status={status}" if status else "") if part)
                lines.append(f"[block:{kind}{' ' + note if note else ''}]")
        if text:
            lines.append(text)
        lines.append("")
    for attachment in item.get("attachments", []):
        if not isinstance(attachment, dict):
            continue
        lines.append(_render_attachment_ref(attachment, item))
    if item.get("attachments"):
        lines.append("")
    for link in item.get("links", []):
        if isinstance(link, dict):
            lines.append(f"[link:{link.get('url_id', '')}] {link.get('url', '')}")
    if item.get("links"):
        lines.append("")
    return lines


def _render_control_block(kind: str, metadata: dict[str, Any], entry: dict[str, Any]) -> str:
    status = str(metadata.get("status") or "applied").strip()
    reset_session_id = str(metadata.get("reset_session_id") or entry.get("session_id") or "").strip()
    message_id = str(entry.get("message_id") or "").strip()
    label = kind.removeprefix("control:") or "event"
    parts = [label]
    if status:
        parts.append(f"status={status}")
    if reset_session_id:
        parts.append(f"session={reset_session_id}")
    if message_id:
        parts.append(f"message_id={message_id}")
    parts.append("hidden_text=true")
    return f"[control:{' '.join(parts)}]"


def _render_attachment_ref(attachment: dict[str, Any], entry: dict[str, Any] | None = None) -> str:
    name = str(attachment.get("name", "")).strip()
    file_id = str(attachment.get("file_id", "")).strip()
    status = str(attachment.get("status", "")).strip()
    kind = str(attachment.get("kind", "")).strip()
    source = str(attachment.get("source", "")).strip()
    artifacts = attachment.get("artifacts") if isinstance(attachment.get("artifacts"), dict) else {}
    parts = []
    if name:
        parts.append(f"name={name}")
    if kind:
        parts.append(f"kind={kind}")
    if status:
        parts.append(f"status={status}")
    if file_id:
        parts.append("read=file.read")
    if entry is not None:
        origin = _entry_attachment_origin(entry)
        direction = "outgoing" if origin in {"agent", "owner_manual"} else "incoming"
        parts.append(f"origin={origin}")
        parts.append(f"direction={direction}")
    send = attachment.get("send") if isinstance(attachment.get("send"), dict) else {}
    send_status = str(send.get("status") or "").strip()
    if send_status:
        parts.append(f"send_status={send_status}")
    bridge_id = str(send.get("bridge_id") or send.get("message_id") or "").strip()
    if bridge_id:
        parts.append(f"bridge_id={bridge_id}")
    reason = str(attachment.get("reason", "")).strip()
    if reason:
        parts.append(f"reason={_compact(reason, 120)}")
    parse = attachment.get("parse") if isinstance(attachment.get("parse"), dict) else {}
    parse_summary = str(parse.get("summary", "")).strip()
    if parse_summary and not file_id:
        parts.append(f"summary={_compact(parse_summary, 180)}")
    for key, label in (
        ("chunk_count", "chunks"),
        ("table_chunk_count", "table_chunks"),
        ("ocr_table_chunk_count", "ocr_tables"),
        ("media_extract_count", "media"),
        ("media_ocr_count", "ocr"),
        ("media_asr_count", "asr"),
    ):
        value = artifacts.get(key, "")
        if value not in ("", None, 0):
            parts.append(f"{label}={value}")
    if file_id:
        return f"[file:{file_id}{' ' + ' '.join(parts) if parts else ''}]"
    if source:
        parts.append(f"source={source}")
    return f"[file:outgoing{' ' + ' '.join(parts) if parts else ''}]"


def _entry_attachment_origin(entry: dict[str, Any]) -> str:
    role = str(entry.get("role") or "")
    if role == "assistant":
        return "agent"
    if entry.get("is_self") or role == "self":
        return "owner_manual"
    return "user"


def _find_entry_by_message_id(entries: list[dict[str, Any]], message_id: str) -> dict[str, Any] | None:
    for item in entries:
        if message_id in _entry_message_ids(item):
            return item
    return None


def _find_entry_by_dedupe_key(entries: list[dict[str, Any]], dedupe_key: str) -> dict[str, Any] | None:
    if not dedupe_key:
        return None
    for item in entries:
        if item.get("role") == "assistant":
            continue
        if str(item.get("dedupe_key") or "") == dedupe_key:
            return item
    return None


def _find_quote_index(entries: list[dict[str, Any]], quote: dict[str, Any]) -> int | None:
    entry_id = str(quote.get("entry_id", "")).strip()
    quote_ids = _quote_aliases(quote)
    quote_text = str(quote.get("text", "")).strip()
    for index, item in enumerate(entries):
        if entry_id and item.get("entry_id") == entry_id:
            return index
        entry_ids = set(_entry_message_ids(item))
        if quote_ids and entry_ids.intersection(quote_ids):
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


def _message_dedupe_key(message: NormalizedMessage) -> str:
    return str(message.metadata.get("dedupe_key") or "").strip()


def _entry_message_ids(payload: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    primary = str(payload.get("message_id") or "").strip()
    if primary:
        ids.append(primary)
    raw_ids = payload.get("message_ids")
    if isinstance(raw_ids, list):
        for item in raw_ids:
            text = str(item or "").strip()
            if text and text not in ids:
                ids.append(text)
    return ids


def _merge_message_ids(existing: dict[str, Any], message_ids: list[str] | str) -> list[str]:
    ids = _entry_message_ids(existing)
    candidates = [message_ids] if isinstance(message_ids, str) else message_ids
    for message_id in candidates:
        for text in _alias_variants(str(message_id or "").strip()):
            if text and text not in ids:
                ids.append(text)
    return ids[-20:]


def _message_aliases(message: NormalizedMessage) -> list[str]:
    aliases: list[str] = []

    def add(value: Any) -> None:
        for text in _alias_variants(str(value or "").strip()):
            if text and text not in aliases:
                aliases.append(text)

    add(message.message_id)
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    for source in _metadata_alias_sources(metadata):
        for key in (
            "message_id",
            "messageId",
            "msg_id",
            "msgId",
            "msgid",
            "server_id",
            "serverId",
            "platformMessageId",
            "newmsgid",
            "newMsgId",
            "local_id",
            "localId",
            "message_key",
            "messageKey",
            "raw_id",
            "rawId",
        ):
            add(source.get(key))
    return aliases[-20:]


def _quote_aliases(raw: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    for source in _metadata_alias_sources(raw):
        for key in (
            "message_id",
            "messageId",
            "quoted_message_id",
            "quotedMessageId",
            "platformMessageId",
            "server_id",
            "serverId",
            "msg_id",
            "msgId",
            "msgid",
            "raw_id",
            "rawId",
            "target_raw_id",
            "targetRawId",
            "local_id",
            "localId",
            "message_key",
            "messageKey",
        ):
            for text in _alias_variants(str(source.get(key) or "").strip()):
                if text and text not in aliases:
                    aliases.append(text)
        raw_ids = source.get("message_ids")
        if isinstance(raw_ids, list):
            for item in raw_ids:
                for text in _alias_variants(str(item or "").strip()):
                    if text and text not in aliases:
                        aliases.append(text)
    return aliases[-20:]


def _metadata_alias_sources(root: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = [root]
    for key in ("ordering", "hook", "source_payload", "raw", "message"):
        value = root.get(key)
        if isinstance(value, dict):
            sources.append(value)
            for nested_key in ("ordering", "hook", "raw", "message"):
                nested = value.get(nested_key)
                if isinstance(nested, dict):
                    sources.append(nested)
    return sources


def _alias_variants(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    variants = [text]
    if text.startswith("weflow:message:"):
        variants.append(text.rsplit(":", 1)[-1])
    if text.startswith("hook:") and ":" in text:
        variants.append(text.rsplit(":", 1)[-1])
    return [item for index, item in enumerate(variants) if item and item not in variants[:index]]


def _entry_attachments(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(attachment) for attachment in item.get("attachments", []) if isinstance(attachment, dict)]


def _dedupe_attachments(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for attachment in attachments:
        key = (
            str(attachment.get("file_id", "")).strip(),
            str(attachment.get("path", "")).strip(),
            str(attachment.get("name", "")).strip(),
        )
        if key != ("", "", "") and key in seen:
            continue
        seen.add(key)
        deduped.append(attachment)
    return deduped


def _message_payload_changed(existing: dict[str, Any], updated: dict[str, Any]) -> bool:
    keys = (
        "dedupe_key",
        "session_id",
        "conversation_type",
        "chat_title",
        "sender_name",
        "sender_wechat_id",
        "is_self",
        "received_at",
        "status",
        "text_blocks",
        "quote",
        "attachments",
        "links",
        "source",
        "role",
        "send",
    )
    for key in keys:
        if existing.get(key) != updated.get(key):
            return True
    return False


def _preferred_chat_title(existing: str, incoming: str) -> str:
    old = str(existing or "").strip()
    new = str(incoming or "").strip()
    if not old:
        return new
    if not new:
        return old
    if not _is_human_title(new) and _is_human_title(old):
        return old
    return new


def _looks_like_wechat_id(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text.startswith(("wxid_", "gh_")) or text.endswith("@chatroom"))


def _is_human_title(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text.lower() in {"unknown", "system", "none", "null"}:
        return False
    return not _looks_like_wechat_id(text)


def _entry_from_payload(payload: dict[str, Any]) -> LedgerEntry:
    return LedgerEntry(
        entry_id=str(payload.get("entry_id", "")),
        message_id=str(payload.get("message_id", "")),
        dedupe_key=str(payload.get("dedupe_key", "")),
        message_ids=_entry_message_ids(payload),
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
    if not text:
        return 0
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    non_cjk = max(0, len(text) - cjk)
    return max(1, cjk + non_cjk // 4)


def _compact(text: str, max_chars: int = 200) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 3)].rstrip() + "..."


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
    message_id: str = "",
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
        send = item.get("send") if isinstance(item.get("send"), dict) else {}
        if str(send.get("status") or "") not in {"queued_to_bridge", "accepted", "sent"}:
            continue
        echo_message_id = str(send.get("echo_message_id") or "")
        if echo_message_id and echo_message_id != str(message_id or ""):
            continue
        if _normalize_for_match(_entry_primary_text(item)) == normalized:
            return item
    return None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

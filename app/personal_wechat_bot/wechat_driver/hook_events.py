from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.runtime.history_fence import history_writer_fences_if_owned
from app.personal_wechat_bot.runtime.process_lock import short_process_lock
from app.personal_wechat_bot.wechat_driver.backend_events import append_backend_event_record


_SOURCE_CHECKPOINT_VERSION = 1
_SOURCE_CHECKPOINT_WINDOW_BYTES = 64 * 1024


@dataclass(frozen=True)
class HookAttachment:
    path: str
    name: str = ""
    kind: str = "file"
    media_id: str = ""
    size: int = 0
    md5: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HookMessageEvent:
    raw_id: str
    event_type: str
    conversation_key: str
    chat_title: str
    sender_name: str
    text: str
    observed_at: str
    is_group: bool = False
    is_self: bool = False
    sender_wechat_id: str = ""
    message_type: str = "text"
    sort_key: str = ""
    server_id: str = ""
    local_id: str = ""
    msg_id: str = ""
    attachments: tuple[HookAttachment, ...] = ()
    voice: dict[str, Any] = field(default_factory=dict)
    quote: dict[str, Any] = field(default_factory=dict)
    recall: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HookImportResult:
    status: str
    source_path: str
    backend_event_path: str
    scanned_count: int
    appended_count: int
    skipped_count: int
    error_count: int
    appended_raw_ids: tuple[str, ...] = ()
    errors: tuple[dict[str, Any], ...] = ()
    source_offset: int = 0
    backend_event_count: int = 0
    imported_sequence_start: int = 0
    imported_sequence_end: int = 0


class HookEventJsonlImporter:
    """Import external WeChat Hook JSONL events into the project event bus.

    This module intentionally does not implement function hooking or process
    injection. It is the stable ingestion boundary for a separate collector.
    """

    def __init__(
        self,
        source_path: str | Path,
        backend_event_path: str | Path,
        *,
        state_path: str | Path | None = None,
    ):
        self.source_path = Path(source_path)
        self.backend_event_path = Path(backend_event_path)
        self.state_path = Path(state_path) if state_path else self.backend_event_path.parent / "hook_events_state.json"

    def import_new(self) -> HookImportResult:
        with history_writer_fences_if_owned(
            (
                self.source_path.parent,
                self.backend_event_path.parent,
                self.state_path.parent,
            ),
            label="hook_event_import",
        ):
            with _state_file_lock(self.state_path.with_suffix(self.state_path.suffix + ".lock")):
                return self._import_new_locked()

    def _import_new_locked(self) -> HookImportResult:
        state = _read_state(self.state_path)
        previous_offset = int(state.get(str(self.source_path), 0) or 0)
        if not self.source_path.exists():
            return HookImportResult(
                status="missing_source",
                source_path=str(self.source_path),
                backend_event_path=str(self.backend_event_path),
                scanned_count=0,
                appended_count=0,
                skipped_count=0,
                error_count=0,
            )

        size = self.source_path.stat().st_size
        offset = previous_offset if 0 <= previous_offset <= size else 0
        if offset == 0 and previous_offset != 0:
            state.pop(_source_line_state_key(self.source_path), None)
        scanned = 0
        appended = 0
        skipped = 0
        errors: list[dict[str, Any]] = []
        appended_raw_ids: list[str] = []
        appended_sequences: list[int] = []
        backend_event_count = _state_int(state, "_backend_event_count")
        if backend_event_count <= 0:
            backend_event_count = _jsonl_line_count(self.backend_event_path)
        import_sequence = int(state.get("_import_sequence", 0) or 0)
        durable_offset = offset
        source_checkpoint: dict[str, Any] = {}
        with self.source_path.open("r", encoding="utf-8") as f:
            try:
                opened_size = int(os.fstat(f.fileno()).st_size)
            except OSError:
                opened_size = size
            if offset > opened_size or (
                offset > 0
                and not _source_checkpoint_matches(
                    state.get(_source_checkpoint_state_key(self.source_path)),
                    _source_checkpoint_from_open_file(f, offset),
                    offset,
                )
            ):
                offset = 0
                durable_offset = 0
                state.pop(_source_line_state_key(self.source_path), None)
            f.seek(offset)
            line_no = _state_int(state, _source_line_state_key(self.source_path))
            if line_no <= 0 and offset > 0:
                line_no = _jsonl_line_count_until(self.source_path, offset)
            durable_line_no = line_no
            while True:
                line_offset = f.tell()
                line = f.readline()
                if line == "":
                    break
                candidate_line_no = line_no + 1
                if not line.strip():
                    line_no = candidate_line_no
                    durable_line_no = line_no
                    durable_offset = f.tell()
                    continue
                scanned += 1
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    if not line.endswith("\n"):
                        scanned -= 1
                        f.seek(line_offset)
                        break
                    skipped += 1
                    errors.append(
                        {
                            "line": candidate_line_no,
                            "type": type(exc).__name__,
                            "message": str(exc),
                            "phase": "parse",
                            "disposition": "skip_poison",
                        }
                    )
                    line_no = candidate_line_no
                    durable_line_no = line_no
                    durable_offset = f.tell()
                    continue
                try:
                    events = _hook_events_from_payload(payload)
                except Exception as exc:
                    # A complete JSON value that cannot be normalized is a
                    # deterministic poison record. Record and advance past it
                    # so one bad collector payload cannot block the stream.
                    skipped += 1
                    errors.append(
                        {
                            "line": candidate_line_no,
                            "type": type(exc).__name__,
                            "message": str(exc),
                            "phase": "normalize",
                            "disposition": "skip_poison",
                        }
                    )
                    line_no = candidate_line_no
                    durable_line_no = line_no
                    durable_offset = f.tell()
                    continue

                persist_failed = False
                line_appended_before = appended
                for batch_index, event in enumerate(events):
                    import_sequence += 1
                    try:
                        raw_id, was_appended = append_backend_event_record(
                            self.backend_event_path,
                            chat_title=event.chat_title,
                            sender_name=event.sender_name,
                            text=event.text,
                            event_type=event.event_type,
                            sender_wechat_id=event.sender_wechat_id,
                            is_self=event.is_self,
                            is_group=event.is_group,
                            attachments=[_backend_attachment(item) for item in event.attachments],
                            voice=event.voice,
                            observed_at=event.observed_at,
                            quote=event.quote,
                            recall=event.recall,
                            raw_id=event.raw_id,
                            source_payload=_source_payload(
                                event,
                                source_path=self.source_path,
                                source_line_no=candidate_line_no,
                                source_offset=line_offset,
                                batch_index=batch_index,
                                batch_count=len(events),
                                import_sequence=import_sequence,
                            ),
                        )
                        if was_appended:
                            appended += 1
                            backend_event_count += 1
                            appended_raw_ids.append(raw_id)
                            appended_sequences.append(import_sequence)
                        else:
                            skipped += 1
                    except Exception as exc:
                        # The source line is not durable until every expanded
                        # event is present in the backend bus. Keep its offset
                        # pending; already-appended siblings are deduplicated on
                        # retry by raw_id.
                        persist_failed = True
                        skipped += 1
                        errors.append(
                            {
                                "line": candidate_line_no,
                                "type": type(exc).__name__,
                                "message": str(exc),
                                "phase": "append",
                                "disposition": "retry",
                                "batch_index": batch_index,
                                "batch_count": len(events),
                                "partial_appended_count": appended - line_appended_before,
                            }
                        )
                        f.seek(line_offset)
                        break
                if persist_failed:
                    break
                line_no = candidate_line_no
                durable_line_no = line_no
                durable_offset = f.tell()
            source_checkpoint = _source_checkpoint_from_open_file(f, durable_offset)

        if errors and errors[-1].get("disposition") == "retry":
            # The append helper may have written the JSONL line before a
            # sidecar/index error surfaced. Reconcile the diagnostic counter
            # from the durable bus while leaving the source offset pending.
            backend_event_count = _jsonl_line_count(self.backend_event_path)
        state[str(self.source_path)] = durable_offset
        state[_source_line_state_key(self.source_path)] = durable_line_no
        if source_checkpoint:
            state[_source_checkpoint_state_key(self.source_path)] = source_checkpoint
        else:
            state.pop(_source_checkpoint_state_key(self.source_path), None)
        state["_import_sequence"] = import_sequence
        state["_backend_event_count"] = backend_event_count
        _write_state(self.state_path, state)
        return HookImportResult(
            status="ok",
            source_path=str(self.source_path),
            backend_event_path=str(self.backend_event_path),
            scanned_count=scanned,
            appended_count=appended,
            skipped_count=skipped,
            error_count=len(errors),
            appended_raw_ids=tuple(appended_raw_ids),
            errors=tuple(errors[:20]),
            source_offset=durable_offset,
            backend_event_count=backend_event_count,
            imported_sequence_start=appended_sequences[0] if appended_sequences else import_sequence,
            imported_sequence_end=appended_sequences[-1] if appended_sequences else import_sequence,
        )


def hook_event_from_payload(payload: dict[str, Any]) -> HookMessageEvent:
    if not isinstance(payload, dict):
        raise ValueError("hook event must be a JSON object")
    raw_payload = payload
    payload = _canonical_payload(payload)
    event_type = _event_type(payload)
    talker = _first_text(
        payload,
        "conversation_key",
        "conversationKey",
        "conversationId",
        "talker_id",
        "talkerId",
        "talker",
        "roomid",
        "room_id",
        "roomId",
        "chat_id",
        "chatId",
        "wxid",
        "fromUserName",
    )
    chat_title = _first_text(
        payload,
        "chat_title",
        "chatTitle",
        "talker_name",
        "talkerName",
        "room_name",
        "roomName",
        "display_name",
        "displayName",
        "nickname",
        "remark",
    )
    if not chat_title:
        chat_title = talker
    if not talker:
        talker = chat_title
    sender_id = _first_text(
        payload,
        "sender_wechat_id",
        "senderWechatId",
        "sender_id",
        "senderId",
        "sender",
        "senderWxid",
        "from_wxid",
        "fromWxid",
        "from_user_name",
        "fromUserName",
        "actual_sender",
        "actualSender",
    )
    sender_name = _first_text(
        payload,
        "sender_name",
        "senderName",
        "sender_display_name",
        "senderDisplayName",
        "from_name",
        "fromName",
        "sender_nickname",
        "senderNickname",
        "remark_name",
        "remarkName",
    )
    if not sender_name:
        sender_name = "system" if event_type == "recall" else (sender_id or "unknown")
    if not talker:
        raise ValueError("conversation_key/talker is required")
    if not sender_name:
        raise ValueError("sender_name or sender id is required")

    text = _content_text(payload)
    raw_id = _raw_id(payload, event_type=event_type, talker=talker, sender=sender_id or sender_name, text=text)
    observed_at = _observed_at(payload)
    attachments = tuple(_attachments(payload))
    voice = _voice_payload(payload, attachments=attachments)
    quote = _quote_payload(payload)
    recall = _recall_payload(payload)
    return HookMessageEvent(
        raw_id=raw_id,
        event_type=event_type,
        conversation_key=talker,
        chat_title=chat_title,
        sender_name=sender_name,
        text=text,
        observed_at=observed_at,
        is_group=_is_group(payload, talker),
        is_self=_is_self(payload),
        sender_wechat_id=sender_id,
        message_type=_first_text(payload, "message_type", "messageType", "msg_type", "msgType", "type") or "text",
        sort_key=_first_text(payload, "sort_key", "sortKey", "sort_seq", "sortSeq", "seq", "sequence"),
        server_id=_first_text(payload, "server_id", "serverId"),
        local_id=_first_text(payload, "local_id", "localId"),
        msg_id=_first_text(payload, "msg_id", "msgId", "msgid", "message_id", "messageId"),
        attachments=attachments,
        voice=voice,
        quote=quote,
        recall=recall,
        raw=raw_payload,
    )


def _event_type(payload: dict[str, Any]) -> str:
    value = _first_text(payload, "event_type", "eventType", "event", "kind") or "message"
    value = value.lower()
    if value in {"revoke", "revoked", "recall", "delete", "deleted"}:
        return "recall"
    content = _content_text(payload)
    msg_type = _first_text(payload, "message_type", "messageType", "msg_type", "msgType", "type", "typeName").lower()
    if "revokemsg" in content.lower() or (msg_type == "10002" and "撤回" in content):
        return "recall"
    return "message"


def _content_text(payload: dict[str, Any]) -> str:
    for key in (
        "text",
        "parsed_content",
        "parsedContent",
        "content_text",
        "contentText",
        "plain_text",
        "plainText",
        "display_content",
        "displayContent",
        "message_content",
        "messageContent",
        "msg_content",
        "msgContent",
        "str_content",
        "strContent",
        "content",
        "raw_content",
        "rawContent",
        "message",
        "msg",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    content = payload.get("content")
    if isinstance(content, dict):
        return _first_text(content, "text", "title", "desc", "description")
    return ""


def _raw_id(payload: dict[str, Any], *, event_type: str, talker: str, sender: str, text: str) -> str:
    raw_id = _first_text(payload, "raw_id", "rawId", "event_id", "eventId")
    if raw_id:
        return raw_id
    msg_id = _first_text(payload, "msg_id", "msgId", "msgid", "message_id", "messageId", "id")
    server_id = _first_text(payload, "server_id", "serverId", "server_msg_id", "serverMsgId", "newmsgid", "newMsgId")
    local_id = _first_text(payload, "local_id", "localId", "local_msg_id", "localMsgId")
    message_key = _first_text(payload, "message_key", "messageKey")
    sort_key = _first_text(payload, "sort_key", "sortKey", "sort_seq", "sortSeq", "seq", "sequence", "sortSequence")
    if msg_id or server_id or local_id or message_key or sort_key:
        return ":".join(item for item in ("hook", event_type, talker, msg_id, server_id, local_id, message_key, sort_key) if item)
    seed = json.dumps(
        {
            "event_type": event_type,
            "talker": talker,
            "sender": sender,
            "text": text,
            "time": _observed_at(payload),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "hook:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _observed_at(payload: dict[str, Any]) -> str:
    value = _time_text(payload, "observed_at", "observedAt", "create_time", "createTime", "createTimeMs", "time", "timestamp", "ts")
    return value or utc_now_iso()


def _is_group(payload: dict[str, Any], talker: str) -> bool:
    for key in ("is_group", "isGroup", "group"):
        if key in payload:
            return _truthy(payload.get(key))
    return talker.endswith("@chatroom") or "@chatroom" in talker


def _is_self(payload: dict[str, Any]) -> bool:
    for key in ("is_self", "isSelf", "from_me", "fromMe", "self"):
        if key in payload:
            return _truthy(payload.get(key))
    return False


def _attachments(payload: dict[str, Any]) -> list[HookAttachment]:
    raw_items = payload.get("attachments")
    if raw_items is None:
        raw_items = payload.get("attachment")
    if raw_items is None:
        raw_items = payload.get("files")
    if raw_items is None:
        raw_items = []
    if isinstance(raw_items, (str, dict)):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raw_items = []
    for implicit in _implicit_attachment_payloads(payload):
        raw_items.append(implicit)
    result: list[HookAttachment] = []
    for item in raw_items:
        if isinstance(item, str):
            path = item.strip()
            if path:
                result.append(HookAttachment(path=path, name=Path(path).name))
            continue
        if not isinstance(item, dict):
            continue
        path = _first_text(item, "path", "file_path", "filePath", "local_path", "localPath", "url")
        name = _first_text(item, "name", "filename", "file_name", "fileName", "original_name", "originalName")
        if not path and not name:
            continue
        result.append(
            HookAttachment(
                path=path,
                name=name or Path(path).name,
                kind=_first_text(item, "kind", "type", "media_type", "mediaType") or "file",
                media_id=_first_text(item, "media_id", "mediaId", "md5", "aeskey"),
                size=_safe_int(item.get("size") or item.get("file_size") or item.get("fileSize")),
                md5=_first_text(item, "md5", "hash", "sha256"),
                raw=item,
            )
        )
    return result


def _voice_payload(payload: dict[str, Any], *, attachments: tuple[HookAttachment, ...]) -> dict[str, Any]:
    raw = payload.get("voice") if isinstance(payload.get("voice"), dict) else {}
    source = _first_text(raw, "source") or _first_text(payload, "voice_source", "voiceSource")
    voice_text = ""
    if _trusted_voice_text_source(source):
        voice_text = _first_text(raw, "text", "transcript") or _first_text(payload, "voice_text", "voiceText", "transcript")
    audio_path = _first_text(raw, "audio_path", "audioPath", "path") or _first_text(payload, "voice_audio_path", "voiceAudioPath", "mediaLocalPath")
    audio_name = _first_text(raw, "audio_name", "audioName", "name") or _first_text(payload, "voice_audio_name", "voiceAudioName")
    duration = _first_text(raw, "duration") or _first_text(payload, "voice_duration", "voiceDuration")
    message_type = _first_text(payload, "message_type", "messageType", "msg_type", "msgType", "type", "typeName").lower()
    local_type = _first_text(payload, "local_type", "localType").lower()
    media_type = _first_text(payload, "media_type", "mediaType").lower()
    looks_like_voice = message_type in {"voice", "audio", "34"} or local_type == "34" or media_type in {"voice", "audio"}
    if not audio_path:
        audio = next((item for item in attachments if item.kind in {"voice", "audio"}), None)
        if audio is not None:
            audio_path = audio.path
            audio_name = audio.name
    status = _first_text(raw, "status") or _first_text(payload, "voice_status", "voiceStatus")
    if not status and (voice_text or audio_path or duration or looks_like_voice):
        status = "transcribed" if voice_text else "pending"
    if not status:
        return {}
    return {
        key: value
        for key, value in {
            "status": status,
            "source": source or "wechat_native_event",
            "text": voice_text,
            "duration": duration,
            "audio_path": audio_path,
            "audio_name": audio_name,
        }.items()
        if value
    }


def _trusted_voice_text_source(source: str) -> bool:
    value = str(source or "").strip().lower()
    if not value:
        return False
    return value.startswith("local_asr") or value in {"manual_voice_transcript", "voice.local_asr", "file_workspace_local_asr"}


def _quote_payload(payload: dict[str, Any]) -> dict[str, Any]:
    quote = payload.get("quote")
    if not isinstance(quote, dict):
        quote = {}
    aliases = [
        value
        for value in (
            _first_text(quote, "message_id", "messageId", "quoted_message_id", "quotedMessageId"),
            _first_text(quote, "platformMessageId", "server_id", "serverId"),
            _first_text(quote, "raw_id", "rawId"),
            _first_text(quote, "local_id", "localId"),
            _first_text(quote, "message_key", "messageKey"),
        )
        if value
    ]
    raw_aliases = quote.get("message_ids")
    if isinstance(raw_aliases, list):
        aliases.extend(str(item).strip() for item in raw_aliases if str(item).strip())
    result = {
        "message_id": aliases[0] if aliases else "",
        "message_ids": aliases,
        "text": _first_text(quote, "text", "content", "quoted_text", "quotedText"),
        "sender_name": _first_text(quote, "sender_name", "senderName", "quoted_sender_name", "quotedSenderName"),
        "received_at": _first_text(quote, "received_at", "receivedAt", "create_time", "createTime"),
    }
    cleaned = {key: value for key, value in result.items() if value}
    return {**cleaned, "source": "wechat_native_event"} if cleaned else {}


def _recall_payload(payload: dict[str, Any]) -> dict[str, Any]:
    recall = payload.get("recall")
    if not isinstance(recall, dict):
        recall = payload
    xml_recall = _recall_from_xml(_content_text(payload))
    result = {
        "target_raw_id": _first_text(recall, "target_raw_id", "targetRawId", "recalled_raw_id", "recalledRawId"),
        "target_message_id": _first_text(
            recall,
            "target_message_id",
            "targetMessageId",
            "recalled_message_id",
            "recalledMessageId",
            "old_msg_id",
            "oldMsgId",
        ),
        "reason": _first_text(recall, "reason"),
    }
    cleaned = {key: value for key, value in {**xml_recall, **result}.items() if value}
    if cleaned or _event_type(payload) == "recall":
        cleaned.setdefault("reason", "wechat_recall")
        return cleaned
    return {}


def _source_payload(
    event: HookMessageEvent,
    *,
    source_path: Path | None = None,
    source_line_no: int = 0,
    source_offset: int = 0,
    batch_index: int = 0,
    batch_count: int = 1,
    import_sequence: int = 0,
) -> dict[str, Any]:
    source = str(event.raw.get("source") or "wechat_native_jsonl")
    upstream_source_payload = event.raw.get("source_payload")
    upstream_source_payload = dict(upstream_source_payload) if isinstance(upstream_source_payload, dict) else {}
    upstream_source_payload.pop("hook", None)
    source_file = str(source_path) if source_path is not None else ""
    message_key = _first_text(event.raw, "message_key", "messageKey")
    create_time = _first_text(event.raw, "create_time", "createTime")
    local_type = _first_text(event.raw, "local_type", "localType")
    media_export_path = _first_text(event.raw, "media_export_path", "mediaExportPath")
    context_only = bool(event.raw.get("context_only") or event.raw.get("contextOnly"))
    return {
        **upstream_source_payload,
        "source": source,
        "adapter": source,
        "conversation_key": event.conversation_key,
        "talker_id": event.conversation_key,
        "message_type": event.message_type,
        "sort_key": event.sort_key,
        "server_id": event.server_id,
        "local_id": event.local_id,
        "message_key": message_key,
        "create_time": create_time,
        "local_type": local_type,
        "msg_id": event.msg_id,
        "media_export_path": media_export_path,
        "context_only": context_only,
        "source_path": source_file,
        "source_line_no": source_line_no,
        "source_offset": source_offset,
        "batch_index": batch_index,
        "batch_count": batch_count,
        "import_sequence": import_sequence,
        "hook": {
            "event_type": event.event_type,
            "message_type": event.message_type,
            "sort_key": event.sort_key,
            "server_id": event.server_id,
            "local_id": event.local_id,
            "message_key": message_key,
            "create_time": create_time,
            "local_type": local_type,
            "msg_id": event.msg_id,
            "media_export_path": media_export_path,
            "context_only": context_only,
            "source_path": source_file,
            "source_line_no": source_line_no,
            "source_offset": source_offset,
            "batch_index": batch_index,
            "batch_count": batch_count,
            "import_sequence": import_sequence,
            "ordering": {
                "conversation_key": event.conversation_key,
                "observed_at": event.observed_at,
                "sort_key": event.sort_key,
                "server_id": event.server_id,
                "local_id": event.local_id,
                "message_key": message_key,
                "create_time": create_time,
                "local_type": local_type,
                "msg_id": event.msg_id,
                "context_only": context_only,
                "source_line_no": source_line_no,
                "source_offset": source_offset,
                "batch_index": batch_index,
                "import_sequence": import_sequence,
            },
            "raw": event.raw,
            "attachments": [asdict(item) for item in event.attachments],
        },
    }


def _hook_events_from_payload(payload: Any) -> list[HookMessageEvent]:
    items = _payload_items(payload)
    return [hook_event_from_payload(item) for item in items]


def _payload_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise ValueError("hook event must be a JSON object")
    for key in ("messages", "items", "events"):
        value = payload.get(key)
        if isinstance(value, list):
            parent = {parent_key: parent_value for parent_key, parent_value in payload.items() if parent_key != key}
            return [{**parent, **item} for item in value if isinstance(item, dict)]
    return [payload]


def _canonical_payload(payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload)
    for key in ("data", "payload", "message", "msg", "wx_msg", "wxMsg", "extra"):
        value = payload.get(key)
        if isinstance(value, dict):
            merged = {**value, **merged}
    if isinstance(payload.get("content"), dict):
        merged = {**payload["content"], **merged}
    return merged


def _backend_attachment(item: HookAttachment) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "path": item.path,
            "original_name": item.name,
            "kind": item.kind,
        }.items()
        if value
    }


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _time_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None:
            continue
        parsed = _epoch_iso(value)
        if parsed:
            return parsed
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _epoch_iso(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    if number > 10_000_000_000:
        number = number / 1000
    try:
        return datetime.fromtimestamp(number, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _implicit_attachment_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    message_type = _first_text(payload, "message_type", "messageType", "msg_type", "msgType", "type", "typeName").lower()
    media_kind = _media_kind(message_type)
    result: list[dict[str, Any]] = []
    for key in ("path", "file_path", "filePath", "local_path", "localPath", "media_path", "mediaPath", "thumb", "extra"):
        value = _first_text(payload, key)
        if value and _looks_like_path(value):
            result.append({"path": value, "kind": media_kind or "file"})
    return result


def _media_kind(message_type: str) -> str:
    if message_type in {"image", "img", "3"}:
        return "image"
    if message_type in {"voice", "audio", "34"}:
        return "audio"
    if message_type in {"video", "43", "62"}:
        return "video"
    if message_type in {"file", "app", "49"}:
        return "file"
    return ""


def _looks_like_path(value: str) -> bool:
    text = value.strip()
    if not text or "<" in text[:5]:
        return False
    return bool(re.search(r"(^[A-Za-z]:[\\/])|([\\/])|(\.[A-Za-z0-9]{2,6}$)", text))


def _recall_from_xml(text: str) -> dict[str, Any]:
    if "revokemsg" not in text.lower():
        return {}
    result: dict[str, Any] = {}
    for key, out_key in (("newmsgid", "target_message_id"), ("msgid", "target_message_id"), ("session", "conversation_key")):
        value = _xml_tag_text(text, key)
        if value and out_key not in result:
            result[out_key] = value
    replace = _xml_tag_text(text, "replacemsg")
    if replace:
        result["reason"] = replace
    return result


def _xml_tag_text(text: str, tag: str) -> str:
    pattern = re.compile(rf"<{tag}[^>]*>(.*?)</{tag}>", re.IGNORECASE | re.DOTALL)
    match = pattern.search(text)
    if not match:
        return ""
    value = match.group(1).strip()
    if value.startswith("<![CDATA[") and value.endswith("]]>"):
        value = value[9:-3].strip()
    return value


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _source_line_state_key(source_path: Path) -> str:
    return f"{source_path}:line_no"


def _source_checkpoint_state_key(source_path: Path) -> str:
    return f"{source_path}:checkpoint"


def _source_checkpoint_from_open_file(source: Any, offset: int) -> dict[str, Any]:
    try:
        stat = os.fstat(source.fileno())
    except (AttributeError, OSError):
        return {}
    consumed = int(offset)
    if consumed < 0 or consumed > int(stat.st_size):
        return {}
    duplicate = -1
    try:
        duplicate = os.dup(source.fileno())
        with os.fdopen(duplicate, "rb", closefd=True) as raw:
            duplicate = -1
            fingerprint = _source_prefix_fingerprint(raw, consumed)
    except OSError:
        return {}
    finally:
        if duplicate >= 0:
            try:
                os.close(duplicate)
            except OSError:
                pass
    if not fingerprint:
        return {}
    return {
        "version": _SOURCE_CHECKPOINT_VERSION,
        "offset": consumed,
        "source_device": int(getattr(stat, "st_dev", 0) or 0),
        "source_file_id": int(getattr(stat, "st_ino", 0) or 0),
        "source_size": int(stat.st_size),
        "consumed_fingerprint": fingerprint,
    }


def _source_checkpoint_matches(value: Any, current: dict[str, Any], offset: int) -> bool:
    if not isinstance(value, dict) or not current:
        return False
    try:
        if int(value.get("version", 0) or 0) != _SOURCE_CHECKPOINT_VERSION:
            return False
        if int(value.get("offset", -1)) != int(offset):
            return False
        stored_device = int(value.get("source_device", 0) or 0)
        stored_file_id = int(value.get("source_file_id", 0) or 0)
    except (TypeError, ValueError):
        return False
    current_device = int(current.get("source_device", 0) or 0)
    current_file_id = int(current.get("source_file_id", 0) or 0)
    if stored_file_id and current_file_id and (
        stored_device != current_device or stored_file_id != current_file_id
    ):
        return False
    return str(value.get("consumed_fingerprint") or "") == str(current.get("consumed_fingerprint") or "")


def _source_prefix_fingerprint(source: Any, offset: int) -> str:
    consumed = max(0, int(offset))
    digest = hashlib.sha256()
    digest.update(f"hook-source-v{_SOURCE_CHECKPOINT_VERSION}:{consumed}:".encode("ascii"))
    try:
        source.seek(0)
        head = source.read(min(consumed, _SOURCE_CHECKPOINT_WINDOW_BYTES))
        if len(head) != min(consumed, _SOURCE_CHECKPOINT_WINDOW_BYTES):
            return ""
        digest.update(head)
        if consumed > _SOURCE_CHECKPOINT_WINDOW_BYTES:
            source.seek(max(0, consumed - _SOURCE_CHECKPOINT_WINDOW_BYTES))
            tail = source.read(_SOURCE_CHECKPOINT_WINDOW_BYTES)
            if len(tail) != _SOURCE_CHECKPOINT_WINDOW_BYTES:
                return ""
            digest.update(b"\0tail\0")
            digest.update(tail)
    except (AttributeError, OSError):
        return ""
    return digest.hexdigest()


def _state_int(state: dict[str, Any], key: str) -> int:
    try:
        return int(state.get(key, 0) or 0)
    except Exception:
        return 0


@contextmanager
def _state_file_lock(path: Path, *, timeout_seconds: float = 30.0) -> Iterator[None]:
    with short_process_lock(
        path,
        timeout_seconds=timeout_seconds,
        stale_after_seconds=60.0,
        timeout_label="hook import state lock",
    ):
        yield


def _jsonl_line_count(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
    except OSError:
        return 0
    return count


def _jsonl_line_count_until(path: Path, offset: int) -> int:
    if offset <= 0 or not path.exists():
        return 0
    count = 0
    read_bytes = 0
    try:
        with path.open("rb") as f:
            while read_bytes < offset:
                chunk = f.read(min(1024 * 1024, offset - read_bytes))
                if not chunk:
                    break
                read_bytes += len(chunk)
                count += chunk.count(b"\n")
    except OSError:
        return 0
    return count

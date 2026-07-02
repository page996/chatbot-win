from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.wechat_driver.backend_events import append_backend_event


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
        scanned = 0
        appended = 0
        skipped = 0
        errors: list[dict[str, Any]] = []
        appended_raw_ids: list[str] = []
        backend_event_count = _jsonl_line_count(self.backend_event_path)
        import_sequence = int(state.get("_import_sequence", 0) or 0)
        first_import_sequence = import_sequence + 1
        with self.source_path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            line_no = _jsonl_line_count_until(self.source_path, offset)
            while True:
                line_offset = f.tell()
                line = f.readline()
                if line == "":
                    break
                line_no += 1
                if not line.strip():
                    continue
                scanned += 1
                try:
                    payload = json.loads(line)
                    events = _hook_events_from_payload(payload)
                    for batch_index, event in enumerate(events):
                        import_sequence += 1
                        raw_id = append_backend_event(
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
                                source_line_no=line_no,
                                source_offset=line_offset,
                                batch_index=batch_index,
                                batch_count=len(events),
                                import_sequence=import_sequence,
                            ),
                        )
                        appended += 1
                        backend_event_count += 1
                        appended_raw_ids.append(raw_id)
                except json.JSONDecodeError as exc:
                    if not line.endswith("\n"):
                        scanned -= 1
                        f.seek(line_offset)
                        break
                    skipped += 1
                    errors.append({"line": line_no, "type": type(exc).__name__, "message": str(exc)})
                except Exception as exc:
                    skipped += 1
                    errors.append({"line": line_no, "type": type(exc).__name__, "message": str(exc)})
            new_offset = f.tell()

        state[str(self.source_path)] = new_offset
        state["_import_sequence"] = import_sequence
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
            source_offset=new_offset,
            backend_event_count=backend_event_count,
            imported_sequence_start=first_import_sequence if appended else import_sequence,
            imported_sequence_end=import_sequence,
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
            "source": "wechat_hook_event",
            "text": voice_text,
            "duration": duration,
            "audio_path": audio_path,
            "audio_name": audio_name,
        }.items()
        if value
    }


def _quote_payload(payload: dict[str, Any]) -> dict[str, Any]:
    quote = payload.get("quote")
    if not isinstance(quote, dict):
        quote = {}
    result = {
        "message_id": _first_text(quote, "message_id", "messageId", "quoted_message_id", "quotedMessageId"),
        "text": _first_text(quote, "text", "content", "quoted_text", "quotedText"),
        "sender_name": _first_text(quote, "sender_name", "senderName", "quoted_sender_name", "quotedSenderName"),
        "received_at": _first_text(quote, "received_at", "receivedAt", "create_time", "createTime"),
    }
    cleaned = {key: value for key, value in result.items() if value}
    return {**cleaned, "source": "wechat_hook_event"} if cleaned else {}


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
    source = str(event.raw.get("source") or "wechat_hook_jsonl")
    source_file = str(source_path) if source_path is not None else ""
    message_key = _first_text(event.raw, "message_key", "messageKey")
    create_time = _first_text(event.raw, "create_time", "createTime")
    local_type = _first_text(event.raw, "local_type", "localType")
    media_export_path = _first_text(event.raw, "media_export_path", "mediaExportPath")
    context_only = bool(event.raw.get("context_only") or event.raw.get("contextOnly"))
    return {
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
    for key in ("data", "payload", "message", "msg", "wx_msg", "wxMsg", "wcf_message", "wcfMessage", "extra"):
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


@contextmanager
def _state_file_lock(path: Path, *, timeout_seconds: float = 30.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _stale_lock(path):
                try:
                    path.unlink()
                    continue
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for hook import state lock: {path}")
            time.sleep(0.025)
    try:
        os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _stale_lock(path: Path, *, max_age_seconds: float = 60.0) -> bool:
    try:
        return time.time() - path.stat().st_mtime > max_age_seconds
    except OSError:
        return False


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

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

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
        with self.source_path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            for line_no, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                scanned += 1
                try:
                    payload = json.loads(line)
                    event = hook_event_from_payload(payload)
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
                        source_payload=_source_payload(event),
                    )
                    appended += 1
                    appended_raw_ids.append(raw_id)
                except Exception as exc:
                    skipped += 1
                    errors.append({"line": line_no, "type": type(exc).__name__, "message": str(exc)})
            new_offset = f.tell()

        state[str(self.source_path)] = new_offset
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
        )


def hook_event_from_payload(payload: dict[str, Any]) -> HookMessageEvent:
    if not isinstance(payload, dict):
        raise ValueError("hook event must be a JSON object")
    event_type = _event_type(payload)
    talker = _first_text(payload, "conversation_key", "conversationKey", "talker_id", "talkerId", "talker", "roomid", "room_id", "wxid")
    chat_title = _first_text(payload, "chat_title", "chatTitle", "talker_name", "talkerName", "room_name", "roomName", "display_name", "displayName")
    if not chat_title:
        chat_title = talker
    if not talker:
        talker = chat_title
    sender_id = _first_text(payload, "sender_wechat_id", "senderWechatId", "sender_id", "senderId", "sender", "from_wxid", "fromWxid")
    sender_name = _first_text(payload, "sender_name", "senderName", "sender_display_name", "senderDisplayName", "from_name", "fromName")
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
        raw=payload,
    )


def _event_type(payload: dict[str, Any]) -> str:
    value = _first_text(payload, "event_type", "eventType", "event", "kind") or "message"
    value = value.lower()
    if value in {"revoke", "revoked", "recall", "delete", "deleted"}:
        return "recall"
    return "message"


def _content_text(payload: dict[str, Any]) -> str:
    for key in ("text", "content", "message", "msg", "message_content", "messageContent"):
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
    msg_id = _first_text(payload, "msg_id", "msgId", "msgid", "message_id", "messageId")
    server_id = _first_text(payload, "server_id", "serverId")
    local_id = _first_text(payload, "local_id", "localId")
    sort_key = _first_text(payload, "sort_key", "sortKey", "sort_seq", "sortSeq", "seq", "sequence")
    if msg_id or server_id or local_id or sort_key:
        return ":".join(item for item in ("hook", event_type, talker, msg_id, server_id, local_id, sort_key) if item)
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
    value = _first_text(payload, "observed_at", "observedAt", "create_time", "createTime", "time", "timestamp")
    return value or utc_now_iso()


def _is_group(payload: dict[str, Any], talker: str) -> bool:
    if "is_group" in payload:
        return bool(payload.get("is_group"))
    if "isGroup" in payload:
        return bool(payload.get("isGroup"))
    return talker.endswith("@chatroom") or "@chatroom" in talker


def _is_self(payload: dict[str, Any]) -> bool:
    for key in ("is_self", "isSelf", "from_me", "fromMe", "self"):
        if key in payload:
            return bool(payload.get(key))
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
    audio_path = _first_text(raw, "audio_path", "audioPath", "path") or _first_text(payload, "voice_audio_path", "voiceAudioPath")
    audio_name = _first_text(raw, "audio_name", "audioName", "name") or _first_text(payload, "voice_audio_name", "voiceAudioName")
    duration = _first_text(raw, "duration") or _first_text(payload, "voice_duration", "voiceDuration")
    if not audio_path:
        audio = next((item for item in attachments if item.kind in {"voice", "audio"}), None)
        if audio is not None:
            audio_path = audio.path
            audio_name = audio.name
    status = _first_text(raw, "status") or _first_text(payload, "voice_status", "voiceStatus")
    if not status and (voice_text or audio_path or duration):
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
        "source": "wechat_hook_event",
    }
    return {key: value for key, value in result.items() if value}


def _recall_payload(payload: dict[str, Any]) -> dict[str, Any]:
    recall = payload.get("recall")
    if not isinstance(recall, dict):
        recall = payload
    result = {
        "target_raw_id": _first_text(recall, "target_raw_id", "targetRawId", "recalled_raw_id", "recalledRawId"),
        "target_message_id": _first_text(recall, "target_message_id", "targetMessageId", "recalled_message_id", "recalledMessageId"),
        "reason": _first_text(recall, "reason") or "wechat_recall",
    }
    return {key: value for key, value in result.items() if value}


def _source_payload(event: HookMessageEvent) -> dict[str, Any]:
    return {
        "source": "wechat_hook_jsonl",
        "conversation_key": event.conversation_key,
        "talker_id": event.conversation_key,
        "message_type": event.message_type,
        "sort_key": event.sort_key,
        "server_id": event.server_id,
        "local_id": event.local_id,
        "msg_id": event.msg_id,
        "hook": {
            "event_type": event.event_type,
            "message_type": event.message_type,
            "sort_key": event.sort_key,
            "server_id": event.server_id,
            "local_id": event.local_id,
            "msg_id": event.msg_id,
            "raw": event.raw,
            "attachments": [asdict(item) for item in event.attachments],
        },
    }


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


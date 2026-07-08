from __future__ import annotations

import hashlib
import json
import re

from app.personal_wechat_bot.domain.models import NormalizedMessage, RawWeChatMessage


class MessageNormalizer:
    def normalize(self, raw: RawWeChatMessage) -> NormalizedMessage | None:
        text = raw.text.strip()
        if not text and not raw.driver_meta.get("allow_empty_message"):
            return None
        conversation_type = "group" if raw.is_group else "private"
        conversation_key = _conversation_key(raw.driver_meta, raw.chat_title)
        conversation_id = conversation_id_for(conversation_type, conversation_key)
        metadata = dict(raw.driver_meta)
        raw_key = raw.raw_id.strip()
        message_key = raw_key or f"{raw.sender_name}:{raw.sender_wechat_id or ''}:{text}:{raw.observed_at}"
        message_id = _stable_hash(f"{message_key}:{conversation_id}")
        dedupe_key = message_dedupe_key(raw, conversation_id=conversation_id)
        if dedupe_key:
            metadata["dedupe_key"] = dedupe_key
        if raw_key:
            metadata["raw_id"] = raw_key
        return NormalizedMessage(
            message_id=message_id,
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            chat_title=raw.chat_title,
            sender_name=raw.sender_name,
            sender_wechat_id=raw.sender_wechat_id,
            text=text,
            is_self=raw.is_self,
            received_at=raw.observed_at,
            metadata=metadata,
        )


def conversation_id_for(conversation_type: str, chat_title: str) -> str:
    return _stable_hash(f"{conversation_type}:{chat_title}")


def message_dedupe_key(raw: RawWeChatMessage, *, conversation_id: str) -> str:
    meta = raw.driver_meta if isinstance(raw.driver_meta, dict) else {}
    event_type = str(meta.get("event_type") or "message").strip() or "message"
    stable_server_id = _nonzero_text(
        _first_metadata_text(meta, "server_id", "serverId", "platformMessageId", "newmsgid")
    )
    if stable_server_id:
        return _stable_hash(f"dedupe:{conversation_id}:{event_type}:server:{stable_server_id}")

    message_key = _first_metadata_text(meta, "message_key", "messageKey")
    if message_key and not _looks_like_path(message_key):
        return _stable_hash(f"dedupe:{conversation_id}:{event_type}:message_key:{message_key}")

    local_id = _first_metadata_text(meta, "local_id", "localId")
    create_time = _first_metadata_text(meta, "create_time", "createTime")
    sort_key = _first_metadata_text(meta, "sort_key", "sortSeq", "sortSequence")
    if local_id and (create_time or sort_key):
        return _stable_hash(
            f"dedupe:{conversation_id}:{event_type}:local:{local_id}:{create_time}:{sort_key}:{raw.sender_wechat_id or raw.sender_name}:{raw.is_self}"
        )

    raw_id = raw.raw_id.strip()
    if raw_id and not _looks_like_path(raw_id):
        return _stable_hash(f"dedupe:{conversation_id}:{event_type}:raw:{raw_id}")

    seed = {
        "conversation_id": conversation_id,
        "event_type": event_type,
        "sender_id": raw.sender_wechat_id or "",
        "sender_name": raw.sender_name,
        "is_self": raw.is_self,
        "observed_at": raw.observed_at,
        "text": _normalize_text(raw.text),
        "attachments": _attachment_fingerprint(meta),
        "voice": _voice_fingerprint(meta),
    }
    return _stable_hash("dedupe:" + json.dumps(seed, ensure_ascii=False, sort_keys=True))


def _conversation_key(driver_meta: dict[str, object], chat_title: str) -> str:
    """Stable conversation identity, preferring the upstream talker id.

    Must stay aligned with the backend driver so both layers derive the same
    conversation_id. Prefer wxid / roomid so two contacts sharing a display
    name never collapse into one conversation; fall back to the chat title.
    """

    for key in ("conversation_key", "conversationKey", "talker_id", "talkerId", "talker"):
        value = str(driver_meta.get(key) or "").strip()
        if value:
            return value
    return str(chat_title).strip() or chat_title


def _first_metadata_text(meta: dict[str, object], *keys: str) -> str:
    sources: list[dict[str, object]] = [meta]
    for section in ("ordering", "hook", "source_payload"):
        value = meta.get(section)
        if isinstance(value, dict):
            sources.append(value)
    source_payload = meta.get("source_payload")
    if isinstance(source_payload, dict):
        hook = source_payload.get("hook")
        if isinstance(hook, dict):
            sources.append(hook)
            ordering = hook.get("ordering")
            if isinstance(ordering, dict):
                sources.append(ordering)
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, (int, float)):
                return str(value)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _nonzero_text(value: str) -> str:
    text = str(value or "").strip()
    if text in {"", "0", "0.0"}:
        return ""
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return "" if text == "0" else text


def _looks_like_path(value: str) -> bool:
    text = str(value or "").strip()
    lowered = text.lower()
    return bool(
        text
        and (
            ":\\" in text
            or ":/" in text
            or "/" in text
            or "\\" in text
            or "%5c" in lowered
            or "%2f" in lowered
            or "%3a" in lowered
        )
    )


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _attachment_fingerprint(meta: dict[str, object]) -> list[dict[str, str]]:
    raw = meta.get("attachments")
    if not isinstance(raw, list):
        return []
    result: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "file_id": str(item.get("file_id") or "").strip(),
                "md5": str(item.get("md5") or item.get("sha256") or "").strip(),
                "name": str(item.get("name") or item.get("original_name") or "").strip(),
                "kind": str(item.get("kind") or "").strip(),
            }
        )
    return result


def _voice_fingerprint(meta: dict[str, object]) -> dict[str, str]:
    voice = meta.get("voice")
    if not isinstance(voice, dict):
        return {}
    return {
        "duration": str(voice.get("duration") or "").strip(),
        "audio_name": str(voice.get("audio_name") or voice.get("name") or "").strip(),
        "audio_path": str(voice.get("audio_path") or voice.get("path") or "").strip(),
    }


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

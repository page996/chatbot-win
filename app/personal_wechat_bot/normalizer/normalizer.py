from __future__ import annotations

import hashlib

from app.personal_wechat_bot.domain.models import NormalizedMessage, RawWeChatMessage


class MessageNormalizer:
    def normalize(self, raw: RawWeChatMessage) -> NormalizedMessage | None:
        text = raw.text.strip()
        if not text and not raw.driver_meta.get("allow_empty_message"):
            return None
        conversation_type = "group" if raw.is_group else "private"
        conversation_key = _conversation_key(raw.driver_meta, raw.chat_title)
        conversation_id = conversation_id_for(conversation_type, conversation_key)
        raw_key = raw.raw_id.strip()
        message_key = raw_key or f"{raw.sender_name}:{raw.sender_wechat_id or ''}:{text}:{raw.observed_at}"
        message_id = _stable_hash(f"{message_key}:{conversation_id}")
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
            metadata=dict(raw.driver_meta),
        )


def conversation_id_for(conversation_type: str, chat_title: str) -> str:
    return _stable_hash(f"{conversation_type}:{chat_title}")


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


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

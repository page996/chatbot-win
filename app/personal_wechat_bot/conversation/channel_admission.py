from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.domain.models import NormalizedMessage


@dataclass(frozen=True)
class ChannelAdmission:
    allowed: bool
    reason: str
    identity: str = ""


PLACEHOLDER_NAMES = frozenset(
    {
        "unknown",
        "unknown contact",
        "unknown user",
        "unknown friend",
        "wechat user",
        "weixin user",
        "未知",
        "未知联系人",
        "未知用户",
        "微信用户",
        "微信联系人",
        "system",
        "none",
        "null",
    }
)

FRIEND_TRUE_KEYS = (
    "is_friend",
    "isFriend",
    "friend",
    "is_contact",
    "isContact",
    "contact",
    "in_contacts",
    "inContacts",
    "in_address_book",
    "inAddressBook",
)
FRIEND_FALSE_KEYS = (
    "is_friend",
    "isFriend",
    "friend",
    "is_contact",
    "isContact",
    "contact",
    "in_contacts",
    "inContacts",
    "in_address_book",
    "inAddressBook",
)
NON_FRIEND_TRUE_KEYS = (
    "is_stranger",
    "isStranger",
    "stranger",
    "non_friend",
    "nonFriend",
    "is_non_friend",
    "isNonFriend",
    "temporary",
    "is_temporary",
    "isTemporary",
)
RELATION_KEYS = (
    "relationship",
    "relation",
    "contact_status",
    "contactStatus",
    "friend_status",
    "friendStatus",
    "verifyFlag",
    "verify_flag",
)
NON_FRIEND_VALUES = frozenset(
    {
        "unknown",
        "stranger",
        "non_friend",
        "nonfriend",
        "not_friend",
        "temporary",
        "temp",
        "stranger_from_group",
        "group_only",
    }
)
FRIEND_VALUES = frozenset({"friend", "contact", "contacts", "accepted", "verified", "known"})
NON_FRIEND_TEXT_KEYS = (
    "banner",
    "notice",
    "tip",
    "hint",
    "subtitle",
    "description",
    "relation_text",
    "relationText",
    "relationship_text",
    "relationshipText",
    "friend_tip",
    "friendTip",
    "verify_content",
    "verifyContent",
    "status_text",
    "statusText",
)
NON_FRIEND_TEXT_MARKERS = (
    "对方还不是你的朋友",
    "还不是你的朋友",
    "不是你的朋友",
    "not your friend",
    "not a friend",
)


def channel_admission_for_message(
    message: NormalizedMessage,
    config: BotConfig,
    *,
    existing_channel: Any = False,
) -> ChannelAdmission:
    """Decide whether a WeChat conversation is allowed to open a channel."""

    identity = conversation_identity_for_message(message)
    existing_present = bool(existing_channel)
    if message.conversation_type == "group":
        if _identity_accepted(identity, config.accepted_groups, message.chat_title):
            return ChannelAdmission(True, "accepted_group", identity)
        return ChannelAdmission(True, "existing_group_channel" if existing_present else "group_auto_channel", identity)

    if message.conversation_type != "private":
        return ChannelAdmission(True, "non_wechat_channel", identity)

    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    if _explicit_non_friend(metadata):
        return ChannelAdmission(False, "private_contact_explicitly_not_friend", identity)
    if _identity_accepted(
        identity,
        config.accepted_contacts,
        message.chat_title,
        message.sender_name,
        *_existing_channel_titles(existing_channel),
        _existing_channel_identity(existing_channel),
    ):
        return ChannelAdmission(True, "accepted_private_contact", identity)
    existing_payload = _channel_payload(existing_channel)
    if _explicit_friend(metadata) or _explicit_friend(existing_payload):
        return ChannelAdmission(True, "private_contact_verified", identity)
    if _looks_like_private_wechat_receiver(identity):
        return ChannelAdmission(False, "private_contact_unknown_or_unidentified", identity)

    titles = [message.chat_title, message.sender_name, *_existing_channel_titles(existing_channel)]
    if _has_human_private_title(titles, identity):
        return ChannelAdmission(True, "existing_private_channel_identified" if existing_present else "private_display_name_present", identity)
    return ChannelAdmission(False, "private_contact_unknown_or_unidentified", identity)


def channel_admission_for_session(
    session: dict[str, Any],
    config: BotConfig,
    *,
    existing_channel: Any = False,
) -> ChannelAdmission:
    session_id = _first_text(session, "id", "username", "talker", "sessionId", "session_id")
    session_type = _first_text(session, "type", "sessionType", "session_type")
    is_group = session_type in {"group", "2"} or session_id.endswith("@chatroom")
    existing_present = bool(existing_channel)
    if is_group:
        if _identity_accepted(session_id, config.accepted_groups, _first_text(session, "name", "displayName", "remark")):
            return ChannelAdmission(True, "accepted_group", session_id)
        return ChannelAdmission(True, "existing_group_channel" if existing_present else "group_auto_channel", session_id)

    if _explicit_non_friend(session):
        return ChannelAdmission(False, "private_contact_explicitly_not_friend", session_id)
    if _identity_accepted(
        session_id,
        config.accepted_contacts,
        _first_text(session, "name", "displayName", "remark"),
        *_existing_channel_titles(existing_channel),
        _existing_channel_identity(existing_channel),
    ):
        return ChannelAdmission(True, "accepted_private_contact", session_id)
    existing_payload = _channel_payload(existing_channel)
    if _explicit_friend(session) or _explicit_friend(existing_payload):
        return ChannelAdmission(True, "private_contact_verified", session_id)
    if _looks_like_private_wechat_receiver(session_id):
        return ChannelAdmission(False, "private_contact_unknown_or_unidentified", session_id)
    if _has_human_private_title(
        [
            _first_text(session, "remark", "remarkName"),
            _first_text(session, "displayName", "display_name"),
            _first_text(session, "nickName", "nickname"),
            _first_text(session, "name"),
            *_existing_channel_titles(existing_channel),
        ],
        session_id,
    ):
        return ChannelAdmission(True, "existing_private_channel_identified" if existing_present else "private_display_name_present", session_id)
    return ChannelAdmission(False, "private_contact_unknown_or_unidentified", session_id)


def conversation_identity_for_message(message: NormalizedMessage) -> str:
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    for key in ("conversation_key", "conversationKey", "talker_id", "talkerId", "talker"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    return str(message.sender_wechat_id or message.chat_title or message.conversation_id).strip()


def channel_allows_private_receiver(channel: Any, config: BotConfig | None = None) -> bool:
    """True when an existing private channel carries enough identity to send.

    This is the send-bridge backstop for legacy channel files. A trusted source
    alone is not enough: old WeFlow/native discovery could persist a bare wxid
    before the channel-admission policy became stricter.
    """

    if not channel or isinstance(channel, bool):
        return False
    conversation_type = _channel_text(channel, "conversation_type")
    if conversation_type and conversation_type != "private":
        return True
    identity = _existing_channel_identity(channel)
    titles = _existing_channel_titles(channel)
    if config is not None and _identity_accepted(identity, config.accepted_contacts, *titles):
        return True
    payload = channel if isinstance(channel, dict) else {}
    if payload and _explicit_non_friend(payload):
        return False
    if payload and _explicit_friend(payload):
        return True
    if _looks_like_private_wechat_receiver(identity):
        return False
    return _has_human_private_title(titles, identity)


def private_contact_is_explicit_friend(payload: dict[str, Any]) -> bool:
    return _explicit_friend(payload)


def private_contact_is_explicit_non_friend(payload: dict[str, Any]) -> bool:
    return _explicit_non_friend(payload)


def _identity_accepted(identity: str, accepted: set[str], *aliases: str) -> bool:
    candidates = {str(identity or "").strip(), *(str(alias or "").strip() for alias in aliases)}
    candidates.discard("")
    accepted_clean = {str(item or "").strip() for item in accepted if str(item or "").strip()}
    return bool(candidates.intersection(accepted_clean))


def _channel_text(channel: Any, key: str) -> str:
    if isinstance(channel, dict):
        return str(channel.get(key, "") or "").strip()
    return str(getattr(channel, key, "") or "").strip()


def _channel_payload(channel: Any) -> dict[str, Any]:
    if isinstance(channel, dict):
        return channel
    if not channel or isinstance(channel, bool):
        return {}
    return {
        key: getattr(channel, key)
        for key in (
            "conversation_type",
            "chat_title",
            "display_name",
            "sender_names",
            "sender_wechat_ids",
            "conversation_key",
            "conversation_id",
            "is_friend",
            "is_contact",
            "relationship",
            "contact_status",
            "friend_status",
        )
        if hasattr(channel, key)
    }


def _existing_channel_identity(channel: Any) -> str:
    if not channel or isinstance(channel, bool):
        return ""
    if isinstance(channel, dict):
        values = [
            channel.get("conversation_key"),
            *list(channel.get("sender_wechat_ids") if isinstance(channel.get("sender_wechat_ids"), list) else []),
            channel.get("conversation_id"),
        ]
    else:
        values = [
            getattr(channel, "conversation_key", ""),
            *list(getattr(channel, "sender_wechat_ids", []) or []),
            getattr(channel, "conversation_id", ""),
        ]
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _existing_channel_titles(channel: Any) -> list[str]:
    if not channel or isinstance(channel, bool):
        return []
    if isinstance(channel, dict):
        values: list[Any] = [channel.get("chat_title"), channel.get("display_name")]
        sender_names = channel.get("sender_names")
    else:
        values = [getattr(channel, "chat_title", ""), getattr(channel, "display_name", "")]
        sender_names = getattr(channel, "sender_names", [])
    if isinstance(sender_names, list):
        values.extend(sender_names)
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _has_human_private_title(values: list[str], identity: str) -> bool:
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if _looks_like_placeholder(text):
            continue
        if _looks_like_mojibake_title(text):
            continue
        if text == identity and _looks_like_wechat_receiver(identity):
            continue
        if _looks_like_wechat_receiver(text):
            continue
        if not any(ch.isalnum() for ch in text):
            continue
        return True
    return False


def _looks_like_placeholder(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text in PLACEHOLDER_NAMES


def _looks_like_mojibake_title(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    suspicious = ("�", "锟", "馃", "鈥", "鐚", "鍟", "娴", "灏", "鏃", "闀", "涓", "鎺")
    return any(token in text for token in suspicious)


def _looks_like_wechat_receiver(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(text.startswith(("wxid_", "gh_")) or text.endswith("@chatroom") or re.fullmatch(r"\d+@qqim", text))


def _looks_like_private_wechat_receiver(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text.startswith(("wxid_", "gh_")) or re.fullmatch(r"\d+@qqim", text))


def _explicit_friend(payload: dict[str, Any]) -> bool:
    for item in _walk_dicts(payload):
        for key in FRIEND_TRUE_KEYS:
            if key in item and _truthy(item.get(key)):
                return True
        for key in RELATION_KEYS:
            value = str(item.get(key) or "").strip().lower()
            if value in FRIEND_VALUES:
                return True
    return False


def _explicit_non_friend(payload: dict[str, Any]) -> bool:
    for item in _walk_dicts(payload):
        for key in NON_FRIEND_TRUE_KEYS:
            if key in item and _truthy(item.get(key)):
                return True
        for key in FRIEND_FALSE_KEYS:
            if key in item and item.get(key) is False:
                return True
        for key in RELATION_KEYS:
            value = str(item.get(key) or "").strip().lower()
            if value in NON_FRIEND_VALUES:
                return True
        for key in NON_FRIEND_TEXT_KEYS:
            value = str(item.get(key) or "").strip().lower()
            if value and any(marker in value for marker in NON_FRIEND_TEXT_MARKERS):
                return True
    return False


def _walk_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    dicts: list[dict[str, Any]] = []
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            dicts.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return dicts[:100]


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on", "friend", "contact", "verified"}

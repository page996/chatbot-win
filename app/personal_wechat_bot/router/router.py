from __future__ import annotations

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.conversation.channel_admission import channel_admission_for_message
from app.personal_wechat_bot.conversation.channel_store import ConversationChannelStore
from app.personal_wechat_bot.domain.models import NormalizedMessage, RouteDecision
from app.personal_wechat_bot.router.deduper import Deduper


TRUSTED_CHANNEL_SOURCES = frozenset(
    {
        "backend_events_jsonl",
        "backend_file_watcher",
        "manual_backend_event",
    }
)
UNTRUSTED_CHANNEL_SOURCES = frozenset(
    {
        "windows_snapshot",
        "ocr_file_card",
        "poll_ocr_window",
        "ocr_snapshot",
    }
)


class Router:
    def __init__(
        self,
        config: BotConfig,
        deduper: Deduper,
        channel_store: ConversationChannelStore | None = None,
    ):
        self.config = config
        self.deduper = deduper
        self.channel_store = channel_store

    def decide(self, message: NormalizedMessage) -> RouteDecision:
        if self.deduper.seen_any(_message_dedupe_ids(message)):
            return RouteDecision(message.message_id, message.conversation_id, "duplicate", "message already processed")

        trusted_channel = _trusted_channel_source(message)
        channel = None
        if self.channel_store is not None and trusted_channel:
            existing = self.channel_store.get_channel(message.conversation_id)
            admission = channel_admission_for_message(
                message,
                self.config,
                existing_channel=existing or False,
            )
            if not admission.allowed:
                return RouteDecision(
                    message.message_id,
                    message.conversation_id,
                    "ignore",
                    f"channel_admission_blocked:{admission.reason}:{admission.identity}",
                )
            channel = self.channel_store.ensure_channel(message)
        channel_reason = "channel auto registered" if channel is not None else "auto accepted conversation"
        if message.conversation_type == "private":
            return RouteDecision(message.message_id, message.conversation_id, "process", channel_reason)

        return RouteDecision(
            message.message_id,
            message.conversation_id,
            "process",
            channel_reason,
            requires_topic_decision=True,
        )

    def mark_done(self, message_id: str) -> None:
        self.deduper.mark(message_id)

    def mark_message_done(self, message: NormalizedMessage) -> None:
        self.deduper.mark_many(_message_dedupe_ids(message))


def _trusted_channel_source(message: NormalizedMessage) -> bool:
    source = str(message.metadata.get("source", "")).strip()
    if source in TRUSTED_CHANNEL_SOURCES:
        return True
    if message.metadata.get("trusted_channel_source") is True:
        return True
    if source in UNTRUSTED_CHANNEL_SOURCES:
        return False
    return False


def _message_dedupe_ids(message: NormalizedMessage) -> list[str]:
    ids = [message.message_id]
    dedupe_key = str(message.metadata.get("dedupe_key") or "").strip()
    if dedupe_key:
        ids.append(f"dedupe:{dedupe_key}")
    return ids

from __future__ import annotations

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.conversation.channel_store import ConversationChannelStore
from app.personal_wechat_bot.domain.models import NormalizedMessage, RouteDecision
from app.personal_wechat_bot.router.deduper import Deduper


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
        if self.deduper.seen(message.message_id):
            return RouteDecision(message.message_id, message.conversation_id, "duplicate", "message already processed")

        channel = self.channel_store.ensure_channel(message) if self.channel_store is not None else None
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

from __future__ import annotations

from collections import defaultdict, deque

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.domain.models import NormalizedMessage, SpeakDecision
from app.personal_wechat_bot.llm.base import LLMClient


class AITopicClassifier:
    def __init__(self, llm: LLMClient, config: BotConfig):
        self.llm = llm
        self.config = config
        self._recent: dict[str, deque[NormalizedMessage]] = defaultdict(
            lambda: deque(maxlen=config.context_window_messages)
        )

    def decide(self, message: NormalizedMessage) -> SpeakDecision:
        if message.conversation_type == "private":
            return SpeakDecision(
                conversation_id=message.conversation_id,
                decision="speak",
                reason="private_chat_allowed",
                topic="private",
                confidence=1.0,
                style_context="自然朋友聊天",
            )
        recent = self._recent[message.conversation_id]
        recent.append(message)
        try:
            return self.llm.classify_topic(list(recent), self.config.topics, self.config.avoid_topics)
        except Exception as exc:
            return SpeakDecision(
                conversation_id=message.conversation_id,
                decision="silent",
                reason=f"topic_classifier_error:{type(exc).__name__}",
                confidence=0.0,
                style_context="fallback_silent",
            )

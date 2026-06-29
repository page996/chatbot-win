from __future__ import annotations

from typing import Protocol

from app.personal_wechat_bot.domain.models import NormalizedMessage, SpeakDecision


class LLMClient(Protocol):
    model: str

    def generate_reply(self, prompt: str) -> str: ...

    def classify_topic(
        self,
        recent_messages: list[NormalizedMessage],
        topics: list[str],
        avoid_topics: list[str],
    ) -> SpeakDecision: ...

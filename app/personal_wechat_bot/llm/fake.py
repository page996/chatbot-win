from __future__ import annotations

from app.personal_wechat_bot.domain.models import NormalizedMessage, SpeakDecision


class FakeLLMClient:
    def __init__(self, model: str = "fake-gpt-5.5"):
        self.model = model

    def generate_reply(self, prompt: str) -> str:
        return "收到，我会自然地接一句。\nPLAN: 先理解上下文，再给自然朋友式回复。\nMONITOR: fake_llm.completed\nSUMMARY: 收到，我会自然地接一句。"

    def classify_topic(
        self,
        recent_messages: list[NormalizedMessage],
        topics: list[str],
        avoid_topics: list[str],
    ) -> SpeakDecision:
        latest = recent_messages[-1]
        decision = latest.metadata.get("topic_decision")
        if decision == "speak":
            return SpeakDecision(
                conversation_id=latest.conversation_id,
                decision="speak",
                reason="fake_ai_context_classifier_matched",
                topic=latest.metadata.get("topic", topics[0] if topics else "临时topic"),
                confidence=0.91,
                style_context="自然朋友聊天",
            )
        if decision == "wait":
            return SpeakDecision(latest.conversation_id, "wait", "fake_ai_context_classifier_wait", confidence=0.5)
        return SpeakDecision(latest.conversation_id, "silent", "fake_ai_context_classifier_not_matched", confidence=0.2)

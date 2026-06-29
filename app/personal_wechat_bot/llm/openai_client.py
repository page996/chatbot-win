from __future__ import annotations

import json
import os
import re
import urllib.request

from app.personal_wechat_bot.config.schema import ProviderConfig
from app.personal_wechat_bot.conversation.channel_store import ConversationChannelStore
from app.personal_wechat_bot.domain.models import NormalizedMessage, SpeakDecision
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool


DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"


def normalize_openai_base_url(base_url: str, provider: str = "relay") -> str:
    base = base_url.rstrip("/")
    if not base:
        return ""
    if provider == "deepseek" or "api.deepseek.com" in base:
        return base.removesuffix("/v1")
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


class RelayOpenAIClient:
    """Small OpenAI-compatible relay client skeleton.

    The minimum closed loop uses FakeLLMClient. This class is intentionally
    narrow and is not exercised unless explicitly configured later.
    """

    def __init__(
        self,
        config: ProviderConfig,
        key_pool: ApiKeyPool | None = None,
        channel_store: ConversationChannelStore | None = None,
    ):
        self.config = config
        self.model = config.model
        self.key_pool = key_pool or ApiKeyPool(config)
        self.channel_store = channel_store

    def generate_reply(self, prompt: str) -> str:
        data = self._chat_completion(
            [{"role": "user", "content": prompt}],
            conversation_id=_conversation_id_from_prompt(prompt),
        )
        return self._extract_content(data)

    def classify_topic(
        self,
        recent_messages: list[NormalizedMessage],
        topics: list[str],
        avoid_topics: list[str],
    ) -> SpeakDecision:
        if not recent_messages:
            raise RuntimeError("missing recent messages for topic classification")
        latest = recent_messages[-1]
        prompt = {
            "conversation_id": latest.conversation_id,
            "conversation_type": latest.conversation_type,
            "topics": topics,
            "avoid_topics": avoid_topics,
            "recent_messages": [
                {
                    "sender": item.sender_name,
                    "text": item.text,
                    "conversation_type": item.conversation_type,
                }
                for item in recent_messages[-10:]
            ],
            "output_schema": {
                "decision": "speak|silent|wait",
                "reason": "short explanation",
                "topic": "selected topic or null",
                "confidence": 0.0,
                "style_context": "short style hint",
            },
        }
        data = self._chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a WeChat group topic classifier. Allowed topics are semantic labels, not keywords. "
                        "Choose speak when the latest group message meaningfully relates to an allowed topic, asks a question, "
                        "or invites discussion the bot can naturally join. Choose silent for unrelated chatter, avoided topics, "
                        "spam, or very low confidence. Choose wait when the context is incomplete. Return JSON only."
                    ),
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            temperature=0,
            conversation_id=latest.conversation_id,
        )
        parsed = self._parse_json(self._extract_content(data))
        decision = str(parsed.get("decision", "silent"))
        if decision not in {"speak", "silent", "wait"}:
            decision = "silent"
        reason = str(parsed.get("reason", "relay_topic_classifier"))
        topic = parsed.get("topic")
        topic_value = str(topic) if topic not in {None, ""} else None
        confidence = self._coerce_float(parsed.get("confidence", 0.0))
        style_context = str(parsed.get("style_context", "自然朋友聊天"))
        return SpeakDecision(
            conversation_id=latest.conversation_id,
            decision=decision,  # type: ignore[arg-type]
            reason=reason,
            topic=topic_value,
            confidence=confidence,
            style_context=style_context,
        )

    def _chat_completion(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        conversation_id: str = "",
    ) -> dict[str, object]:
        if not self.config.base_url:
            raise RuntimeError("missing relay base_url")
        api_key = self._api_key_for_conversation(conversation_id)
        if not api_key:
            raise RuntimeError(f"missing API key env: {self.config.api_key_env}")
        payload: dict[str, object] = {"model": self.model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        req = urllib.request.Request(
            normalize_openai_base_url(self.config.base_url, self.config.provider) + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": DEFAULT_USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.config.max_wait_seconds) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _api_key_for_conversation(self, conversation_id: str) -> str | None:
        if conversation_id and self.channel_store is not None:
            key = self.channel_store.api_key_for_request(conversation_id)
            if key:
                return key
        return self.key_pool.default_key()

    def _extract_content(self, data: dict[str, object]) -> str:
        choices = data.get("choices", [])
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("missing choices in relay response")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise RuntimeError("invalid choice in relay response")
        message = choice.get("message", {})
        if not isinstance(message, dict):
            raise RuntimeError("invalid message in relay response")
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(item) for item in content)
        return str(content)

    def _parse_json(self, content: str) -> dict[str, object]:
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _coerce_float(self, value: object) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


def _conversation_id_from_prompt(prompt: str) -> str:
    match = re.search(r"conversation_id=([A-Za-z0-9_.-]+)", prompt)
    return match.group(1) if match else ""

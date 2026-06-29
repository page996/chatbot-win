from __future__ import annotations

from dataclasses import dataclass

from app.personal_wechat_bot.config.schema import ProviderConfig
from app.personal_wechat_bot.domain.errors import ConfigError
from app.personal_wechat_bot.llm.openai_client import RelayOpenAIClient


@dataclass(frozen=True)
class ProviderSelection:
    provider_id: str
    config: ProviderConfig


class ModelRouter:
    def __init__(self, providers: dict[str, ProviderConfig]):
        self.providers = dict(providers)

    def require_provider(self, provider_id: str) -> ProviderSelection:
        provider = self.providers.get(provider_id)
        if provider is None:
            raise ConfigError(f"missing provider: {provider_id}")
        return ProviderSelection(provider_id=provider_id, config=provider)

    def require_capability(self, capability: str) -> ProviderSelection:
        for provider_id, provider in self.providers.items():
            if capability in provider.capabilities:
                return ProviderSelection(provider_id=provider_id, config=provider)
        raise ConfigError(f"missing provider capability: {capability}")

    def chat_provider(self) -> ProviderSelection:
        if "chat" in self.providers:
            return self.require_provider("chat")
        return self.require_capability("chat")

    def build_chat_client(self) -> RelayOpenAIClient:
        return RelayOpenAIClient(self.chat_provider().config)

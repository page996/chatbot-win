from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Mode = Literal["dry_run", "confirm", "auto"]


@dataclass
class ProviderConfig:
    provider_id: str = "chat"
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    base_url: str = ""
    api_key_env: str = "DEEPSEEK_API_KEY"
    api_key_env_pool: list[str] = field(default_factory=list)
    api_key_file: str = ""
    stream: bool = False
    max_wait_seconds: int | None = None
    capabilities: list[str] = field(
        default_factory=lambda: ["chat", "planning", "summarization", "relevance_filter"]
    )
    max_concurrency: int = 2
    cooldown_seconds: int = 0


@dataclass
class LLMConfig(ProviderConfig):
    pass


def default_providers() -> dict[str, ProviderConfig]:
    return {"chat": ProviderConfig()}


@dataclass
class BotConfig:
    mode: Mode = "dry_run"
    data_dir: str = "data"
    send_enabled: bool = False
    send_driver: str = "not_implemented"
    send_confirm_required: bool = True
    send_max_chars: int = 800
    send_min_interval_seconds: int = 5
    accepted_contacts: set[str] = field(default_factory=set)
    accepted_groups: set[str] = field(default_factory=set)
    group_cooldown_seconds: int = 60
    context_window_messages: int = 20
    topics: list[str] = field(default_factory=lambda: ["日常闲聊", "学习", "AI"])
    avoid_topics: list[str] = field(default_factory=list)
    llm: LLMConfig = field(default_factory=LLMConfig)
    providers: dict[str, ProviderConfig] = field(default_factory=default_providers)
    key_assignment_policy: str = "conversation_sticky"
    save_full_chat: bool = True
    save_raw_and_summary: bool = True
    file_read_roots: list[str] = field(default_factory=lambda: ["inbox"])
    file_allowed_extensions: list[str] = field(
        default_factory=lambda: [
            ".txt",
            ".md",
            ".docx",
            ".pdf",
            ".xlsx",
            ".xlsm",
            ".csv",
            ".png",
            ".jpg",
            ".jpeg",
            ".bmp",
            ".webp",
            ".mp3",
            ".wav",
            ".m4a",
            ".aac",
            ".ogg",
            ".wma",
            ".amr",
            ".silk",
        ]
    )
    file_max_bytes: int = 20 * 1024 * 1024
    search_blocklist: list[str] = field(
        default_factory=lambda: [
            "baidu.com",
            "baike.baidu.com",
            "zhihu.com",
            "csdn.net",
            "sohu.com",
            "163.com",
            "qq.com",
        ]
    )

    @property
    def contacts_whitelist(self) -> set[str]:
        return self.accepted_contacts

    @contacts_whitelist.setter
    def contacts_whitelist(self, value: set[str]) -> None:
        self.accepted_contacts = set(value)

    @property
    def groups_whitelist(self) -> set[str]:
        return self.accepted_groups

    @groups_whitelist.setter
    def groups_whitelist(self, value: set[str]) -> None:
        self.accepted_groups = set(value)

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Mode = Literal["dry_run", "confirm", "auto"]
DEFAULT_LLM_MAX_CONCURRENCY = 6


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
    max_concurrency: int = DEFAULT_LLM_MAX_CONCURRENCY
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
    send_driver: str = "bridge_outbox"
    send_backend: str = "wechat_native_http"
    weflow_base_url: str = "http://127.0.0.1:5031"
    weflow_token_env: str = "WEFLOW_API_TOKEN"
    weflow_send_text_path: str = "/send/text"
    weflow_send_file_path: str = "/send/file"
    weflow_send_timeout_seconds: float = 35.0
    wechat_native_base_url: str = "http://127.0.0.1:30001"
    wechat_native_send_text_path: str = "/SendTextMsg"
    wechat_native_send_image_path: str = "/SendImgMsg"
    wechat_native_send_file_path: str = "/send_file_msg"
    wechat_native_status_path: str = "/QueryDB/status"
    wechat_native_timeout_seconds: float = 15.0
    wechat_native_verify_timeout_seconds: float = 10.0
    wechat_native_file_verify_timeout_seconds: float = 45.0
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
    wechat_voice_roots: list[str] = field(default_factory=list)
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
            ".gif",
            ".tif",
            ".tiff",
            ".mp3",
            ".wav",
            ".m4a",
            ".aac",
            ".ogg",
            ".wma",
            ".amr",
            ".silk",
            ".ppt",
            ".pptx",
            ".zip",
            ".rar",
            ".7z",
            ".tar",
            ".gz",
            ".tgz",
            ".exe",
            ".msi",
            ".apk",
            ".app",
            ".dmg",
            ".bat",
            ".cmd",
            ".ps1",
            ".scr",
            ".mp4",
            ".mov",
            ".avi",
            ".mkv",
            ".wmv",
            ".flv",
            ".webm",
            ".m4v",
        ]
    )
    file_max_bytes: int = 20 * 1024 * 1024
    # Outgoing (agent-produced) files are trusted artifacts, so they use relaxed
    # limits: an empty allowed-extensions list disables the extension gate, and a
    # larger size cap avoids blocking legitimate tool outputs (archives, media).
    # Integrity of the agent's own output takes priority over the inbound guard.
    outgoing_file_allowed_extensions: list[str] = field(default_factory=list)
    outgoing_file_max_bytes: int = 200 * 1024 * 1024
    # auto/cpu stay on the light CPU path; gpu is explicit, strict, and queued.
    ocr_mode: str = "auto"  # auto | cpu | gpu
    asr_mode: str = "auto"  # auto | cpu | gpu
    search_blocklist: list[str] = field(
        default_factory=lambda: [
            "doubleclick.net",
            "googlesyndication.com",
            "googleadservices.com",
            "taboola.com",
            "outbrain.com",
            "casino",
            "gambling",
            "porn",
            "xxx",
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

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.config.schema import BotConfig, LLMConfig, ProviderConfig
from app.personal_wechat_bot.domain.errors import ConfigError


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def create_default_config(data_dir: str | Path = "data") -> BotConfig:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    config = BotConfig(data_dir=str(root))
    _write_json(root / "config.json", _config_to_json(config))
    _write_json(root / "accepted_contacts.json", [])
    _write_json(root / "accepted_groups.json", [])
    _write_json(root / "contacts_whitelist.json", [])
    _write_json(root / "groups_whitelist.json", [])
    _write_json(root / "topic_rules.json", {"topics": config.topics, "avoid_topics": []})
    _write_json(root / "search_blocklist.json", config.search_blocklist)
    (root / "inbox").mkdir(exist_ok=True)
    (root / "tool_outputs").mkdir(exist_ok=True)
    return config


def load_config(data_dir: str | Path = "data") -> BotConfig:
    root = Path(data_dir)
    raw = _read_json(root / "config.json", None)
    if raw is None:
        raise ConfigError(f"missing config: {root / 'config.json'}; run init first")

    contacts = _read_accept_list(root, "accepted_contacts.json", "contacts_whitelist.json")
    groups = _read_accept_list(root, "accepted_groups.json", "groups_whitelist.json")
    topic_raw = _read_json(root / "topic_rules.json", {})
    blocklist = _read_json(root / "search_blocklist.json", raw.get("search_blocklist", []))

    llm = _llm_from_json(raw.get("llm", {}))
    providers = _providers_from_json(raw.get("providers"), llm)
    mode = raw.get("mode", "dry_run")
    if mode not in {"dry_run", "confirm", "auto"}:
        raise ConfigError(f"invalid mode: {mode}")
    return BotConfig(
        mode=mode,
        data_dir=str(root),
        send_enabled=bool(raw.get("send_enabled", False)),
        send_driver=str(raw.get("send_driver", "not_implemented")),
        send_confirm_required=bool(raw.get("send_confirm_required", True)),
        send_max_chars=int(raw.get("send_max_chars", 800)),
        send_min_interval_seconds=int(raw.get("send_min_interval_seconds", 5)),
        accepted_contacts=contacts,
        accepted_groups=groups,
        group_cooldown_seconds=int(raw.get("group_cooldown_seconds", 60)),
        context_window_messages=int(raw.get("context_window_messages", 20)),
        topics=list(topic_raw.get("topics", raw.get("topics", ["日常闲聊", "学习", "AI"]))),
        avoid_topics=list(topic_raw.get("avoid_topics", raw.get("avoid_topics", []))),
        llm=llm,
        providers=providers,
        key_assignment_policy=str(raw.get("key_assignment_policy", "conversation_sticky")),
        save_full_chat=bool(raw.get("save_full_chat", True)),
        save_raw_and_summary=bool(raw.get("save_raw_and_summary", True)),
        file_read_roots=list(raw.get("file_read_roots", ["inbox"])),
        wechat_voice_roots=list(raw.get("wechat_voice_roots", [])),
        file_allowed_extensions=_file_allowed_extensions_from_json(raw),
        file_max_bytes=int(raw.get("file_max_bytes", 20 * 1024 * 1024)),
        search_blocklist=list(blocklist),
    )


def save_config(config: BotConfig) -> None:
    root = Path(config.data_dir)
    _write_json(root / "config.json", _config_to_json(config))
    _write_json(root / "accepted_contacts.json", sorted(config.accepted_contacts))
    _write_json(root / "accepted_groups.json", sorted(config.accepted_groups))
    _write_json(root / "contacts_whitelist.json", sorted(config.accepted_contacts))
    _write_json(root / "groups_whitelist.json", sorted(config.accepted_groups))
    _write_json(root / "topic_rules.json", {"topics": config.topics, "avoid_topics": config.avoid_topics})
    _write_json(root / "search_blocklist.json", config.search_blocklist)


def accept_contact(data_dir: str | Path, wechat_id: str) -> None:
    config = load_config(data_dir)
    config.accepted_contacts.add(wechat_id)
    save_config(config)


def accept_group(data_dir: str | Path, group_name: str) -> None:
    config = load_config(data_dir)
    config.accepted_groups.add(group_name)
    save_config(config)


def add_contact(data_dir: str | Path, wechat_id: str) -> None:
    accept_contact(data_dir, wechat_id)


def add_group(data_dir: str | Path, group_name: str) -> None:
    accept_group(data_dir, group_name)


def rename_group(data_dir: str | Path, old_name: str, new_name: str) -> None:
    config = load_config(data_dir)
    if old_name in config.accepted_groups:
        config.accepted_groups.remove(old_name)
    config.accepted_groups.add(new_name)
    save_config(config)


def set_chat_provider(
    data_dir: str | Path,
    base_url: str,
    model: str = "gpt-5.5",
    api_key_env: str = "OPENAI_API_KEY",
    max_wait_seconds: int | None = None,
) -> None:
    config = load_config(data_dir)
    provider = LLMConfig(
        provider_id="chat",
        provider="relay",
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        api_key_env_pool=[],
        api_key_file="",
        stream=False,
        max_wait_seconds=max_wait_seconds,
        capabilities=["chat", "planning", "summarization", "relevance_filter"],
        max_concurrency=2,
        cooldown_seconds=0,
    )
    config.llm = provider
    config.providers["chat"] = provider
    save_config(config)


def set_deepseek_provider(
    data_dir: str | Path,
    base_url: str = "https://api.deepseek.com",
    model: str = "deepseek-v4-flash",
    api_key_env: str = "DEEPSEEK_API_KEY",
    max_wait_seconds: int | None = 60,
) -> None:
    config = load_config(data_dir)
    provider = LLMConfig(
        provider_id="chat",
        provider="deepseek",
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        api_key_env_pool=[],
        api_key_file="",
        stream=False,
        max_wait_seconds=max_wait_seconds,
        capabilities=["chat", "planning", "summarization", "relevance_filter"],
        max_concurrency=2,
        cooldown_seconds=0,
    )
    config.llm = provider
    config.providers["chat"] = provider
    save_config(config)


def _llm_from_json(raw: dict[str, Any]) -> LLMConfig:
    return LLMConfig(
        provider_id=raw.get("provider_id", "chat"),
        provider=raw.get("provider", "deepseek"),
        model=raw.get("model", "deepseek-v4-flash"),
        base_url=raw.get("base_url", ""),
        api_key_env=raw.get("api_key_env", "DEEPSEEK_API_KEY"),
        api_key_env_pool=list(raw.get("api_key_env_pool", [])),
        api_key_file=raw.get("api_key_file", ""),
        stream=bool(raw.get("stream", False)),
        max_wait_seconds=raw.get("max_wait_seconds"),
        capabilities=list(raw.get("capabilities", ["chat", "planning", "summarization", "relevance_filter"])),
        max_concurrency=int(raw.get("max_concurrency", 2)),
        cooldown_seconds=int(raw.get("cooldown_seconds", 0)),
    )


def _providers_from_json(raw: Any, fallback_llm: LLMConfig) -> dict[str, ProviderConfig]:
    if not isinstance(raw, dict):
        return {"chat": _provider_from_llm(fallback_llm)}
    providers = {
        name: _provider_from_json(name, value)
        for name, value in raw.items()
        if isinstance(name, str) and isinstance(value, dict)
    }
    if "chat" not in providers:
        providers["chat"] = _provider_from_llm(fallback_llm)
    return providers


def _file_allowed_extensions_from_json(raw: dict[str, Any]) -> list[str]:
    default_extensions = BotConfig().file_allowed_extensions
    configured = raw.get("file_allowed_extensions", default_extensions)
    values = configured if isinstance(configured, list) else default_extensions
    normalized: list[str] = []
    seen: set[str] = set()
    for item in [*values, *default_extensions]:
        suffix = str(item).strip().lower()
        if not suffix:
            continue
        if not suffix.startswith("."):
            suffix = "." + suffix
        if suffix in seen:
            continue
        seen.add(suffix)
        normalized.append(suffix)
    return normalized


def _provider_from_json(name: str, raw: dict[str, Any]) -> ProviderConfig:
    return ProviderConfig(
        provider_id=raw.get("provider_id", name),
        provider=raw.get("provider", "deepseek"),
        model=raw.get("model", "deepseek-v4-flash"),
        base_url=raw.get("base_url", ""),
        api_key_env=raw.get("api_key_env", "DEEPSEEK_API_KEY"),
        api_key_env_pool=list(raw.get("api_key_env_pool", [])),
        api_key_file=raw.get("api_key_file", ""),
        stream=bool(raw.get("stream", False)),
        max_wait_seconds=raw.get("max_wait_seconds"),
        capabilities=list(raw.get("capabilities", ["chat", "planning", "summarization", "relevance_filter"])),
        max_concurrency=int(raw.get("max_concurrency", 2)),
        cooldown_seconds=int(raw.get("cooldown_seconds", 0)),
    )


def _provider_from_llm(llm: LLMConfig) -> ProviderConfig:
    return ProviderConfig(
        provider_id=llm.provider_id,
        provider=llm.provider,
        model=llm.model,
        base_url=llm.base_url,
        api_key_env=llm.api_key_env,
        api_key_env_pool=list(llm.api_key_env_pool),
        api_key_file=llm.api_key_file,
        stream=llm.stream,
        max_wait_seconds=llm.max_wait_seconds,
        capabilities=list(llm.capabilities),
        max_concurrency=llm.max_concurrency,
        cooldown_seconds=llm.cooldown_seconds,
    )


def _config_to_json(config: BotConfig) -> dict[str, Any]:
    payload = asdict(config)
    payload["accepted_contacts"] = sorted(config.accepted_contacts)
    payload["accepted_groups"] = sorted(config.accepted_groups)
    payload["contacts_whitelist"] = sorted(config.accepted_contacts)
    payload["groups_whitelist"] = sorted(config.accepted_groups)
    return payload


def _read_accept_list(root: Path, preferred_name: str, legacy_name: str) -> set[str]:
    preferred = _read_json(root / preferred_name, None)
    if isinstance(preferred, list):
        return {str(item) for item in preferred if str(item).strip()}
    legacy = _read_json(root / legacy_name, [])
    if isinstance(legacy, list):
        return {str(item) for item in legacy if str(item).strip()}
    return set()

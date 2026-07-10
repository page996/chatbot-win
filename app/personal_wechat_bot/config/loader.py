from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.config.schema import (
    DEFAULT_LLM_MAX_CONCURRENCY,
    BotConfig,
    LLMConfig,
    ProviderConfig,
)
from app.personal_wechat_bot.domain.errors import ConfigError


# Deprecated/removed send drivers mapped to their replacement. windows_guarded
# was removed in favour of the non-foreground bridge_outbox driver; a stale
# config naming it would otherwise pass the send guard but resolve to no driver
# (silent send_driver_missing). Normalizing at load time is self-healing.
_DEPRECATED_SEND_DRIVERS = {"windows_guarded": "bridge_outbox"}
_SIDEBAR_CONFIG_DIR = ".chatbot_sidebar_config"
_CONFIG_FILE_NAMES = (
    "config.json",
    "accepted_contacts.json",
    "accepted_groups.json",
    "contacts_whitelist.json",
    "groups_whitelist.json",
    "topic_rules.json",
    "search_blocklist.json",
)


def _normalize_send_driver(name: str) -> str:
    cleaned = str(name or "").strip()
    return _DEPRECATED_SEND_DRIVERS.get(cleaned, cleaned)


def _normalize_send_backend(name: str) -> str:
    return str(name or "").strip().lower()


def _bool_from_json(raw: dict[str, Any], name: str, default: bool) -> bool:
    value = raw.get(name, default)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off", ""}:
        return False
    raise ConfigError(f"invalid boolean for {name}: {value!r}")


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


def persistent_config_dir(data_dir: str | Path = "data") -> Path:
    """Stable sidebar/config storage outside the disposable history tree."""

    root = Path(data_dir).resolve()
    segment = _safe_config_segment(str(root))
    return root.parent / _SIDEBAR_CONFIG_DIR / segment


def ensure_config(data_dir: str | Path = "data") -> BotConfig:
    """Load config, restoring from sidecar or creating defaults for the sidebar."""

    try:
        return load_config(data_dir)
    except ConfigError:
        return create_default_config(data_dir)


def create_default_config(data_dir: str | Path = "data") -> BotConfig:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    config = BotConfig(data_dir=str(root))
    _write_config_json(root, "config.json", _config_to_json(config))
    _write_config_json(root, "accepted_contacts.json", [])
    _write_config_json(root, "accepted_groups.json", [])
    _write_config_json(root, "contacts_whitelist.json", [])
    _write_config_json(root, "groups_whitelist.json", [])
    _write_config_json(root, "topic_rules.json", {"topics": config.topics, "avoid_topics": []})
    _write_config_json(root, "search_blocklist.json", config.search_blocklist)
    (root / "inbox").mkdir(exist_ok=True)
    (root / "tool_outputs").mkdir(exist_ok=True)
    return config


def load_config(data_dir: str | Path = "data") -> BotConfig:
    root = Path(data_dir)
    raw = _read_config_json(root, "config.json", None)
    if raw is None:
        raise ConfigError(f"missing config: {root / 'config.json'}; run init first")

    contacts = _read_accept_list(root, "accepted_contacts.json", "contacts_whitelist.json")
    groups = _read_accept_list(root, "accepted_groups.json", "groups_whitelist.json")
    topic_raw = _read_config_json(root, "topic_rules.json", {})
    blocklist = _read_config_json(root, "search_blocklist.json", raw.get("search_blocklist", []))

    llm = _llm_from_json(raw.get("llm", {}))
    providers = _providers_from_json(raw.get("providers"), llm)
    mode = raw.get("mode", "dry_run")
    if mode not in {"dry_run", "confirm", "auto"}:
        raise ConfigError(f"invalid mode: {mode}")
    return BotConfig(
        mode=mode,
        data_dir=str(root),
        send_enabled=_bool_from_json(raw, "send_enabled", False),
        send_driver=_normalize_send_driver(raw.get("send_driver", "not_implemented")),
        send_backend=_normalize_send_backend(raw.get("send_backend", "dry_run")),
        weflow_base_url=str(raw.get("weflow_base_url", "http://127.0.0.1:5031") or "http://127.0.0.1:5031"),
        weflow_token_env=str(raw.get("weflow_token_env", "WEFLOW_API_TOKEN") or "WEFLOW_API_TOKEN"),
        weflow_send_text_path=str(raw.get("weflow_send_text_path", "/send/text") or "/send/text"),
        weflow_send_file_path=str(raw.get("weflow_send_file_path", "/send/file") or "/send/file"),
        weflow_send_timeout_seconds=float(raw.get("weflow_send_timeout_seconds", 35.0)),
        wechat_native_base_url=str(raw.get("wechat_native_base_url", "http://127.0.0.1:30001") or "http://127.0.0.1:30001"),
        wechat_native_send_text_path=str(raw.get("wechat_native_send_text_path", "/SendTextMsg") or "/SendTextMsg"),
        wechat_native_send_image_path=str(raw.get("wechat_native_send_image_path", "/SendImgMsg") or "/SendImgMsg"),
        wechat_native_send_file_path=str(raw.get("wechat_native_send_file_path", "/send_file_msg") or "/send_file_msg"),
        wechat_native_status_path=str(raw.get("wechat_native_status_path", "/QueryDB/status") or "/QueryDB/status"),
        wechat_native_timeout_seconds=float(raw.get("wechat_native_timeout_seconds", 15.0)),
        wechat_native_verify_timeout_seconds=float(raw.get("wechat_native_verify_timeout_seconds", 10.0)),
        wechat_native_file_verify_timeout_seconds=float(raw.get("wechat_native_file_verify_timeout_seconds", 45.0)),
        send_confirm_required=_bool_from_json(raw, "send_confirm_required", True),
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
        save_full_chat=_bool_from_json(raw, "save_full_chat", True),
        save_raw_and_summary=_bool_from_json(raw, "save_raw_and_summary", True),
        file_read_roots=list(raw.get("file_read_roots", ["inbox"])),
        wechat_voice_roots=list(raw.get("wechat_voice_roots", [])),
        file_allowed_extensions=_file_allowed_extensions_from_json(raw),
        file_max_bytes=int(raw.get("file_max_bytes", 20 * 1024 * 1024)),
        outgoing_file_allowed_extensions=_outgoing_extensions_from_json(raw),
        outgoing_file_max_bytes=int(raw.get("outgoing_file_max_bytes", 200 * 1024 * 1024)),
        ocr_mode=_ocr_mode_from_json(raw),
        asr_mode=_asr_mode_from_json(raw),
        search_blocklist=list(blocklist),
    )


def save_config(config: BotConfig) -> None:
    root = Path(config.data_dir)
    _write_config_json(root, "config.json", _config_to_json(config))
    _write_config_json(root, "accepted_contacts.json", sorted(config.accepted_contacts))
    _write_config_json(root, "accepted_groups.json", sorted(config.accepted_groups))
    _write_config_json(root, "contacts_whitelist.json", sorted(config.accepted_contacts))
    _write_config_json(root, "groups_whitelist.json", sorted(config.accepted_groups))
    _write_config_json(root, "topic_rules.json", {"topics": config.topics, "avoid_topics": config.avoid_topics})
    _write_config_json(root, "search_blocklist.json", config.search_blocklist)


def migrate_file_allowed_extensions(data_dir: str | Path = "data") -> dict[str, Any]:
    """Persist newly supported attachment suffixes into an existing config."""

    root = Path(data_dir)
    raw = _read_config_json(root, "config.json", None)
    if not isinstance(raw, dict):
        raise ConfigError(f"missing config: {root / 'config.json'}; run init first")
    default_extensions = BotConfig().file_allowed_extensions
    configured = raw.get("file_allowed_extensions", [])
    values = configured if isinstance(configured, list) else []
    normalized = {_normalize_extension(item) for item in values}
    normalized.discard("")
    missing = [item for item in default_extensions if item not in normalized]
    config = load_config(root)
    if missing:
        save_config(config)
    return {
        "status": "updated" if missing else "ok",
        "added_extensions": missing,
        "file_allowed_extensions": config.file_allowed_extensions,
    }


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


def set_model_provider(
    data_dir: str | Path,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    max_wait_seconds: int | None = None,
    max_concurrency: int | None = None,
) -> ProviderConfig:
    """Update the chat provider's model/endpoint/format in place.

    Only the fields the model-config panel edits are touched; the key pool link
    (api_key_file / api_key_env_pool) is preserved so existing keys keep working.
    The async-summary path shares the chat provider, so it follows the same
    model automatically. Returns the updated chat provider config.
    """
    config = load_config(data_dir)
    current = config.providers.get("chat", config.llm)
    updated = LLMConfig(
        provider_id="chat",
        provider=str(provider) if provider is not None else current.provider,
        model=str(model) if model is not None else current.model,
        base_url=str(base_url) if base_url is not None else current.base_url,
        api_key_env=str(api_key_env) if api_key_env is not None else current.api_key_env,
        api_key_env_pool=list(current.api_key_env_pool),
        api_key_file=current.api_key_file,
        stream=current.stream,
        max_wait_seconds=max_wait_seconds if max_wait_seconds is not None else current.max_wait_seconds,
        capabilities=list(current.capabilities),
        max_concurrency=_bounded_positive_int(max_concurrency, current.max_concurrency),
        cooldown_seconds=current.cooldown_seconds,
    )
    config.llm = updated
    config.providers["chat"] = updated
    save_config(config)
    return updated


def set_chat_provider(
    data_dir: str | Path,
    base_url: str,
    model: str = "gpt-5.5",
    api_key_env: str = "OPENAI_API_KEY",
    max_wait_seconds: int | None = None,
    max_concurrency: int = DEFAULT_LLM_MAX_CONCURRENCY,
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
        max_concurrency=_bounded_positive_int(max_concurrency, DEFAULT_LLM_MAX_CONCURRENCY),
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
    max_concurrency: int = DEFAULT_LLM_MAX_CONCURRENCY,
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
        max_concurrency=_bounded_positive_int(max_concurrency, DEFAULT_LLM_MAX_CONCURRENCY),
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
        stream=_bool_from_json(raw, "stream", False),
        max_wait_seconds=raw.get("max_wait_seconds"),
        capabilities=list(raw.get("capabilities", ["chat", "planning", "summarization", "relevance_filter"])),
        max_concurrency=_bounded_positive_int(raw.get("max_concurrency"), DEFAULT_LLM_MAX_CONCURRENCY),
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
        suffix = _normalize_extension(item)
        if not suffix:
            continue
        if suffix in seen:
            continue
        seen.add(suffix)
        normalized.append(suffix)
    return normalized


def _outgoing_extensions_from_json(raw: dict[str, Any]) -> list[str]:
    """Outgoing (agent-produced) file extension allow-list.

    Unlike inbound files, this list is NOT merged with the built-in defaults: an
    empty list means "allow any extension" (the extension gate is disabled), so
    agent-generated artifacts are never blocked by type. Only an explicit,
    non-empty configured list restricts outgoing types.
    """

    configured = raw.get("outgoing_file_allowed_extensions", [])
    values = configured if isinstance(configured, list) else []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        suffix = _normalize_extension(item)
        if not suffix or suffix in seen:
            continue
        seen.add(suffix)
        normalized.append(suffix)
    return normalized


def _ocr_mode_from_json(raw: dict[str, Any]) -> str:
    mode = str(raw.get("ocr_mode", "auto") or "auto").strip().lower()
    if mode in {"gpu", "cuda", "gpu-only", "gpu_only"}:
        return "gpu"
    if mode in {"cpu", "rapidocr", "cpu-only", "cpu_only"}:
        return "cpu"
    return "auto"


def _asr_mode_from_json(raw: dict[str, Any]) -> str:
    mode = str(raw.get("asr_mode", "auto") or "auto").strip().lower()
    if mode in {"gpu", "cuda", "gpu-only", "gpu_only"}:
        return "gpu"
    if mode in {"cpu", "cpu-only", "cpu_only"}:
        return "cpu"
    return "auto"


def _normalize_extension(value: Any) -> str:
    suffix = str(value).strip().lower()
    if not suffix:
        return ""
    if not suffix.startswith("."):
        suffix = "." + suffix
    return suffix


def _bounded_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(1, min(64, parsed))


def _provider_from_json(name: str, raw: dict[str, Any]) -> ProviderConfig:
    return ProviderConfig(
        provider_id=raw.get("provider_id", name),
        provider=raw.get("provider", "deepseek"),
        model=raw.get("model", "deepseek-v4-flash"),
        base_url=raw.get("base_url", ""),
        api_key_env=raw.get("api_key_env", "DEEPSEEK_API_KEY"),
        api_key_env_pool=list(raw.get("api_key_env_pool", [])),
        api_key_file=raw.get("api_key_file", ""),
        stream=_bool_from_json(raw, "stream", False),
        max_wait_seconds=raw.get("max_wait_seconds"),
        capabilities=list(raw.get("capabilities", ["chat", "planning", "summarization", "relevance_filter"])),
        max_concurrency=_bounded_positive_int(raw.get("max_concurrency"), DEFAULT_LLM_MAX_CONCURRENCY),
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
        _mirror_config_file(root, preferred_name, preferred, from_primary=True)
        return {str(item) for item in preferred if str(item).strip()}
    legacy = _read_json(root / legacy_name, None)
    if isinstance(legacy, list):
        _write_config_json(root, preferred_name, legacy)
        _mirror_config_file(root, legacy_name, legacy, from_primary=True)
        return {str(item) for item in legacy if str(item).strip()}
    preferred = _read_json(persistent_config_dir(root) / preferred_name, None)
    if isinstance(preferred, list):
        _write_json(root / preferred_name, preferred)
        return {str(item) for item in preferred if str(item).strip()}
    legacy = _read_json(persistent_config_dir(root) / legacy_name, [])
    if isinstance(legacy, list):
        _write_config_json(root, preferred_name, legacy)
        return {str(item) for item in legacy if str(item).strip()}
    return set()


def _read_config_json(root: Path, name: str, default: Any) -> Any:
    primary = root / name
    value = _read_json(primary, None)
    if value is not None:
        _mirror_config_file(root, name, value, from_primary=True)
        return value
    sidecar = persistent_config_dir(root) / name
    value = _read_json(sidecar, None)
    if value is None:
        return default
    _write_json(primary, value)
    return value


def _write_config_json(root: Path, name: str, payload: Any) -> None:
    _write_json(root / name, payload)
    _write_json(persistent_config_dir(root) / name, payload)


def _mirror_config_file(root: Path, name: str, payload: Any, *, from_primary: bool) -> None:
    if name not in _CONFIG_FILE_NAMES:
        return
    target = persistent_config_dir(root) / name if from_primary else root / name
    if target.exists():
        return
    try:
        _write_json(target, payload)
    except OSError:
        return


def _safe_config_segment(value: str) -> str:
    import hashlib
    import re

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(value).name).strip("._")
    return f"{cleaned or 'data'}_{digest}"

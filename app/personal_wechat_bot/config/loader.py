from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterator

from app.personal_wechat_bot.config.schema import (
    DEFAULT_LLM_MAX_CONCURRENCY,
    BotConfig,
    ProviderConfig,
    default_providers,
)
from app.personal_wechat_bot.domain.errors import ConfigError
from app.personal_wechat_bot.runtime.process_lock import blocking_process_lock


_SIDEBAR_CONFIG_DIR = ".chatbot_sidebar_config"
_CONFIG_FILE_NAMES = (
    "config.json",
    "accepted_contacts.json",
    "accepted_groups.json",
    "topic_rules.json",
    "search_blocklist.json",
)


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
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def persistent_config_dir(data_dir: str | Path = "data") -> Path:
    """Stable sidebar/config storage outside the disposable history tree."""

    root = Path(data_dir).resolve()
    segment = _safe_config_segment(str(root))
    return root.parent / _SIDEBAR_CONFIG_DIR / segment


@contextmanager
def config_update_lock(data_dir: str | Path = "data") -> Iterator[None]:
    root = Path(data_dir)
    with blocking_process_lock(
        persistent_config_dir(root) / ".config-update.lock",
        label="bot_config_update",
        stale_after_seconds=60.0,
        wait_timeout_seconds=30.0,
    ):
        yield


def update_config(data_dir: str | Path, updater: Callable[[BotConfig], Any]) -> BotConfig:
    """Atomically apply a read-modify-write config update across UI threads."""

    with config_update_lock(data_dir):
        config = load_config(data_dir)
        updater(config)
        _save_config_unlocked(config)
        return config


def ensure_config(data_dir: str | Path = "data") -> BotConfig:
    """Load config, restoring from sidecar or creating defaults for the sidebar."""

    try:
        return load_config(data_dir)
    except ConfigError:
        return create_default_config(data_dir)


def create_default_config(data_dir: str | Path = "data") -> BotConfig:
    root = Path(data_dir)
    with config_update_lock(root):
        root.mkdir(parents=True, exist_ok=True)
        config = BotConfig(data_dir=str(root))
        _save_config_unlocked(config)
        (root / "inbox").mkdir(exist_ok=True)
        (root / "tool_outputs").mkdir(exist_ok=True)
        return config


def load_config(data_dir: str | Path = "data") -> BotConfig:
    root = Path(data_dir)
    raw = _read_config_json(root, "config.json", None)
    if raw is None:
        raise ConfigError(f"missing config: {root / 'config.json'}; run init first")

    contacts = _read_accept_list(root, "accepted_contacts.json")
    groups = _read_accept_list(root, "accepted_groups.json")
    topic_raw = _read_config_json(root, "topic_rules.json", {})
    blocklist = _read_config_json(root, "search_blocklist.json", raw.get("search_blocklist", []))

    providers = _providers_from_json(raw.get("providers"))
    mode = raw.get("mode", "dry_run")
    if mode not in {"dry_run", "confirm", "auto"}:
        raise ConfigError(f"invalid mode: {mode}")
    return BotConfig(
        mode=mode,
        data_dir=str(root),
        send_enabled=_bool_from_json(raw, "send_enabled", False),
        send_driver=str(raw.get("send_driver", "not_implemented") or "").strip(),
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
    with config_update_lock(config.data_dir):
        _save_config_unlocked(config)


def _save_config_unlocked(config: BotConfig) -> None:
    root = Path(config.data_dir)
    _write_config_json(root, "config.json", _config_to_json(config))
    _write_config_json(root, "accepted_contacts.json", sorted(config.accepted_contacts))
    _write_config_json(root, "accepted_groups.json", sorted(config.accepted_groups))
    _write_config_json(root, "topic_rules.json", {"topics": config.topics, "avoid_topics": config.avoid_topics})
    _write_config_json(root, "search_blocklist.json", config.search_blocklist)


def accept_contact(data_dir: str | Path, wechat_id: str) -> None:
    update_config(data_dir, lambda config: config.accepted_contacts.add(wechat_id))


def accept_group(data_dir: str | Path, group_name: str) -> None:
    update_config(data_dir, lambda config: config.accepted_groups.add(group_name))


def rename_group(data_dir: str | Path, old_name: str, new_name: str) -> None:
    def apply(config: BotConfig) -> None:
        config.accepted_groups.discard(old_name)
        config.accepted_groups.add(new_name)

    update_config(data_dir, apply)


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
    updated: ProviderConfig | None = None

    def apply(config: BotConfig) -> None:
        nonlocal updated
        current = config.providers.get("chat", ProviderConfig())
        updated = ProviderConfig(
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
        config.providers["chat"] = updated

    update_config(data_dir, apply)
    assert updated is not None
    return updated


def set_chat_provider(
    data_dir: str | Path,
    base_url: str,
    model: str = "gpt-5.5",
    api_key_env: str = "OPENAI_API_KEY",
    max_wait_seconds: int | None = None,
    max_concurrency: int = DEFAULT_LLM_MAX_CONCURRENCY,
) -> None:
    provider = ProviderConfig(
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
    def apply(config: BotConfig) -> None:
        config.providers["chat"] = provider

    update_config(data_dir, apply)


def set_deepseek_provider(
    data_dir: str | Path,
    base_url: str = "https://api.deepseek.com",
    model: str = "deepseek-v4-flash",
    api_key_env: str = "DEEPSEEK_API_KEY",
    max_wait_seconds: int | None = 60,
    max_concurrency: int = DEFAULT_LLM_MAX_CONCURRENCY,
) -> None:
    provider = ProviderConfig(
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
    def apply(config: BotConfig) -> None:
        config.providers["chat"] = provider

    update_config(data_dir, apply)


def _providers_from_json(raw: Any) -> dict[str, ProviderConfig]:
    if not isinstance(raw, dict):
        return default_providers()
    providers = {
        name: _provider_from_json(name, value)
        for name, value in raw.items()
        if isinstance(name, str) and isinstance(value, dict)
    }
    if "chat" not in providers:
        providers["chat"] = ProviderConfig()
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


def _config_to_json(config: BotConfig) -> dict[str, Any]:
    payload = asdict(config)
    payload.pop("accepted_contacts", None)
    payload.pop("accepted_groups", None)
    return payload


def _read_accept_list(root: Path, name: str) -> set[str]:
    values = _read_config_json(root, name, [])
    if not isinstance(values, list):
        return set()
    return {str(item) for item in values if str(item).strip()}


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

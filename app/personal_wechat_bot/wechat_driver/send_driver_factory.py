from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.reply_gate.send_executor import SendingDriver
from app.personal_wechat_bot.wechat_driver.bridge_send import (
    BRIDGE_OUTBOX_SEND_DRIVER,
    BridgeOutboxSendDriver,
)
from app.personal_wechat_bot.wechat_driver.send_backends import (
    wechat_native_http_status,
    weflow_http_status,
)


SendDriverBuilder = Callable[[BotConfig], SendingDriver]


@dataclass(frozen=True)
class SendDriverSpec:
    name: str
    builder: SendDriverBuilder
    real_send_implemented: bool
    description: str


_SEND_DRIVER_SPECS: dict[str, SendDriverSpec] = {
    BRIDGE_OUTBOX_SEND_DRIVER: SendDriverSpec(
        name=BRIDGE_OUTBOX_SEND_DRIVER,
        builder=lambda config: BridgeOutboxSendDriver(
            send_enabled=config.send_enabled,
            data_dir=config.data_dir,
            send_backend=str(getattr(config, "send_backend", "dry_run") or "dry_run"),
            weflow_base_url=str(getattr(config, "weflow_base_url", "http://127.0.0.1:5031") or "http://127.0.0.1:5031"),
            weflow_token_env=str(getattr(config, "weflow_token_env", "WEFLOW_API_TOKEN") or "WEFLOW_API_TOKEN"),
            weflow_send_text_path=str(getattr(config, "weflow_send_text_path", "/send/text") or "/send/text"),
            weflow_send_file_path=str(getattr(config, "weflow_send_file_path", "/send/file") or "/send/file"),
            weflow_send_timeout_seconds=float(getattr(config, "weflow_send_timeout_seconds", 35.0) or 35.0),
            wechat_native_base_url=str(
                getattr(config, "wechat_native_base_url", "http://127.0.0.1:30001") or "http://127.0.0.1:30001"
            ),
            wechat_native_send_text_path=str(getattr(config, "wechat_native_send_text_path", "/SendTextMsg") or "/SendTextMsg"),
            wechat_native_send_image_path=str(getattr(config, "wechat_native_send_image_path", "/SendImgMsg") or "/SendImgMsg"),
            wechat_native_send_file_path=str(
                getattr(config, "wechat_native_send_file_path", "/send_file_msg") or "/send_file_msg"
            ),
            wechat_native_status_path=str(getattr(config, "wechat_native_status_path", "/QueryDB/status") or "/QueryDB/status"),
            wechat_native_timeout_seconds=float(getattr(config, "wechat_native_timeout_seconds", 15.0) or 15.0),
            wechat_native_verify_timeout_seconds=float(
                getattr(config, "wechat_native_verify_timeout_seconds", 10.0) or 0.0
            ),
            wechat_native_file_verify_timeout_seconds=float(
                getattr(config, "wechat_native_file_verify_timeout_seconds", 45.0) or 0.0
            ),
        ),
        real_send_implemented=True,
        description="Non-foreground outbox producer; delivered by the selected send bridge backend",
    ),
}


def _normalized_send_backend(config: Any) -> str:
    return str(getattr(config, "send_backend", "dry_run") or "dry_run").strip().lower()


def build_send_driver(config: BotConfig) -> SendingDriver | None:
    name = normalize_send_driver_name(config.send_driver)
    if not name or name == "not_implemented":
        return None
    spec = _SEND_DRIVER_SPECS.get(name)
    if spec is None:
        return None
    return spec.builder(config)


def is_real_send_driver_implemented(name: str) -> bool:
    spec = _SEND_DRIVER_SPECS.get(normalize_send_driver_name(name))
    return bool(spec and spec.real_send_implemented)


def is_send_driver_registered(name: str) -> bool:
    return normalize_send_driver_name(name) in _SEND_DRIVER_SPECS


def implemented_send_drivers() -> list[str]:
    return sorted(name for name, spec in _SEND_DRIVER_SPECS.items() if spec.real_send_implemented)


def registered_send_drivers() -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "real_send_implemented": spec.real_send_implemented,
            "description": spec.description,
        }
        for spec in sorted(_SEND_DRIVER_SPECS.values(), key=lambda item: item.name)
    ]


def probe_send_driver(config: BotConfig) -> dict[str, Any]:
    name = normalize_send_driver_name(config.send_driver)
    driver = build_send_driver(config)
    probe = getattr(driver, "probe", None)
    payload = {
        "configured_driver": config.send_driver,
        "normalized_driver": name,
        "registered": is_send_driver_registered(name),
        "real_send_implemented": is_real_send_driver_implemented(name),
        "send_enabled": config.send_enabled,
        "send_backend": _normalized_send_backend(config),
        "weflow_http": {},
        "weflow_base_url": str(getattr(config, "weflow_base_url", "http://127.0.0.1:5031") or "http://127.0.0.1:5031"),
        "weflow_token_env": str(getattr(config, "weflow_token_env", "WEFLOW_API_TOKEN") or "WEFLOW_API_TOKEN"),
        "weflow_send_text_path": str(getattr(config, "weflow_send_text_path", "/send/text") or "/send/text"),
        "weflow_send_file_path": str(getattr(config, "weflow_send_file_path", "/send/file") or "/send/file"),
        "wechat_native_http": {},
        "wechat_native_base_url": str(
            getattr(config, "wechat_native_base_url", "http://127.0.0.1:30001") or "http://127.0.0.1:30001"
        ),
        "wechat_native_send_text_path": str(getattr(config, "wechat_native_send_text_path", "/SendTextMsg") or "/SendTextMsg"),
        "wechat_native_send_image_path": str(getattr(config, "wechat_native_send_image_path", "/SendImgMsg") or "/SendImgMsg"),
        "wechat_native_send_file_path": str(
            getattr(config, "wechat_native_send_file_path", "/send_file_msg") or "/send_file_msg"
        ),
        "wechat_native_status_path": str(getattr(config, "wechat_native_status_path", "/QueryDB/status") or "/QueryDB/status"),
        "driver_present": driver is not None,
        "registered_send_drivers": registered_send_drivers(),
    }
    if str(payload["send_backend"]).strip().lower() == "weflow_http":
        payload["weflow_http"] = weflow_http_status(
            str(payload["weflow_base_url"]),
            token_env=str(payload["weflow_token_env"]),
        )
    if str(payload["send_backend"]).strip().lower() == "wechat_native_http":
        payload["wechat_native_http"] = wechat_native_http_status(
            str(payload["wechat_native_base_url"]),
            text_path=str(payload["wechat_native_send_text_path"]),
            image_path=str(payload["wechat_native_send_image_path"]),
            file_path=str(payload["wechat_native_send_file_path"]),
            status_path=str(payload["wechat_native_status_path"]),
        )
    if probe is None:
        payload["driver_probe"] = None
        return payload
    result = probe()
    payload["driver_probe"] = result.__dict__ if hasattr(result, "__dict__") else result
    return payload


def normalize_send_driver_name(name: str) -> str:
    return str(name or "").strip().lower()

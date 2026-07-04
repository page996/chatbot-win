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
        builder=lambda config: BridgeOutboxSendDriver(send_enabled=config.send_enabled, data_dir=config.data_dir),
        real_send_implemented=True,
        description="Non-foreground outbox producer; delivered by the WeChatFerry send bridge worker",
    ),
}


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
        "driver_present": driver is not None,
        "registered_send_drivers": registered_send_drivers(),
    }
    if probe is None:
        payload["driver_probe"] = None
        return payload
    result = probe()
    payload["driver_probe"] = result.__dict__ if hasattr(result, "__dict__") else result
    return payload


def normalize_send_driver_name(name: str) -> str:
    return str(name or "").strip().lower()

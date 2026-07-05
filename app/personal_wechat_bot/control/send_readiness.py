from __future__ import annotations

from pathlib import Path
from typing import Any

from app.personal_wechat_bot.config.loader import load_config
from app.personal_wechat_bot.control.preflight import build_preflight_report
from app.personal_wechat_bot.wechat_driver.send_driver_factory import is_send_driver_registered


def build_send_readiness_report(data_dir: str | Path = "data") -> dict[str, Any]:
    config = load_config(data_dir)
    preflight = build_preflight_report(config, show_accepted=True)
    checks = _checks(preflight)
    blockers = [item for item in checks if item["status"] == "blocker"]
    warnings = [item for item in checks if item["status"] == "warn"]
    passed = [item for item in checks if item["status"] == "pass"]
    return {
        "status": "blocked" if blockers else ("warn" if warnings else "ready"),
        "data_dir": str(Path(data_dir).resolve()),
        "mode": preflight["mode"],
        "summary": {
            "passed": len(passed),
            "warnings": len(warnings),
            "blockers": len(blockers),
        },
        "send_policy": preflight["send_policy"],
        "wechat_access": preflight["wechat_access"],
        "conversation_channels": preflight.get("conversation_channels", {}),
        "model": {
            "provider": preflight["model"]["provider"],
            "model": preflight["model"]["model"],
            "api_key_present": preflight["model"]["api_key_present"],
            "key_pool_available_count": preflight["model"]["key_pool_available_count"],
            "max_concurrency": preflight["model"]["max_concurrency"],
        },
        "checks": checks,
        "required_next_steps": _required_next_steps(blockers, preflight),
        "recommended_rollout": _recommended_rollout(preflight),
    }


def _checks(preflight: dict[str, Any]) -> list[dict[str, str]]:
    send_policy = preflight.get("send_policy", {})
    wechat_access = preflight.get("wechat_access", {})
    model = preflight.get("model", {})
    channels = preflight.get("conversation_channels", {})
    checks: list[dict[str, str]] = []
    checks.append(
        _check(
            "rollout_mode",
            "pass" if send_policy.get("dry_run") else "warn",
            "dry_run is active",
            f"{preflight.get('mode')} mode is active; keep guarded rollout narrow",
        )
    )
    checks.append(
        _check(
            "real_send_driver",
            "pass" if send_policy.get("real_send_implemented") else "blocker",
            "real WeChat send driver is implemented",
            "configured send driver is not implemented or not selected",
        )
    )
    checks.append(
        _check(
            "send_enabled",
            "pass" if send_policy.get("send_enabled") else "blocker",
            "send_enabled is true",
            "send_enabled is false; no code path should send real WeChat messages",
        )
    )
    # A real send driver + send_enabled but a dry_run backend means the bridge
    # worker only logs and acks 'sent' without delivering anything — the ledger
    # and UI would show 'sent' for a message that never left. Warn loudly so an
    # operator doesn't mistake dry-run acks for real delivery.
    send_backend = str(send_policy.get("send_backend") or "dry_run").lower()
    intends_real_send = bool(send_policy.get("send_enabled")) and bool(send_policy.get("real_send_implemented"))
    checks.append(
        _check(
            "send_backend",
            "pass" if send_backend == "wcf" else ("warn" if intends_real_send else "pass"),
            f"send backend: {send_backend}",
            f"send backend is '{send_backend}', not 'wcf'; queued replies are acked as sent but NOT delivered",
        )
    )
    checks.append(
        _check(
            "wechat_write_access",
            "pass" if not wechat_access.get("read_only") else "blocker",
            "WeChat driver has write access",
            "current WeChat access is read-only",
        )
    )
    driver_name = str(wechat_access.get("send_driver") or "")
    driver_registered = is_send_driver_registered(driver_name)
    checks.append(
        _check(
            "send_driver_name",
            "pass" if driver_registered else "blocker",
            f"send driver: {driver_name}",
            f"send driver '{driver_name}' is not a registered driver",
        )
    )
    checks.append(
        _check(
            "api_keys",
            "pass" if model.get("api_key_present") else "blocker",
            f"{model.get('key_pool_available_count', 0)} model key(s) available",
            "no model API key is available",
        )
    )
    checks.append(
        _check(
            "conversation_channels",
            "pass" if channels.get("auto_register_private") and channels.get("auto_register_groups") else "warn",
            "conversation channels auto-register private chats and groups",
            "conversation channel auto-registration is incomplete",
        )
    )
    checks.append(
        _check(
            "confirm_first_rollout",
            "pass" if preflight.get("mode") == "confirm" else "warn",
            "confirm mode is active",
            "confirm mode is not active; use confirm before real auto sending",
        )
    )
    return checks


def _check(check_id: str, status: str, ok_detail: str, bad_detail: str | None = None) -> dict[str, str]:
    return {
        "id": check_id,
        "status": status,
        "detail": ok_detail if status == "pass" else (bad_detail or ok_detail),
    }


def _required_next_steps(blockers: list[dict[str, str]], preflight: dict[str, Any]) -> list[str]:
    next_steps: list[str] = []
    ids = {item["id"] for item in blockers}
    if {"real_send_driver", "send_driver_name", "wechat_write_access"} & ids:
        next_steps.append("select and validate a guarded real WeChat send driver")
    if "send_enabled" in ids:
        next_steps.append("keep send_enabled=false until a guarded send driver passes confirm-mode rollout")
    if "api_keys" in ids:
        next_steps.append("configure at least one available model API key")
    # The bridge_outbox driver only *queues* replies; a separate worker must be
    # running to deliver them. Surface this whenever bridge_outbox is the active
    # driver (not gated on a blocker id — there is no check that emits one, and
    # the worker requirement holds even when every other check passes).
    send_driver = str(preflight.get("wechat_access", {}).get("send_driver") or "")
    if send_driver == "bridge_outbox":
        next_steps.append(
            "start the WeChatFerry send bridge worker (scripts/send_bridge_worker.py) so queued replies are delivered"
        )
    return next_steps


def _recommended_rollout(preflight: dict[str, Any]) -> list[str]:
    send_policy = preflight.get("send_policy", {})
    wechat_access = preflight.get("wechat_access", {})
    if send_policy.get("real_send_implemented"):
        steps = [
            "keep confirm mode active while validating guarded real sends",
            "approve/send only from the sidebar or confirm queue after reviewing message text",
            "start the WeChatFerry send bridge worker so approved replies are delivered non-foreground by wxid/roomid",
            "limit initial real sending to one private conversation and very low rate",
            "only consider auto mode after confirm-mode audit logs are clean",
        ]
        if not send_policy.get("send_enabled") or wechat_access.get("read_only"):
            steps.insert(0, "enable guarded sending only after driver probe reports ready")
        return steps
    return [
        "keep dry_run while validating ingestion and channel isolation",
        "implement a real send driver behind an explicit feature flag",
        "run confirm mode first and require manual approval from confirm_queue",
        "limit initial real sending to one private conversation and very low rate",
        "only consider auto mode after confirm-mode audit logs are clean",
    ]

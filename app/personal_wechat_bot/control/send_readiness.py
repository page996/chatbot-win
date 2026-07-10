from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.config.loader import load_config
from app.personal_wechat_bot.control.preflight import build_preflight_report
from app.personal_wechat_bot.runtime.send_bridge_worker import (
    bridge_worker_config_signature,
    bridge_worker_lock_alive,
    bridge_worker_lock_path,
)
from app.personal_wechat_bot.wechat_driver.send_driver_factory import is_send_driver_registered
from app.personal_wechat_bot.wechat_driver.send_backends import (
    wechat_native_http_status,
    weflow_http_status,
)


def build_send_readiness_report(data_dir: str | Path = "data") -> dict[str, Any]:
    config = load_config(data_dir)
    preflight = build_preflight_report(config, show_accepted=True, include_tool_health=False)
    checks = _checks(preflight, config)
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
        "required_next_steps": _required_next_steps(blockers, preflight, data_dir, config),
        "recommended_rollout": _recommended_rollout(preflight),
    }


def _checks(preflight: dict[str, Any], config: Any) -> list[dict[str, str]]:
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
    # worker only logs and acks 'sent' without delivering anything; the ledger
    # and UI would show 'sent' for a message that never left. Warn loudly so an
    # operator doesn't mistake dry-run acks for real delivery.
    send_backend = str(send_policy.get("send_backend") or "dry_run").lower()
    intends_real_send = bool(send_policy.get("send_enabled")) and bool(send_policy.get("real_send_implemented"))
    real_backend_selected = send_backend in {"weflow_http", "wechat_native_http"}
    checks.append(
        _check(
            "send_backend",
            "pass" if real_backend_selected else ("warn" if intends_real_send else "pass"),
            f"send backend: {send_backend}",
            f"send backend is '{send_backend}', not a real delivery backend; queued replies are acked as sent but NOT delivered",
        )
    )
    if send_backend == "weflow_http":
        weflow_status = weflow_http_status(
            str(getattr(config, "weflow_base_url", "http://127.0.0.1:5031") or "http://127.0.0.1:5031"),
            token_env=str(getattr(config, "weflow_token_env", "WEFLOW_API_TOKEN") or "WEFLOW_API_TOKEN"),
            timeout_seconds=min(float(getattr(config, "weflow_send_timeout_seconds", 35.0) or 35.0), 3.0),
        )
        weflow_ok = bool(weflow_status.get("available")) and bool(weflow_status.get("token_present"))
        weflow_capabilities = (
            weflow_status.get("send_capabilities")
            if isinstance(weflow_status.get("send_capabilities"), dict)
            else {}
        )
        text_capability = (
            weflow_capabilities.get("text", {})
            if isinstance(weflow_capabilities.get("text"), dict)
            else {}
        )
        file_capability = (
            weflow_capabilities.get("file", {})
            if isinstance(weflow_capabilities.get("file"), dict)
            else {}
        )
        weflow_text_supported = text_capability.get("supports") is not False
        checks.append(
            _check(
                "weflow_http_send_bridge",
                "pass" if weflow_ok and weflow_text_supported else ("blocker" if intends_real_send else "warn"),
                "WeFlow HTTP bridge is reachable and token is present",
                (
                    "WeFlow HTTP bridge does not expose text sending"
                    if weflow_ok and not weflow_text_supported
                    else f"WeFlow HTTP bridge is not ready for sending: {weflow_status.get('reason') or 'token_missing'}"
                ),
            )
        )
        if file_capability.get("supports") is False:
            checks.append(
                _check(
                    "weflow_http_file_send_capability",
                    "warn",
                    "WeFlow HTTP bridge does not expose file sending",
                    "WeFlow HTTP bridge does not expose file sending",
                )
            )
    else:
        checks.append(
            _check(
                "weflow_http_send_bridge",
                "pass",
                "WeFlow HTTP send bridge is not selected",
                "WeFlow HTTP send bridge is not selected",
            )
        )
    if send_backend == "wechat_native_http":
        hook_status = wechat_native_http_status(
            str(getattr(config, "wechat_native_base_url", "http://127.0.0.1:30001") or "http://127.0.0.1:30001"),
            text_path=str(getattr(config, "wechat_native_send_text_path", "/SendTextMsg") or "/SendTextMsg"),
            image_path=str(getattr(config, "wechat_native_send_image_path", "/SendImgMsg") or "/SendImgMsg"),
            file_path=str(getattr(config, "wechat_native_send_file_path", "/send_file_msg") or "/send_file_msg"),
            status_path=str(getattr(config, "wechat_native_status_path", "/QueryDB/status") or "/QueryDB/status"),
            timeout_seconds=min(float(getattr(config, "wechat_native_timeout_seconds", 15.0) or 15.0), 3.0),
        )
        checks.append(
            _check(
                "wechat_native_http_send_bridge",
                "pass" if bool(hook_status.get("available")) else ("blocker" if intends_real_send else "warn"),
                "WeChat Native HTTP service is reachable and logged in",
                f"WeChat Native HTTP service is not ready for sending: {hook_status.get('reason') or 'not_login'}",
            )
        )
        checks.append(
            _check(
                "wechat_native_text_delivery_verification",
                "pass" if _weflow_readback_verification_available(config) else "warn",
                "WeChat Native HTTP text sends are verified by hook response or WeFlow outgoing-message readback",
                "WeChat Native HTTP text endpoint accepts send requests, but delivery cannot be verified unless WeFlow readback is available or the hook response includes delivery_verified=true",
            )
        )
        send_capabilities = hook_status.get("send_capabilities", {})
        image_capability = (
            send_capabilities
            .get("image", {})
            .get("status", "")
        )
        file_capability = (
            send_capabilities
            .get("file", {})
            .get("status", "")
        )
        if image_capability == "default_route_unsupported_in_text_hook_build":
            checks.append(
                _check(
                    "wechat_native_media_send_capability",
                    "warn",
                    "WeChat Native text and ordinary-file sends are configured; default image/GIF sending is still unsupported",
                    "WeChat Native text and ordinary-file sends are configured; default image/GIF sending is still unsupported",
                )
            )
        elif file_capability == "default_route_accepts_unverified_native_file":
            file_readback_available = _weflow_readback_verification_available(config)
            checks.append(
                _check(
                    "wechat_native_file_delivery_verification",
                    "pass" if file_readback_available else "warn",
                    (
                        "WeChat Native ordinary-file sends are verified by WeFlow readback; late async file writes are rechecked without re-sending"
                        if file_readback_available
                        else "WeChat Native ordinary-file endpoint accepts requests, but delivery remains unverified until readback or operator confirmation"
                    ),
                    "WeChat Native ordinary-file endpoint accepts requests, but delivery remains unverified until readback or operator confirmation",
                )
            )
    else:
        checks.append(
            _check(
                "wechat_native_http_send_bridge",
                "pass",
                "WeChat Native HTTP send bridge is not selected",
                "WeChat Native HTTP send bridge is not selected",
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
            "pass"
            if channels.get("auto_register_private") == "identified_or_accepted_only"
            and channels.get("auto_register_groups")
            and channels.get("blocks_unknown_private")
            else "warn",
            "conversation channels admit identified private chats and block unknown private contacts",
            "conversation channel admission policy is incomplete",
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


def _weflow_readback_verification_available(config: Any) -> bool:
    token_env = str(getattr(config, "weflow_token_env", "WEFLOW_API_TOKEN") or "WEFLOW_API_TOKEN")
    if not (os.environ.get(token_env) or os.environ.get("WEFLOW_API_TOKEN")):
        return False
    status = weflow_http_status(
        str(getattr(config, "weflow_base_url", "http://127.0.0.1:5031") or "http://127.0.0.1:5031"),
        token_env=token_env,
        timeout_seconds=min(float(getattr(config, "weflow_send_timeout_seconds", 35.0) or 35.0), 3.0),
    )
    return bool(status.get("available")) and bool(status.get("token_present"))


def _required_next_steps(
    blockers: list[dict[str, str]],
    preflight: dict[str, Any],
    data_dir: str | Path,
    config: Any,
) -> list[str]:
    next_steps: list[str] = []
    ids = {item["id"] for item in blockers}
    if {"real_send_driver", "send_driver_name", "wechat_write_access"} & ids:
        next_steps.append("select and validate a guarded real WeChat send driver")
    if "send_enabled" in ids:
        next_steps.append("keep send_enabled=false until a guarded send driver passes confirm-mode rollout")
    if "api_keys" in ids:
        next_steps.append("configure at least one available model API key")
    if "weflow_http_send_bridge" in ids:
        next_steps.append("start the local WeFlow HTTP service and configure WEFLOW_API_TOKEN before approving real sends")
    if "wechat_native_http_send_bridge" in ids:
        next_steps.append("start the local PC WeChat Native HTTP service and confirm /QueryDB/status reports IsLogin=1")
    # The bridge_outbox driver only *queues* replies; a separate worker must be
    # running to deliver them. Surface this whenever bridge_outbox is the active
    # driver (not gated on a blocker id; there is no check that emits one, and
    # the worker requirement holds even when every other check passes).
    send_driver = str(preflight.get("wechat_access", {}).get("send_driver") or "")
    send_enabled = bool(preflight.get("send_policy", {}).get("send_enabled"))
    if send_enabled and send_driver == "bridge_outbox" and not _bridge_worker_ready_for_config(data_dir, config):
        next_steps.append(
            "start the send bridge worker (scripts/send_bridge_worker.py) so queued replies are delivered by the selected backend"
        )
    return next_steps


def _bridge_worker_ready_for_config(data_dir: str | Path, config: Any) -> bool:
    if not bridge_worker_lock_alive(data_dir):
        return False
    lock_path = bridge_worker_lock_path(data_dir)
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    if not isinstance(payload, dict):
        return True
    signature = payload.get("config_signature")
    if not isinstance(signature, dict) or not signature:
        return True
    return signature == bridge_worker_config_signature(config)


def _recommended_rollout(preflight: dict[str, Any]) -> list[str]:
    send_policy = preflight.get("send_policy", {})
    wechat_access = preflight.get("wechat_access", {})
    if send_policy.get("real_send_implemented"):
        steps = [
            "keep confirm mode active while validating guarded real sends",
            "approve/send only from the sidebar or confirm queue after reviewing message text",
            "start the send bridge worker so approved replies are delivered non-foreground by wxid/roomid",
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

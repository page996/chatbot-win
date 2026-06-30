from __future__ import annotations

from pathlib import Path
from typing import Any

from app.personal_wechat_bot.conversation.channel_store import ConversationChannelStore
from app.personal_wechat_bot.config.loader import load_config
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool
from app.personal_wechat_bot.control.send_commands import (
    approve_confirm_item,
    list_confirm_queue,
    list_send_audit,
    probe_send_controls,
    reject_confirm_item,
    send_approved_confirm_item,
    set_send_controls,
)
from app.personal_wechat_bot.control.send_readiness import build_send_readiness_report
from app.personal_wechat_bot.wechat_driver.window_introspection import build_wechat_window_probe


QUEUE_STATUSES = ("pending", "approved", "rejected", "sent", "failed")


def build_sidebar_state(data_dir: str | Path = "data") -> dict[str, Any]:
    config = load_config(data_dir)
    queues = {status: list_confirm_queue(data_dir, status=status) for status in QUEUE_STATUSES}
    channels = _channel_state(data_dir)
    return {
        "status": "ok",
        "role": "visual_audit_console",
        "capture": {
            "owner": "backend_message_sources",
            "sidebar_role": "audit_and_send_controls_only",
            "window_probe_role": "diagnostic_only",
            "supports_multi_conversation": True,
        },
        "config": {
            "mode": config.mode,
            "send_enabled": config.send_enabled,
            "send_driver": config.send_driver,
            "send_confirm_required": config.send_confirm_required,
            "send_max_chars": config.send_max_chars,
            "send_min_interval_seconds": config.send_min_interval_seconds,
        },
        "channels": channels,
        "queues": queues,
        "readiness": build_send_readiness_report(data_dir),
        "driver_probe": probe_send_controls(data_dir)["probe"],
        "wechat_window_probe": build_wechat_window_probe(max_children=80, max_controls=160),
        "audit": list_send_audit(data_dir, limit=30),
    }


def build_sidebar_wechat_probe(data_dir: str | Path = "data") -> dict[str, Any]:
    _ = data_dir
    return build_wechat_window_probe()


def update_sidebar_controls(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    mode = payload.get("mode")
    enabled = payload.get("send_enabled")
    driver = payload.get("send_driver")
    confirm_required = payload.get("send_confirm_required")
    max_chars = payload.get("send_max_chars")
    min_interval_seconds = payload.get("send_min_interval_seconds")
    controls = set_send_controls(
        data_dir,
        mode=str(mode) if mode is not None else None,
        enabled=bool(enabled) if enabled is not None else None,
        driver=str(driver) if driver is not None else None,
        confirm_required=bool(confirm_required) if confirm_required is not None else None,
        max_chars=int(max_chars) if max_chars is not None else None,
        min_interval_seconds=int(min_interval_seconds) if min_interval_seconds is not None else None,
    )
    return {"status": "ok", "send_controls": controls}


def sidebar_queue_action(data_dir: str | Path, action: str, queue_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    reviewer = str(payload.get("reviewer", "sidebar"))
    note = str(payload.get("note", ""))
    if action == "approve":
        return approve_confirm_item(data_dir, queue_id, reviewer=reviewer, note=note)
    if action == "reject":
        return reject_confirm_item(data_dir, queue_id, reviewer=reviewer, note=note)
    if action == "send-approved":
        return send_approved_confirm_item(data_dir, queue_id)
    raise ValueError(f"unknown queue action: {action}")


def _channel_state(data_dir: str | Path) -> dict[str, Any]:
    config = load_config(data_dir)
    root = Path(data_dir)
    chat_provider = config.providers.get("chat", config.llm)
    key_pool = ApiKeyPool(chat_provider, root)
    store = ConversationChannelStore(
        root,
        key_pool,
        file_workspace_root=root / "file_workspace",
        context_root=root / "conversation_ledgers",
    )
    channels = []
    for channel in store.list_channels():
        payload = {
            "conversation_id": channel.conversation_id,
            "conversation_type": channel.conversation_type,
            "chat_title": channel.chat_title,
            "status": channel.status,
            "key_slots": channel.key_slots,
            "api_key_refs": channel.api_key_refs,
            "session_scope": channel.session_scope,
            "backend_dir": channel.backend_dir,
            "context_dir": channel.context_dir,
            "file_workspace_dir": channel.file_workspace_dir,
            "sender_names": channel.sender_names,
            "sender_wechat_ids": channel.sender_wechat_ids,
            "updated_at": channel.updated_at,
        }
        channels.append(payload)
    return {
        "status": "ok",
        "policy": "auto_accept_wechat_contacts_and_groups",
        "count": len(channels),
        "private_count": sum(1 for item in channels if item["conversation_type"] == "private"),
        "group_count": sum(1 for item in channels if item["conversation_type"] == "group"),
        "items": sorted(channels, key=lambda item: item.get("updated_at", ""), reverse=True),
    }

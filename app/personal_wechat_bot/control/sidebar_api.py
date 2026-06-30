from __future__ import annotations

import re
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
from app.personal_wechat_bot.wechat_driver.window_binding import WeChatWindowBindingStore


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
    visible_policy = _visible_channel_policy(root, config)
    channels = []
    hidden = []
    for channel in store.list_channels():
        visible, reason = _sidebar_channel_visible(channel, visible_policy)
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
            "source_names": channel.source_names,
            "trusted_channel_source": channel.trusted_channel_source,
            "updated_at": channel.updated_at,
        }
        if visible:
            channels.append(payload)
        else:
            hidden.append({**payload, "hidden_reason": reason})
    return {
        "status": "ok",
        "policy": "auto_accept_wechat_contacts_and_groups",
        "count": len(channels),
        "total_count": len(channels) + len(hidden),
        "hidden_count": len(hidden),
        "hidden_reasons": _reason_counts(hidden),
        "private_count": sum(1 for item in channels if item["conversation_type"] == "private"),
        "group_count": sum(1 for item in channels if item["conversation_type"] == "group"),
        "items": sorted(channels, key=lambda item: item.get("updated_at", ""), reverse=True),
        "hidden_items": sorted(hidden, key=lambda item: item.get("updated_at", ""), reverse=True)[:20],
    }


def _visible_channel_policy(root: Path, config: Any) -> dict[str, set[str]]:
    bindings = WeChatWindowBindingStore(root).list_bindings()
    bound_ids = {
        str(item.get("conversation_id", "")).strip()
        for item in bindings
        if str(item.get("conversation_id", "")).strip() and str(item.get("status", "active")) == "active"
    }
    bound_titles = {
        str(item.get("chat_title", "")).strip()
        for item in bindings
        if str(item.get("chat_title", "")).strip() and str(item.get("status", "active")) == "active"
    }
    return {
        "accepted_contacts": {str(item).strip() for item in config.accepted_contacts if str(item).strip()},
        "accepted_groups": {str(item).strip() for item in config.accepted_groups if str(item).strip()},
        "bound_ids": bound_ids,
        "bound_titles": bound_titles,
    }


def _sidebar_channel_visible(channel: Any, policy: dict[str, set[str]] | None = None) -> tuple[bool, str]:
    title = str(channel.chat_title or "").strip()
    if not title:
        return False, "empty_title"
    if title.lower() in {"wechat agent console", "windows powershell", "powershell", "codex"}:
        return False, "tool_window"
    if _looks_like_probe_fragment(title):
        return False, "probe_fragment"
    if _looks_like_mojibake(title):
        return False, "mojibake"
    if not _channel_has_visible_trust(channel, policy or {}):
        return False, "untrusted_legacy_channel"
    return True, ""


def _channel_has_visible_trust(channel: Any, policy: dict[str, set[str]]) -> bool:
    if bool(getattr(channel, "trusted_channel_source", False)):
        return True
    source_names = {str(item).strip() for item in getattr(channel, "source_names", []) if str(item).strip()}
    if source_names.intersection({"backend_events_jsonl", "backend_file_watcher", "manual_backend_event"}):
        return True
    conversation_id = str(getattr(channel, "conversation_id", "")).strip()
    title = str(getattr(channel, "chat_title", "")).strip()
    sender_names = {str(item).strip() for item in getattr(channel, "sender_names", []) if str(item).strip()}
    sender_ids = {str(item).strip() for item in getattr(channel, "sender_wechat_ids", []) if str(item).strip()}
    if conversation_id in policy.get("bound_ids", set()) or title in policy.get("bound_titles", set()):
        return True
    if title in policy.get("accepted_contacts", set()) or sender_names.intersection(policy.get("accepted_contacts", set())):
        return True
    if title in policy.get("accepted_groups", set()):
        return True
    if sender_ids:
        return True
    return False


def _looks_like_probe_fragment(title: str) -> bool:
    if re.fullmatch(r"[+%0-9.\-/: ]{1,12}", title):
        return True
    lowered = title.lower()
    if any(token in lowered for token in ["driver=", "enabled=", "send_enabled", "not_implemented"]):
        return True
    if len(title) <= 2 and not re.search(r"[\u4e00-\u9fffA-Za-z]", title):
        return True
    return False


def _looks_like_mojibake(title: str) -> bool:
    suspicious = ("�", "锛", "绔", "鐚", "鍟", "娴", "灏", "鏃", "闀", "涓", "鎺", "乬")
    if any(token in title for token in suspicious):
        return True
    non_ascii = sum(1 for char in title if ord(char) > 127)
    if non_ascii >= 2 and not re.search(r"[\u4e00-\u9fff]", title):
        return True
    return False


def _reason_counts(hidden: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in hidden:
        reason = str(item.get("hidden_reason", "unknown"))
        counts[reason] = counts.get(reason, 0) + 1
    return counts

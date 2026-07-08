from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.config.schema import DEFAULT_LLM_MAX_CONCURRENCY
from app.personal_wechat_bot.conversation.channel_store import ConversationChannelStore
from app.personal_wechat_bot.conversation.channel_state_store import (
    ChannelStateStore,
    build_channel_state_projection,
    merge_channel_state_projection,
)
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.config.loader import ensure_config, load_config, migrate_file_allowed_extensions, save_config, set_model_provider
from app.personal_wechat_bot.domain.models import NormalizedMessage, utc_now_iso
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool
from app.personal_wechat_bot.normalizer.normalizer import conversation_id_for
from app.personal_wechat_bot.persona.runtime_cards import RuntimeCardStore
from app.personal_wechat_bot.reply_gate.send_executor import GuardedSendExecutor
from app.personal_wechat_bot.runtime.hook_pull_runner import HookMessagePullRunner
from app.personal_wechat_bot.runtime.polling_runner import PollingRunner
from app.personal_wechat_bot.runtime.process_lock import ProcessLock, ProcessLockError, blocking_process_lock
from app.personal_wechat_bot.runtime.resource_governor import audit_local_resources
from app.personal_wechat_bot.runtime.resource_gate import gpu_gate_snapshot, llm_gate_snapshot
from app.personal_wechat_bot.runtime.resource_scheduler import ResourceScheduler
from app.personal_wechat_bot.runtime.weflow_state_summary import summarize_weflow_bridge_state
from app.personal_wechat_bot.runtime.weflow_worker_metrics import WeflowWorkerMetrics
from app.personal_wechat_bot.tasks.manager import TaskStatusStore
from app.personal_wechat_bot.tools.permissions import resolve_allowed_roots
from app.personal_wechat_bot.wechat_driver.send_driver_factory import build_send_driver
from app.personal_wechat_bot.workspace.file_workspace import FileWorkspace
from app.personal_wechat_bot.control.send_commands import (
    approve_confirm_item,
    clear_send_audit,
    list_confirm_queue,
    list_send_audit,
    probe_send_controls,
    reject_confirm_item,
    remove_confirm_item,
    send_approved_confirm_item,
    set_send_controls,
    sync_bridge_ack_to_send_state,
)
from app.personal_wechat_bot.control.send_readiness import build_send_readiness_report
from app.personal_wechat_bot.wechat_driver.window_introspection import build_wechat_window_probe
from app.personal_wechat_bot.wechat_driver.window_binding import WeChatWindowBindingStore
from app.personal_wechat_bot.wechat_driver.backend_events import BackendEventJsonlDriver
from app.personal_wechat_bot.wechat_driver.backend_events import append_backend_event_payload
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import BackendAttachmentParser
from app.personal_wechat_bot.wechat_driver.bridge_send import bridge_ack, bridge_state, is_terminal_bridge_ack_status
from app.personal_wechat_bot.wechat_driver.hook_events import HookEventJsonlImporter
from app.personal_wechat_bot.wechat_driver.system_accounts import is_system_account
from app.personal_wechat_bot.wechat_driver.hook_source_bridge import (
    WeFlowHttpBridge,
    require_weflow_ready,
    weflow_health_status,
)
from app.personal_wechat_bot.wechat_driver.voice_cache_resolver import WeChatVoiceCacheResolver
from app.personal_wechat_bot.vision.ocr import build_default_ocr_engine
from app.personal_wechat_bot.voice.asr import LocalAsrSubprocessEngine


QUEUE_STATUSES = ("pending", "approved", "queued_to_bridge", "rejected", "sent", "failed")
_HISTORY_RESET_DIRS = (
    "agent_workspace",
    "conversation_channels",
    "conversation_ledgers",
    "conversation_sessions",
    "file_workspace",
    "send_bridge",
    "tool_outputs",
    "task_manager",
)
_HISTORY_RESET_FILES = (
    "backend_events.jsonl",
    "backend_events.jsonl.raw_ids.json",
    "backend_file_watcher.sqlite",
    "confirm_queue.jsonl",
    "conversation_cooldowns.sqlite",
    "channel_state.sqlite",
    "channel_state.sqlite-shm",
    "channel_state.sqlite-wal",
    "file_index.sqlite",
    "hook_events.jsonl",
    "hook_events_state.json",
    "logs.jsonl",
    "processed_messages.sqlite",
    "scheduler.sqlite",
    "scheduler.sqlite-shm",
    "scheduler.sqlite-wal",
    "send_audit.jsonl",
    "weflow_bridge_state.json",
    "weflow_sessions.json",
    "weflow_process.err.log",
    "weflow_process.out.log",
)
_HISTORY_RESET_LOCK_TOLERANT_FILES = {
    "weflow_process.err.log",
    "weflow_process.out.log",
}
logger = logging.getLogger(__name__)
_SIDEBAR_API_SCHEMA_VERSION = "20260707-runtime-probe-v2"
_SIDEBAR_API_LOADED_AT = utc_now_iso()
_WEFLOW_WORKERS: dict[str, dict[str, Any]] = {}
_WEFLOW_BACKFILL_JOBS: dict[str, dict[str, Any]] = {}
_WEFLOW_PULL_JOBS: dict[str, dict[str, Any]] = {}
_WEFLOW_LOCK = threading.RLock()
_WEFLOW_STATE_FILE_LOCK = threading.RLock()
# Max times the supervisor auto-restarts a died worker loop before giving up.
_WEFLOW_MAX_RESTARTS = 10
# In-process send-bridge workers, keyed by data-dir. Started alongside the
# WeFlow pull worker (the pull->reply->deliver chain needs both halves) and
# stopped with it. Each entry: {thread, stop, started_at, last_status, ...}.
_BRIDGE_WORKERS: dict[str, dict[str, Any]] = {}
_BRIDGE_LOCK = threading.Lock()
_BRIDGE_MAX_RESTARTS = 10
_WEFLOW_OPERATION_LOCK = threading.Lock()
# Serializes the hook *consume* step (import + backend replay + scheduler)
# across threads *within this process*. It fronts the cross-process file lock
# (HookMessagePullRunner.consume_lock_path) so at most one sidebar thread polls
# that file at a time instead of N threads spin-waiting on it. The file lock is
# the authoritative cross-process guard (background worker tick, pull-once,
# backfill, and any separate CLI consumer take turns over the shared offset +
# deduper); this in-process lock is the fast front door. Always acquired before
# the file lock, so the two never deadlock. See _WEFLOW consume-lock wiring in
# _build_weflow_pull_context.
_WEFLOW_CONSUMER_LOCK = threading.Lock()


def build_sidebar_state(data_dir: str | Path = "data") -> dict[str, Any]:
    config = ensure_config(data_dir)
    queues = {status: list_confirm_queue(data_dir, status=status) for status in QUEUE_STATUSES}
    channels = _channel_state(data_dir)
    send_bridge = _sidebar_bridge_state(data_dir, channels_state=channels, limit=12)
    return {
        "status": "ok",
        "server": {
            "schema_version": _SIDEBAR_API_SCHEMA_VERSION,
            "pid": os.getpid(),
            "loaded_at": _SIDEBAR_API_LOADED_AT,
            "cwd": str(Path.cwd()),
            "capabilities": {
                "runtime_probe": True,
                "resource_audit": True,
                "queue_remove": True,
                "task_manager": True,
                "gpu_gate": True,
            },
        },
        "role": "visual_audit_console",
        "capture": {
            "owner": "backend_message_sources",
            "sidebar_role": "audit_and_send_controls_only",
            "window_probe_role": "diagnostic_only",
            "supports_multi_conversation": True,
            "send_driver_boundary": "bridge_outbox queues replies for the WeChatFerry send bridge (non-foreground, delivered by wxid/roomid); backend events can receive multiple conversations without page OCR",
            "input_pipeline": "POST /api/backend-events or append-backend-event -> backend_events.jsonl -> run-agent/poll-backend-events -> conversation_ledgers",
            "background_send_status": _background_send_status(config, send_bridge, data_dir),
        },
        "config": {
            "mode": config.mode,
            "send_enabled": config.send_enabled,
            "send_driver": config.send_driver,
            "send_confirm_required": config.send_confirm_required,
            "send_max_chars": config.send_max_chars,
            "send_min_interval_seconds": config.send_min_interval_seconds,
            "ocr_mode": config.ocr_mode,
            "asr_mode": config.asr_mode,
            "file_max_bytes": config.file_max_bytes,
        },
        "channels": channels,
        "channel_states": channels.get("states", []),
        "runtime_cards": build_sidebar_runtime_cards(data_dir),
        "task_manager": build_sidebar_task_manager(data_dir),
        "resource_audit": _last_resource_audit(data_dir),
        "resource_scheduler": _resource_scheduler_snapshot(data_dir),
        "queues": queues,
        "readiness": build_send_readiness_report(data_dir),
        "driver_probe": probe_send_controls(data_dir)["probe"],
        "send_bridge": send_bridge,
        "weflow": build_sidebar_weflow_state(data_dir),
        "wechat_window_probe": _safe_wechat_window_probe(data_dir, max_children=0, max_controls=0),
        "audit": list_send_audit(data_dir, limit=30),
    }


def build_sidebar_task_manager(data_dir: str | Path = "data") -> dict[str, Any]:
    state = TaskStatusStore(data_dir).state()
    _inject_runtime_resource_limits(state, data_dir)
    return state


def sidebar_resource_audit(data_dir: str | Path = "data", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run local resource audit and persist the latest recommendation."""

    payload = payload if isinstance(payload, dict) else {}
    result = audit_local_resources()
    result["updated_at"] = utc_now_iso()
    result["manual"] = bool(payload.get("manual", True))
    path = _resource_audit_path(data_dir)
    try:
        _write_json(path, result)
        result["storage"] = str(path)
    except OSError as exc:
        result["storage_error"] = f"{type(exc).__name__}: {exc}"
    return result


def _last_resource_audit(data_dir: str | Path = "data") -> dict[str, Any]:
    payload = _read_json(_resource_audit_path(data_dir), {})
    return payload if isinstance(payload, dict) else {}


def _resource_audit_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "runtime" / "resource_audit.json"


def _resource_scheduler_snapshot(data_dir: str | Path) -> dict[str, Any]:
    try:
        config = ensure_config(data_dir)
        chat_provider = config.providers.get("chat", config.llm)
        scheduler = ResourceScheduler(
            data_dir,
            key_pool=ApiKeyPool(chat_provider, data_dir),
            provider_max_concurrency=chat_provider.max_concurrency,
        )
        return scheduler.policy_snapshot()
    except Exception as exc:
        return {
            "schema": "resource_scheduler_v1",
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _inject_runtime_resource_limits(state: dict[str, Any], data_dir: str | Path) -> None:
    scheduler = state.get("scheduler") if isinstance(state.get("scheduler"), dict) else {}
    pools = scheduler.get("resource_pools") if isinstance(scheduler.get("resource_pools"), dict) else {}
    if not isinstance(pools, dict):
        return
    llm_limit = DEFAULT_LLM_MAX_CONCURRENCY
    gpu_limit = 1
    gpu_active = 0
    gpu_policy = ""
    llm_pool = pools.setdefault("llm", {"max_parallel": DEFAULT_LLM_MAX_CONCURRENCY, "active": 0, "queued": 0})
    try:
        llm_limit = _key_pool(data_dir).concurrency_limit()
    except Exception:
        llm_limit = max(1, int(llm_pool.get("max_parallel") or DEFAULT_LLM_MAX_CONCURRENCY))
    llm_pool["max_parallel"] = llm_limit
    gpu_pool = pools.setdefault("gpu", {"max_parallel": 1, "active": 0, "queued": 0})
    try:
        snapshot = gpu_gate_snapshot()
        gpu_limit = int(snapshot.get("max_parallel") or 1)
        gpu_active = int(snapshot.get("active_slots") or 0)
        gpu_policy = str(snapshot.get("policy") or "")
    except Exception:
        gpu_limit = max(1, int(gpu_pool.get("max_parallel") or 1))
    gpu_pool["max_parallel"] = gpu_limit
    gpu_pool["active"] = max(int(gpu_pool.get("active") or 0), gpu_active)
    if gpu_policy:
        gpu_pool["policy"] = gpu_policy
    audit = _last_resource_audit(data_dir)
    resource_scheduler = _resource_scheduler_snapshot(data_dir)
    interactive = resource_scheduler.get("interactive") if isinstance(resource_scheduler.get("interactive"), dict) else {}
    background = resource_scheduler.get("background") if isinstance(resource_scheduler.get("background"), dict) else {}
    if interactive or background:
        cpu_pool = pools.setdefault("cpu_io", {"max_parallel": 2, "active": 0, "queued": 0})
        media_pool = pools.setdefault("media_cpu", {"max_parallel": 1, "active": 0, "queued": 0})
        file_pool = pools.setdefault("file_io", {"max_parallel": 1, "active": 0, "queued": 0})
        llm_interactive = pools.setdefault("llm_interactive", {"max_parallel": llm_limit, "active": 0, "queued": 0})
        llm_background = pools.setdefault("llm_background", {"max_parallel": max(1, llm_limit // 3), "active": 0, "queued": 0})
        media_pool["max_parallel"] = max(1, int(interactive.get("media_cpu") or background.get("media_cpu") or media_pool.get("max_parallel") or 1))
        file_pool["max_parallel"] = max(1, int(interactive.get("file_io") or background.get("file_io") or file_pool.get("max_parallel") or 1))
        cpu_pool["max_parallel"] = max(int(cpu_pool.get("max_parallel") or 1), int(media_pool["max_parallel"]))
        llm_interactive["max_parallel"] = max(1, int(interactive.get("llm_interactive") or interactive.get("max_parallel_conversations") or llm_limit))
        llm_background["max_parallel"] = max(1, int(background.get("llm_background") or background.get("max_parallel_conversations") or 1))
        try:
            llm_snapshot = llm_gate_snapshot(
                root=Path(data_dir) / "runtime_locks",
                total_max=llm_limit,
                interactive_max=int(llm_interactive["max_parallel"]),
                background_max=int(llm_background["max_parallel"]),
            )
            total_gate = llm_snapshot.get("total") if isinstance(llm_snapshot.get("total"), dict) else {}
            interactive_gate = llm_snapshot.get("interactive") if isinstance(llm_snapshot.get("interactive"), dict) else {}
            background_gate = llm_snapshot.get("background") if isinstance(llm_snapshot.get("background"), dict) else {}
            llm_pool["active"] = max(int(llm_pool.get("active") or 0), int(total_gate.get("active_slots") or 0))
            llm_interactive["active"] = max(int(llm_interactive.get("active") or 0), int(interactive_gate.get("active_slots") or 0))
            llm_background["active"] = max(int(llm_background.get("active") or 0), int(background_gate.get("active_slots") or 0))
            scheduler["llm_gate"] = llm_snapshot
        except Exception:
            pass
        scheduler["resource_scheduler"] = resource_scheduler
        if audit:
            scheduler["resource_audit"] = audit
        try:
            resource_limits = {
                str(name): max(1, int((pool if isinstance(pool, dict) else {}).get("max_parallel") or 1))
                for name, pool in pools.items()
            }
            scheduler["dispatch_preview"] = TaskStatusStore(data_dir).dispatch_preview(
                resource_limits=resource_limits,
                channel_limit=max(1, int(interactive.get("max_parallel_conversations") or 1)),
            )
        except Exception:
            pass
    for lane in state.get("channels", []) if isinstance(state.get("channels"), list) else []:
        audit = lane.get("resource_audit") if isinstance(lane, dict) else {}
        resources = audit.get("resources") if isinstance(audit, dict) else {}
        if not isinstance(resources, dict):
            continue
        resources.setdefault("llm", {"max_parallel": llm_limit, "active": 0, "queued": 0})["max_parallel"] = llm_limit
        resources.setdefault("gpu", {"max_parallel": gpu_limit, "active": 0, "queued": 0})["max_parallel"] = gpu_limit


def sidebar_task_action(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    store = TaskStatusStore(data_dir)
    action = str(payload.get("action") or "create").strip().lower()
    if action == "list":
        return build_sidebar_task_manager(data_dir)
    if action == "preview":
        resource_limits = payload.get("resource_limits") if isinstance(payload.get("resource_limits"), dict) else None
        return {
            "status": "ok",
            "dispatch_preview": store.dispatch_preview(
                resource_limits=resource_limits,
                channel_limit=max(1, _bounded_int(payload.get("channel_limit"), 1, 1, 100)),
                limit=max(1, _bounded_int(payload.get("limit"), 50, 1, 500)),
            ),
            "task_manager": build_sidebar_task_manager(data_dir),
        }
    if action == "events":
        return {
            "status": "ok",
            "events": store.events(
                task_id=str(payload.get("task_id") or payload.get("id") or ""),
                limit=max(1, _bounded_int(payload.get("limit"), 200, 1, 1000)),
            ),
        }
    if action == "claim":
        resource_limits = payload.get("resource_limits") if isinstance(payload.get("resource_limits"), dict) else None
        allowed_resources = payload.get("allowed_resources") if isinstance(payload.get("allowed_resources"), list) else None
        claimed = store.claim_next(
            worker_id=str(payload.get("worker_id") or payload.get("workerId") or "sidebar-worker"),
            resource_limits=resource_limits,
            channel_limit=max(1, _bounded_int(payload.get("channel_limit"), 1, 1, 100)),
            allowed_resources=[str(item) for item in allowed_resources] if allowed_resources else None,
            limit=max(1, _bounded_int(payload.get("limit"), 1, 1, 100)),
        )
        return {"status": "ok", "claimed": claimed, "task_manager": build_sidebar_task_manager(data_dir)}
    if action == "create":
        task = store.create(payload.get("task") if isinstance(payload.get("task"), dict) else payload)
        return {"status": "ok", "task": task, "task_manager": build_sidebar_task_manager(data_dir)}
    task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else payload
    if action == "update":
        task = store.update(task_id, patch)
    else:
        task = store.transition(task_id, action, patch)
    return {"status": "ok", "task": task, "task_manager": build_sidebar_task_manager(data_dir)}


def sidebar_channel_state_action(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    store = ChannelStateStore(data_dir)
    action = str(payload.get("action") or "update_control").strip().lower()
    conversation_id = str(payload.get("conversation_id") or payload.get("conversationId") or "").strip()
    if not conversation_id:
        raise ValueError("conversation_id is required")
    patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else {}
    control_patch: dict[str, Any] = {}
    if action == "pause":
        control_patch["mode"] = "paused"
        if "wait_reason" in payload or "waitReason" in payload:
            control_patch["wait_reason"] = payload.get("wait_reason") or payload.get("waitReason") or ""
    elif action == "resume":
        control_patch.update({"mode": "active", "wait_reason": "", "snoozed_until": ""})
    elif action == "mute":
        control_patch["mode"] = "muted"
    elif action == "pin":
        control_patch["pinned"] = True
    elif action == "unpin":
        control_patch["pinned"] = False
    elif action == "snooze":
        control_patch["mode"] = "snoozed"
        control_patch["snoozed_until"] = payload.get("snoozed_until") or payload.get("snoozedUntil") or patch.get("snoozed_until") or ""
    elif action == "set_priority":
        control_patch["priority"] = payload.get("priority", patch.get("priority"))
    elif action == "note":
        control_patch["operator_note"] = payload.get("operator_note") or payload.get("operatorNote") or patch.get("operator_note") or ""
    elif action in {"update", "update_control"}:
        control_patch.update(patch)
    else:
        raise ValueError(f"unsupported channel state action: {action}")
    if "priority" in payload and action not in {"set_priority", "update", "update_control"}:
        control_patch["priority"] = payload.get("priority")
    if "pinned" in payload and action not in {"pin", "unpin", "update", "update_control"}:
        control_patch["pinned"] = payload.get("pinned")
    if "operator_note" in payload or "operatorNote" in payload:
        control_patch["operator_note"] = payload.get("operator_note") or payload.get("operatorNote") or ""
    updated = store.patch_control(
        conversation_id,
        control_patch,
        updated_by=str(payload.get("updated_by") or payload.get("updatedBy") or "sidebar"),
    )
    return {
        "status": "ok",
        "action": action,
        "channel_state": updated,
        "task_manager": build_sidebar_task_manager(data_dir),
        "channels": _channel_state(data_dir),
    }


def build_sidebar_wechat_probe(data_dir: str | Path = "data") -> dict[str, Any]:
    return _safe_wechat_window_probe(data_dir, max_children=200, max_controls=300)


def _safe_wechat_window_probe(
    data_dir: str | Path,
    *,
    max_children: int = 80,
    max_controls: int = 160,
) -> dict[str, Any]:
    try:
        probe = build_wechat_window_probe(max_children=max_children, max_controls=max_controls, data_dir=data_dir)
        if isinstance(probe, dict) and probe:
            probe.setdefault("windows", [])
            probe.setdefault("active", {"status": "unknown", "source": "none"})
            probe.setdefault("reason", probe.get("status", ""))
            return probe
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
            "active": {"status": "probe_error", "source": "exception", "hwnd": 0, "title": ""},
            "windows": [],
            "ignored_windows": [],
            "bindings": [],
            "ui_automation": {"available": False, "reason": "probe_error"},
            "raw_probe": {},
        }
    return {
        "status": "empty",
        "reason": "probe returned no structured payload",
        "active": {"status": "empty_probe", "source": "none", "hwnd": 0, "title": ""},
        "windows": [],
        "ignored_windows": [],
        "bindings": [],
        "ui_automation": {"available": False, "reason": "empty_probe"},
        "raw_probe": {},
    }


def delete_sidebar_channel(data_dir: str | Path, conversation_id: str) -> dict[str, Any]:
    channel_id = str(conversation_id or "").strip()
    if not channel_id:
        raise ValueError("conversation_id is required")
    store = _channel_store(data_dir)
    cleanup = store.delete_channel_with_cleanup(channel_id)
    return {
        "status": "ok",
        "deleted_count": 1 if cleanup["deleted"] else 0,
        "deleted_conversation_ids": [channel_id] if cleanup["deleted"] else [],
        "cleanup": cleanup,
        "note": _cleanup_note(cleanup),
    }


def cleanup_file_workspace(data_dir: str | Path, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Prune old per-file workspace dirs to bound disk growth (explicit, opt-in).

    The general artifact-cleanup report deliberately retains file_workspace as the
    isolated per-conversation middle layer, so this is a separate, operator-driven
    action with conservative defaults (keep newest 50, prune older than 30 days).
    """
    payload = payload if isinstance(payload, dict) else {}
    max_age_days = payload.get("max_age_days", 30)
    keep_min = int(payload.get("keep_min", 50) or 0)
    max_total_mb = payload.get("max_total_mb")
    max_age_seconds = float(max_age_days) * 86400.0 if max_age_days not in (None, "") else None
    max_total_bytes = int(float(max_total_mb) * 1024 * 1024) if max_total_mb not in (None, "") else None
    workspace = FileWorkspace(Path(data_dir) / "file_workspace")
    result = workspace.cleanup(
        max_age_seconds=max_age_seconds,
        max_total_bytes=max_total_bytes,
        keep_min=keep_min,
    )
    return {**result, "keep_min": keep_min, "max_age_days": max_age_days}


def cleanup_sidebar_channels(data_dir: str | Path, *, hidden_only: bool = True) -> dict[str, Any]:
    if not hidden_only:
        raise ValueError("only hidden channel cleanup is supported")
    state = _channel_state(data_dir)
    hidden_items = state.get("hidden_items_all", [])
    store = _channel_store(data_dir)
    deleted: list[str] = []
    cleanups: list[dict[str, Any]] = []
    for item in hidden_items if isinstance(hidden_items, list) else []:
        conversation_id = str(item.get("conversation_id", "")).strip() if isinstance(item, dict) else ""
        if not conversation_id:
            continue
        cleanup = store.delete_channel_with_cleanup(conversation_id)
        if cleanup["deleted"]:
            deleted.append(conversation_id)
            cleanups.append({"conversation_id": conversation_id, **cleanup})
    return {
        "status": "ok",
        "deleted_count": len(deleted),
        "deleted_conversation_ids": deleted,
        "cleanups": cleanups,
        "note": "微信可信通道仅删除注册；非微信来源通道会同步清除 ledger、file_workspace、session",
    }


def update_sidebar_controls(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    mode = payload.get("mode")
    enabled = _payload_bool(payload, "send_enabled")
    driver = payload.get("send_driver")
    confirm_required = _payload_bool(payload, "send_confirm_required")
    max_chars = payload.get("send_max_chars")
    min_interval_seconds = payload.get("send_min_interval_seconds")
    controls = set_send_controls(
        data_dir,
        mode=str(mode) if mode is not None else None,
        enabled=enabled,
        driver=str(driver) if driver is not None else None,
        confirm_required=confirm_required,
        max_chars=int(max_chars) if max_chars is not None else None,
        min_interval_seconds=int(min_interval_seconds) if min_interval_seconds is not None else None,
    )
    runtime_config = _update_runtime_modes_from_payload(data_dir, payload)
    return {"status": "ok", "send_controls": controls, "runtime_modes": runtime_config, "runtime_config": runtime_config}


def _payload_bool(payload: dict[str, Any], key: str) -> bool | None:
    if key not in payload or payload.get(key) is None:
        return None
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off", ""}:
        return False
    raise ValueError(f"{key} must be a boolean")


def sidebar_runtime_probe(data_dir: str | Path, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Probe OCR/ASR runtime using the same engine constructors as ingestion."""

    payload = payload if isinstance(payload, dict) else {}
    config = ensure_config(data_dir)
    ocr_mode = _normalize_runtime_mode(payload.get("ocr_mode", config.ocr_mode))
    asr_mode = _normalize_runtime_mode(payload.get("asr_mode", config.asr_mode))
    result: dict[str, Any] = {
        "status": "ok",
        "same_path_as_ingest": True,
        "ingest_path": "文件入库与探测使用同一路径；auto/cpu 走轻量 CPU，只有 gpu 档进入全局 GPU 队列。",
        "config_modes": {"ocr_mode": config.ocr_mode, "asr_mode": config.asr_mode},
        "effective_modes": {"ocr_mode": ocr_mode, "asr_mode": asr_mode},
    }
    errors: list[str] = []
    try:
        ocr_engine = build_default_ocr_engine(mode=ocr_mode)
        ocr_health = ocr_engine.health()
        result["ocr"] = {
            "engine_class": type(ocr_engine).__name__,
            "python_executable": str(getattr(ocr_engine, "python_executable", "")),
            "health": _dataclass_payload(ocr_health),
        }
        if bool(payload.get("run_sample") or payload.get("live_probe")):
            result["ocr"]["sample"] = _probe_ocr_worker_sample(ocr_engine)
    except Exception as exc:
        errors.append(f"ocr:{type(exc).__name__}: {exc}")
        result["ocr"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "mode": ocr_mode}
    try:
        asr_engine = LocalAsrSubprocessEngine(mode=asr_mode)
        asr_health = asr_engine.health()
        result["asr"] = {
            "engine_class": type(asr_engine).__name__,
            "python_executable": str(getattr(asr_engine, "python_executable", "")),
            "model": str(getattr(asr_engine, "model", "")),
            "health": _dataclass_payload(asr_health),
        }
        if bool(payload.get("run_sample") or payload.get("live_probe")):
            result["asr"]["sample"] = _probe_asr_worker_sample(asr_engine)
    except Exception as exc:
        errors.append(f"asr:{type(exc).__name__}: {exc}")
        result["asr"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "mode": asr_mode}
    ocr_sample = result.get("ocr", {}).get("sample") if isinstance(result.get("ocr"), dict) else {}
    ocr_sample_metadata = ocr_sample.get("metadata") if isinstance(ocr_sample, dict) and isinstance(ocr_sample.get("metadata"), dict) else {}
    ocr_sample_ran = isinstance(ocr_sample, dict) and str(ocr_sample.get("status") or "") == "ok"
    asr_sample = result.get("asr", {}).get("sample") if isinstance(result.get("asr"), dict) else {}
    asr_sample_metadata = asr_sample.get("metadata") if isinstance(asr_sample, dict) and isinstance(asr_sample.get("metadata"), dict) else {}
    asr_sample_ran = isinstance(asr_sample, dict) and str(asr_sample.get("status") or "") == "ok"
    result["gpu"] = {
        "ocr_available": bool(result.get("ocr", {}).get("health", {}).get("gpu_available")),
        "ocr_enabled": bool(ocr_sample_metadata.get("gpu_used")) if ocr_sample_ran else bool(result.get("ocr", {}).get("health", {}).get("gpu_used")),
        "ocr_worker_checked": ocr_sample_ran,
        "ocr_worker_backends": list(ocr_sample_metadata.get("backends") or []) if isinstance(ocr_sample_metadata.get("backends"), list) else [],
        "ocr_required": ocr_mode == "gpu",
        "asr_available": bool(result.get("asr", {}).get("health", {}).get("gpu_available")),
        "asr_enabled": bool(asr_sample_metadata.get("gpu_used")) if asr_sample_ran else bool(result.get("asr", {}).get("health", {}).get("gpu_used")),
        "asr_worker_checked": asr_sample_ran,
        "asr_worker_backend": str(asr_sample_metadata.get("backend") or ""),
        "asr_required": asr_mode == "gpu",
    }
    result["gpu_gate"] = gpu_gate_snapshot()
    if errors:
        result["status"] = "partial_error"
        result["errors"] = errors
    return result


def _probe_ocr_worker_sample(ocr_engine: Any) -> dict[str, Any]:
    """Run a tiny image through the exact OCR worker path used by ingestion."""

    sample_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xfc\xff\xff?"
        b"\x00\x05\xfe\x02\xfeA\xde\xfc\x82\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="chatbot_ocr_probe_") as tmp:
            image = Path(tmp) / "gpu_probe.png"
            image.write_bytes(sample_png)
            read_structured = getattr(ocr_engine, "read_structured", None)
            if not callable(read_structured):
                return {"status": "skipped", "reason": "ocr_engine_has_no_read_structured"}
            ocr_result = read_structured(image)
            metadata = dict(getattr(ocr_result, "metadata", {}) or {})
            return {
                "status": "ok",
                "text_length": len(str(getattr(ocr_result, "text", "") or "")),
                "item_count": int(getattr(ocr_result, "item_count", 0) or 0),
                "metadata": metadata,
            }
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _probe_asr_worker_sample(asr_engine: Any) -> dict[str, Any]:
    """Run a tiny WAV through the exact ASR worker path used by ingestion."""

    try:
        with tempfile.TemporaryDirectory(prefix="chatbot_asr_probe_") as tmp:
            audio = Path(tmp) / "gpu_probe.wav"
            _write_silence_wav(audio, seconds=0.35, sample_rate=16000)
            transcribe = getattr(asr_engine, "transcribe", None)
            if not callable(transcribe):
                return {"status": "skipped", "reason": "asr_engine_has_no_transcribe"}
            transcript = transcribe(audio)
            backend = str(getattr(transcript, "backend", "") or "")
            status = str(getattr(transcript, "status", "") or "")
            worker_ok = status in {"transcribed", "empty"}
            return {
                "status": "ok" if worker_ok else "error",
                "transcript_status": status,
                "text_length": len(str(getattr(transcript, "text", "") or "")),
                "error": str(getattr(transcript, "error", "") or ""),
                "metadata": {
                    "backend": backend,
                    "model": str(getattr(transcript, "model", "") or ""),
                    "language": str(getattr(transcript, "language", "") or ""),
                    "gpu_used": backend.endswith("_gpu") or backend.endswith(":gpu") or "_gpu" in backend,
                },
            }
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _write_silence_wav(path: Path, *, seconds: float, sample_rate: int) -> None:
    frame_count = max(1, int(seconds * sample_rate))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frame_count)


def _update_runtime_modes_from_payload(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    config = load_config(data_dir)
    changed = False
    if "ocr_mode" in payload:
        ocr_mode = _normalize_runtime_mode(payload.get("ocr_mode"))
        if config.ocr_mode != ocr_mode:
            config.ocr_mode = ocr_mode
            changed = True
    if "asr_mode" in payload:
        asr_mode = _normalize_runtime_mode(payload.get("asr_mode"))
        if config.asr_mode != asr_mode:
            config.asr_mode = asr_mode
            changed = True
    file_max_bytes = _file_max_bytes_from_payload(payload)
    if file_max_bytes is not None and config.file_max_bytes != file_max_bytes:
        config.file_max_bytes = file_max_bytes
        changed = True
    if changed:
        save_config(config)
    return {"ocr_mode": config.ocr_mode, "asr_mode": config.asr_mode, "file_max_bytes": config.file_max_bytes}


def _file_max_bytes_from_payload(payload: dict[str, Any]) -> int | None:
    raw = payload.get("file_max_bytes")
    if raw is None:
        raw = payload.get("file_max_mb")
        if raw is not None:
            try:
                raw = float(raw) * 1024 * 1024
            except (TypeError, ValueError):
                return None
    if raw is None:
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    return max(1024, min(value, 2 * 1024 * 1024 * 1024))


def _normalize_runtime_mode(value: Any) -> str:
    mode = str(value or "auto").strip().lower()
    if mode in {"gpu", "cuda", "gpu-only", "gpu_only"}:
        return "gpu"
    if mode in {"cpu", "cpu-only", "cpu_only", "rapidocr"}:
        return "cpu"
    return "auto"


def _dataclass_payload(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return {"value": str(value)}


def append_sidebar_backend_event(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    event_file = _backend_event_file_path(data_dir, payload)
    raw_id = append_backend_event_payload(event_file, payload)
    return {
        "status": "ok",
        "event_file": str(event_file),
        "raw_id": raw_id,
        "capture_source": "backend_http_ingest",
        "will_write_ledger": True,
        "send_enabled": False,
    }


def sidebar_agent_tick(data_dir: str | Path = "data", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one bounded dialog-agent tick over the backend event bus."""

    payload = payload if isinstance(payload, dict) else {}
    root = Path(data_dir).resolve()
    config = ensure_config(root)
    runtime = build_runtime(config)
    event_file = _backend_event_file_path(root, payload)
    event_file.parent.mkdir(parents=True, exist_ok=True)
    event_file.touch(exist_ok=True)
    loops = _bounded_int(payload.get("loops"), 1, 1, 20)
    requested_conversations = _string_list(payload.get("conversation_ids") or payload.get("conversationIds") or [])
    job_id = f"agent-tick-{uuid4().hex[:12]}"
    task_id = job_id
    store = TaskStatusStore(root)
    store.create(
        {
            "task_id": task_id,
            "title": "运行对话 Agent",
            "kind": "Agent",
            "status": "queued",
            "priority": 90,
            "progress": 5,
            "phase": "等待读取会话文件",
            "detail": str(event_file),
            "concurrency_key": "agent:tick",
            "resource_class": "llm_interactive",
            "estimated_cost": 2,
            "external_id": job_id,
            "metadata": {
                "event_file": str(event_file),
                "scope_label": "对话 Agent",
                "loops": loops,
            },
        }
    )
    store.transition(task_id, "start", {"progress": 15, "phase": "正在读取当前 session 对话文件"})
    snapshot_before = _agent_session_snapshot(root, runtime=runtime, conversation_ids=requested_conversations)
    result: dict[str, Any]
    processed_conversations: list[str] = []
    try:
        driver = _build_agent_backend_driver(
            config,
            runtime,
            event_file,
            extra_roots=_string_list(payload.get("extra_roots") or payload.get("extraRoots") or []),
        )
        runtime.active_driver = driver
        store.update(
            task_id,
            {
                "progress": 35,
                "phase": "正在运行消息聚合与接话管线",
                "detail": f"会话快照 {snapshot_before.get('conversation_count', 0)} 个",
            },
        )
        result = PollingRunner(
            runtime,
            driver,
            poll_interval_seconds=0,
            workload="interactive",
        ).run_forever(max_loops=loops)
        processed_conversations = _agent_processed_conversation_ids(result)
        snapshot_after = _agent_session_snapshot(
            root,
            runtime=runtime,
            conversation_ids=_dedupe_strings([*requested_conversations, *processed_conversations]),
        )
        processed_count = int(result.get("processed_count") or 0)
        store.transition(
            task_id,
            "complete",
            {
                "progress": 100,
                "phase": "一次接话管线已完成",
                "detail": f"处理 {processed_count} 条消息；聚合 {snapshot_after.get('conversation_count', 0)} 个通道",
                "actual_cost": max(1, processed_count),
            },
        )
        status = "ok"
        error = ""
    except Exception as exc:
        result = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "processed_count": 0, "processed": []}
        snapshot_after = _agent_session_snapshot(root, runtime=runtime, conversation_ids=requested_conversations)
        store.transition(
            task_id,
            "fail",
            {
                "progress": 100,
                "phase": "对话 Agent 运行失败",
                "detail": str(exc),
                "last_error": str(exc),
            },
        )
        status = "error"
        error = str(exc)
    channels = _channel_state(root)
    task_manager = build_sidebar_task_manager(root)
    queues = {queue_status: list_confirm_queue(root, status=queue_status) for queue_status in QUEUE_STATUSES}
    response = {
        "status": status,
        "agent": {
            "schema": "dialog_agent_tick_v1",
            "job_id": job_id,
            "task_id": task_id,
            "event_file": str(event_file),
            "loops": loops,
            "processed_count": int(result.get("processed_count") or 0),
            "processed": result.get("processed", []),
            "runner_status": result.get("status", ""),
            "processed_conversation_ids": processed_conversations,
            "policy": "read_session_snapshot_then_poll_backend_events_then_reply_gate",
        },
        "session_snapshot": {
            "before": snapshot_before,
            "after": snapshot_after,
        },
        "task_manager": task_manager,
        "channels": channels,
        "queues": queues,
    }
    if error:
        response["error"] = error
    return response


def build_sidebar_bridge_state(data_dir: str | Path = "data") -> dict[str, Any]:
    return _sidebar_bridge_state(data_dir, limit=50)


def clear_sidebar_send_audit(data_dir: str | Path) -> dict[str, Any]:
    return clear_send_audit(data_dir)


def clear_sidebar_history_data(data_dir: str | Path, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Clear disposable conversation/runtime history while preserving config."""

    payload = payload if isinstance(payload, dict) else {}
    root = Path(data_dir).resolve()
    ensure_config(root)
    if bool(
        payload.get("shutdown_processes")
        or payload.get("shutdownProcesses")
    ):
        return _schedule_sidebar_history_reset_shutdown(root, payload)
    removed: list[dict[str, Any]] = []
    retained_locked: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    retained = _retained_config_paths(root)
    for relative in _HISTORY_RESET_DIRS:
        _remove_history_path(root, relative, removed, retained_locked, errors, retained)
    for relative in _HISTORY_RESET_FILES:
        _remove_history_path(root, relative, removed, retained_locked, errors, retained)
    reinitialized = _reinitialize_history_runtime_files(root)
    _write_weflow_sidebar_state(
        root,
        {
            "last_health": {},
            "last_discover": {},
            "last_pull": {},
            "last_backfill": {},
            "pull_job": {},
            "backfill_job": {},
            "operation_history": [],
            "last_error": "",
        },
    )
    return {
        "status": "partial_error" if errors else "ok",
        "policy": "history_only_preserve_sidebar_config",
        "removed_count": len(removed),
        "removed": removed,
        "retained_locked_count": len(retained_locked),
        "retained_locked": retained_locked,
        "error_count": len(errors),
        "errors": errors,
        "retained_config": [str(item) for item in sorted(retained)],
        "reinitialized": reinitialized,
    }


def build_sidebar_weflow_state(data_dir: str | Path = "data") -> dict[str, Any]:
    root = Path(data_dir)
    persisted = _read_json(root / "weflow_sidebar_state.json", {})
    worker = _weflow_worker_state(root)
    token_env = str(persisted.get("token_env") or "WEFLOW_API_TOKEN") if isinstance(persisted, dict) else "WEFLOW_API_TOKEN"
    token_present = bool(persisted.get("token_present")) if isinstance(persisted, dict) else False
    env_token_present = bool(_env_value(token_env) or _env_value("WEFLOW_API_TOKEN"))
    if env_token_present:
        token_present = True
    token_source = "environment" if env_token_present else ("payload_or_state" if token_present else "missing")
    cached_sessions = _weflow_cached_sessions(root, limit=200)
    readiness = _weflow_readiness_snapshot(persisted if isinstance(persisted, dict) else {}, worker, token_present, token_source)
    try:
        migration = migrate_file_allowed_extensions(root)
    except Exception as exc:
        migration = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    pull_job = _weflow_pull_job_state(root, persisted)
    backfill_job = _weflow_backfill_job_state(root, persisted)
    last_pull = persisted.get("last_pull", {}) if isinstance(persisted, dict) else {}
    if isinstance(pull_job.get("result"), dict) and pull_job.get("result"):
        last_pull = pull_job["result"]
    last_backfill = persisted.get("last_backfill", {}) if isinstance(persisted, dict) else {}
    if isinstance(backfill_job.get("result"), dict) and backfill_job.get("result"):
        last_backfill = backfill_job["result"]
    return {
        "status": "ok",
        "base_url": str(persisted.get("base_url") or "http://127.0.0.1:5031"),
        "token_env": token_env,
        "token_present": token_present,
        "token_source": token_source,
        "hook_event_file": str(root / "hook_events.jsonl"),
        "backend_event_file": str(root / "backend_events.jsonl"),
        "weflow_state_file": str(root / "weflow_bridge_state.json"),
        "security": {
            "primary_source": "weflow_local_fork",
            "requires_token_for_pull": True,
            "requires_local_fork_marker": True,
            "allows_non_local_by_default": False,
            "wechatferry_primary": False,
        },
        "config_migration": migration,
        "worker": worker,
        "readiness": readiness,
        "pull_job": pull_job,
        "backfill_job": backfill_job,
        "bridge_state": summarize_weflow_bridge_state(root / "weflow_bridge_state.json"),
        "last_health": persisted.get("last_health", {}) if isinstance(persisted, dict) else {},
        "last_discover": persisted.get("last_discover", {}) if isinstance(persisted, dict) else {},
        "discovered_sessions": {
            "status": "ok",
            "source": "weflow_session_store",
            "count": len(cached_sessions),
            "sessions": cached_sessions,
        },
        "last_pull": last_pull,
        "last_backfill": last_backfill,
        "operation_history": persisted.get("operation_history", []) if isinstance(persisted, dict) else [],
        "last_error": persisted.get("last_error", "") if isinstance(persisted, dict) else "",
        "updated_at": persisted.get("updated_at", "") if isinstance(persisted, dict) else "",
    }


def sidebar_weflow_health(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    params = _weflow_params(data_dir, payload)
    result = weflow_health_status(
        params["base_url"],
        token=params["token"],
        allow_non_local=params["allow_non_local"],
        require_token=False,
        require_fork=True,
    )
    result = {**result, "token_source": params["token_source"]}
    _record_weflow_state_safely(data_dir, {"last_health": result, **_weflow_public_params(params)}, action="health", result=result)
    return result


def sidebar_weflow_discover_sessions(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """List available WeFlow sessions (conversations) for the user to pick from.

    Returns {status, sessions: [{id, name, unread_count?, last_message_time?}]}.
    """
    params = _weflow_params(data_dir, payload)
    limit = _bounded_int(payload.get("limit"), 100, 1, 1000)
    try:
        weflow_ready = require_weflow_ready(
            params["base_url"],
            token=params["token"],
            allow_non_local=params["allow_non_local"],
        )
        bridge = WeFlowHttpBridge(
            params["base_url"],
            token=params["token"],
            hook_event_file=params["hook_event_file"],
            state_path=params["weflow_state_file"],
            allow_non_local=params["allow_non_local"],
        )
        sessions = [
            session
            for session in (_normalize_weflow_session(item) for item in bridge.list_sessions(limit=limit))
            if session.get("id") and not is_system_account(session.get("id"))
        ]
        registration = _register_weflow_sessions(data_dir, sessions)
        session_store = _upsert_weflow_sessions(data_dir, sessions, source="weflow_live", registration=registration)
        result = {
            "status": "ok",
            "source": "weflow_live",
            "sessions": sessions,
            "count": len(sessions),
            **registration,
            "session_store": session_store,
            "weflow_ready": weflow_ready,
        }
        _record_weflow_state_safely(
            data_dir,
            {"last_discover": result, "last_error": "", **_weflow_public_params(params)},
            action="discover-sessions",
            result=result,
        )
        return result
    except Exception as exc:
        cached_sessions = _weflow_cached_sessions(data_dir, limit=limit)
        if cached_sessions:
            result = {
                "status": "ok",
                "source": "weflow_session_store_cache",
                "live_status": "error",
                "live_error": f"{type(exc).__name__}: {exc}",
                "message": "实时发现失败，已返回本地通道库中的 WeFlow 会话",
                "sessions": cached_sessions,
                "count": len(cached_sessions),
                "registered_count": len(cached_sessions),
            }
            _record_weflow_state_safely(
                data_dir,
                {
                    "last_discover": result,
                    "last_error": f"{type(exc).__name__}: {exc}",
                    **_weflow_public_params(params),
                },
                action="discover-sessions-cache",
                result=result,
            )
            return result
        result = {
            "status": "error",
            "message": str(exc),
            "type": type(exc).__name__,
            "sessions": [],
        }
        _record_weflow_state_safely(
            data_dir,
            {"last_discover": result, "last_error": f"{type(exc).__name__}: {exc}", **_weflow_public_params(params)},
            action="discover-sessions",
            result=result,
        )
        return result


def sidebar_weflow_pull_once(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    if bool(payload.get("background") or payload.get("async")):
        return _start_weflow_pull_job(data_dir, payload)
    result = _run_sidebar_weflow_once(data_dir, payload)
    params = _weflow_params(data_dir, payload)
    result["session_store"] = _register_weflow_result_sessions(data_dir, payload, result)
    _record_weflow_state_safely(data_dir, {"last_pull": result, **_weflow_public_params(params)}, action="pull-once", result=result)
    return result


def _start_weflow_pull_job(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    key = str(root)
    with _WEFLOW_LOCK:
        existing = _WEFLOW_PULL_JOBS.get(key)
        if existing and _thread_alive(existing.get("thread")):
            result = {
                "status": "running",
                "message": "WeFlow pull is already running in background",
                "pull_job": _public_pull_job(existing),
            }
            _append_weflow_operation_history(data_dir, "pull-once-already-running", result)
            return result
        job_id = f"pull-{uuid4().hex[:12]}"
        job: dict[str, Any] = {
            "job_id": job_id,
            "status": "running",
            "talkers": _string_list(payload.get("talkers") or payload.get("talker") or []),
            "started_at": time.time(),
            "updated_at": time.time(),
            "progress": {
                "session_count": 0,
                "page_count": 0,
                "scanned_count": 0,
                "appended_count": 0,
                "processed_count": 0,
                "current_session_id": "",
                "last_raw_id": "",
            },
            "result": {},
            "last_error": "",
        }
        thread = threading.Thread(
            target=_weflow_pull_job_loop,
            args=(root, dict(payload), job_id),
            name="sidebar-weflow-pull-once",
            daemon=True,
        )
        job["thread"] = thread
        _WEFLOW_PULL_JOBS[key] = job
        thread.start()
        job_snapshot = _public_pull_job(job)
        result = {
            "status": "started",
            "message": "WeFlow pull started in background",
            "pull_job": job_snapshot,
        }
        _record_weflow_state_safely(
            data_dir,
            {"last_pull": result, "pull_job": job_snapshot, **_weflow_public_params(_weflow_params(data_dir, payload))},
            action="pull-once-start",
            result=result,
        )
        return result


def _backfill_payload(payload: dict[str, Any], talkers: list[str]) -> dict[str, Any]:
    """Normalize a caller payload into the forced backfill shape.

    since=0 -> the bridge marks messages context_only (recorded but never
    replied to); max_pages=0 walks every page so the whole history is captured.
    Shared by the async job and the synchronous CLI path so both behave identically.
    """

    return {
        **payload,
        "talkers": talkers,
        "since": 0,
        "context_only": True,
        "force_context_only": True,
        "max_pages": _bounded_int(payload.get("max_pages"), 0, 0, 10000),
        "max_messages": _bounded_int(payload.get("max_messages"), 0, 0, 1000000),
    }


def run_weflow_backfill_sync(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Run a history backfill synchronously and return the final result.

    The sidebar UI uses the async :func:`sidebar_weflow_backfill` (returns
    immediately with a job it polls). A short-lived CLI process cannot poll a
    daemon thread — it would exit before the thread ran — so the CLI calls this
    blocking variant instead. Same forced since=0 / context_only / all-pages
    semantics and system-account filtering.
    """

    talkers = _string_list(payload.get("talkers") or payload.get("talker") or [])
    talkers = [talker for talker in talkers if not is_system_account(talker)]
    if not talkers:
        return {
            "status": "error",
            "error": "backfill requires an explicit talkers list (non-system account)",
            "backfilled_talkers": [],
        }
    backfill_payload = _backfill_payload(payload, talkers)
    result = _run_sidebar_weflow_once(data_dir, backfill_payload)
    result = {**result, "backfill": True, "backfilled_talkers": talkers}
    result["session_store"] = _register_weflow_result_sessions(data_dir, backfill_payload, result)
    _record_weflow_state_safely(
        data_dir,
        {
            "last_backfill": result,
            "last_error": "" if result.get("status") != "error" else str(result.get("error") or ""),
            **_weflow_public_params(_weflow_params(data_dir, backfill_payload)),
        },
        action="backfill",
        result=result,
    )
    return result


def sidebar_weflow_backfill(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Backfill a conversation's history so a newly added talker starts with
    context instead of an empty ledger.

    Pulls from the beginning of the conversation (``since=0``) as context-only
    messages (recorded in the ledger but never replied to), walking every page
    up to an optional ``max_messages`` cap. This lets the agent join a chat that
    already has a long history without the user re-stating anything.

    Requires an explicit ``talkers`` list: backfill targets a specific
    conversation the user is adding, not every discovered session. The bridge
    dedup + max(previous_since, since) state merge make this safe to run even
    after incremental pulls have advanced the cursor.
    """

    talkers = _string_list(payload.get("talkers") or payload.get("talker") or [])
    talkers = [talker for talker in talkers if not is_system_account(talker)]
    if not talkers:
        return {
            "status": "error",
            "error": "backfill requires an explicit talkers list (non-system account)",
            "backfilled_talkers": [],
        }
    root = Path(data_dir).resolve()
    key = str(root)
    with _WEFLOW_LOCK:
        existing = _WEFLOW_BACKFILL_JOBS.get(key)
        if existing and _thread_alive(existing.get("thread")):
            result = {
                "status": "running",
                "message": "WeFlow backfill is already running",
                "backfill_job": _public_backfill_job(existing),
                "backfilled_talkers": existing.get("talkers", []),
            }
            _append_weflow_operation_history(data_dir, "backfill-already-running", result)
            return result

        stop_event = threading.Event()
        job_id = f"backfill-{uuid4().hex[:12]}"
        backfill_payload = _backfill_payload(payload, talkers)
        job: dict[str, Any] = {
            "job_id": job_id,
            "status": "running",
            "talkers": talkers,
            "started_at": time.time(),
            "updated_at": time.time(),
            "cancel_requested": False,
            "progress": {
                "session_count": len(talkers),
                "page_count": 0,
                "scanned_count": 0,
                "appended_count": 0,
                "processed_count": 0,
                "current_session_id": "",
                "last_raw_id": "",
            },
            "result": {},
            "last_error": "",
            "stop": stop_event,
        }
        thread = threading.Thread(
            target=_weflow_backfill_job_loop,
            args=(root, backfill_payload, job_id, stop_event),
            name="sidebar-weflow-backfill",
            daemon=True,
        )
        job["thread"] = thread
        _WEFLOW_BACKFILL_JOBS[key] = job
        thread.start()
        job_snapshot = _public_backfill_job(job)
        result = {
            "status": "started",
            "message": "WeFlow history backfill started",
            "backfill_job": job_snapshot,
            "backfilled_talkers": talkers,
        }
        _record_weflow_state_safely(
            data_dir,
            {"last_backfill": result, "backfill_job": result["backfill_job"], **_weflow_public_params(_weflow_params(data_dir, backfill_payload))},
            action="backfill-start",
            result=result,
        )
        return result


def sidebar_weflow_cancel_backfill(data_dir: str | Path, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    key = str(root)
    job_id = str((payload or {}).get("job_id") or (payload or {}).get("jobId") or "").strip()
    with _WEFLOW_LOCK:
        job = _WEFLOW_BACKFILL_JOBS.get(key)
        if job_id and job and str(job.get("job_id")) != job_id:
            job = None
        if not job:
            result = {"status": "idle", "message": "No active WeFlow backfill job", "backfill_job": _weflow_backfill_job_state(root)}
            _append_weflow_operation_history(data_dir, "backfill-cancel", result)
            return result
        stop = job.get("stop")
        if isinstance(stop, threading.Event):
            stop.set()
        job["cancel_requested"] = True
        job["status"] = "cancel_requested" if _thread_alive(job.get("thread")) else str(job.get("status") or "stopped")
        job["updated_at"] = time.time()
        snapshot = _public_backfill_job(job)
    _write_weflow_sidebar_state(data_dir, {"backfill_job": snapshot})
    result = {"status": "cancel_requested", "message": "WeFlow backfill cancel signal sent", "backfill_job": snapshot}
    _append_weflow_operation_history(data_dir, "backfill-cancel", result)
    return result


def sidebar_weflow_start(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    key = str(root)
    with _WEFLOW_LOCK:
        existing = _WEFLOW_WORKERS.get(key)
        if existing and existing.get("thread") and existing["thread"].is_alive():
            _start_bridge_worker(root, dict(payload))
            result = {"status": "ok", "worker": _weflow_worker_state(root), "message": "WeFlow 后台拉取已经在运行"}
            _append_weflow_operation_history(data_dir, "start", result)
            return result
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_weflow_background_loop,
            args=(root, dict(payload), stop_event),
            name="sidebar-weflow-pull",
            daemon=True,
        )
        _WEFLOW_WORKERS[key] = {
            "thread": thread,
            "stop": stop_event,
            "started_at": time.time(),
            "loops": 0,
            "metrics": WeflowWorkerMetrics(),
        }
        thread.start()
    # Start the send-bridge delivery worker alongside the pull worker: the
    # pull->reply->deliver chain needs both halves running. No-op unless the
    # active send driver is bridge_outbox.
    _start_bridge_worker(root, dict(payload))
    result = {"status": "ok", "worker": _weflow_worker_state(root), "message": "WeFlow 后台拉取已启动"}
    _append_weflow_operation_history(data_dir, "start", result)
    return result


def sidebar_weflow_stop(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    key = str(root)
    thread: threading.Thread | None = None
    with _WEFLOW_LOCK:
        worker = _WEFLOW_WORKERS.get(key)
        if worker and worker.get("stop"):
            worker["stop"].set()
            worker["stop_requested"] = True
            thread = worker.get("thread") if isinstance(worker.get("thread"), threading.Thread) else None
    if thread is not None:
        thread.join(timeout=1.0)
    # Stop the send-bridge worker together with the pull worker.
    _stop_bridge_worker(root)
    worker_state = _weflow_worker_state(root)
    finished_tasks = _finish_weflow_worker_tasks(root, running=bool(worker_state.get("running")))
    result = {
        "status": "ok",
        "worker": worker_state,
        "finished_tasks": finished_tasks,
        "task_manager": build_sidebar_task_manager(root),
        "message": "WeFlow 后台拉取已停止" if not worker_state.get("running") else "WeFlow 后台拉取停止信号已发送，当前 tick 会先收尾",
    }
    _append_weflow_operation_history(data_dir, "stop", result)
    return result


def _finish_weflow_worker_tasks(root: Path, *, running: bool) -> list[dict[str, Any]]:
    status = "cancelled" if running else "completed"
    phase = "停止信号已发送" if running else "后台拉取已停止"
    return TaskStatusStore(root).finish_external(
        "worker",
        {
            "status": status,
            "progress": 100,
            "phase": phase,
            "detail": "user_requested_stop",
            "actual_cost": 1,
        },
    )


def sidebar_weflow_clear_history(data_dir: str | Path, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    _write_weflow_sidebar_state(data_dir, {"operation_history": []})
    return {"status": "ok", "operation_history": []}


def sidebar_weflow_dependency_status(data_dir: str | Path = "data", *, record_history: bool = True) -> dict[str, Any]:
    with _weflow_exclusive_operation(data_dir, label="weflow_dependency_status"):
        result = {**_weflow_dependency_status_snapshot(), "exclusive_operation": True}
    if record_history:
        _append_weflow_operation_history(data_dir, "dependencies", result)
    return result


def _subprocess_module_available(python_executable: Path, module: str) -> bool:
    if not python_executable.exists():
        return False
    try:
        completed = subprocess.run(
            [str(python_executable), "-c", f"import {module}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def sidebar_weflow_install_deps(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    if not bool(payload.get("confirm_install", False)):
        raise ValueError("confirm_install=true is required")
    requested_groups = set(_string_list(payload.get("groups") or payload.get("group") or []))
    with _weflow_exclusive_operation(data_dir, label="weflow_install_deps", wait_timeout_seconds=1200.0):
        before = _weflow_dependency_status_snapshot()
        missing_groups = {
            group["group"]
            for group in before.get("groups", [])
            if isinstance(group, dict) and not bool(group.get("available"))
        }
        target_groups = requested_groups or missing_groups
        install_results = []
        for spec in _dependency_specs():
            if spec["group"] not in target_groups:
                continue
            group_status = next((item for item in before.get("groups", []) if item.get("group") == spec["group"]), {})
            if group_status.get("available"):
                install_results.append({"group": spec["group"], "status": "skipped_available"})
                continue
            python_executable = _dependency_python(spec)
            if not _ensure_dependency_python(spec, python_executable):
                install_results.append(
                    {
                        "group": spec["group"],
                        "status": "runtime_missing",
                        "python": str(python_executable),
                    }
                )
                continue
            command = [str(python_executable), "-m", "pip", "install"]
            if spec.get("target"):
                command.extend(["--target", str(Path(spec["target"]))])
            command.extend(["-r", str(spec["requirements"].resolve())])
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=1800,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
            install_results.append(
                {
                    "group": spec["group"],
                    "status": "ok" if completed.returncode == 0 else "failed",
                    "returncode": completed.returncode,
                    "stdout_tail": completed.stdout[-4000:],
                    "stderr_tail": completed.stderr[-4000:],
                    "python": str(python_executable),
                    "requirements": str(spec["requirements"].resolve()),
                    "target": str(spec.get("target") or ""),
                }
            )
        after = _weflow_dependency_status_snapshot()
        result = {
            "status": "ok" if all(item.get("status") in {"ok", "skipped_available"} for item in install_results) else "failed",
            "install_results": install_results,
            "dependencies": after,
            "before": before,
            "exclusive_operation": True,
        }
    _append_weflow_operation_history(data_dir, "install-deps", result)
    return result


def _weflow_dependency_status_snapshot() -> dict[str, Any]:
    groups = []
    items = []
    duplicate_packages = _dependency_requirement_duplicates()
    for spec in _dependency_specs():
        python_executable = _dependency_python(spec)
        runtime_available = python_executable.exists()
        group_items = []
        for package, module in spec["modules"].items():
            available = _dependency_module_available(spec, python_executable, module)
            item = {
                "group": spec["group"],
                "package": package,
                "module": module,
                "runtime": spec["runtime"],
                "python": str(python_executable),
                "available": available,
            }
            group_items.append(item)
            items.append(item)
        groups.append(
            {
                "group": spec["group"],
                "runtime": spec["runtime"],
                "python": str(python_executable),
                "runtime_available": runtime_available,
                "available": bool(group_items) and all(item["available"] for item in group_items),
                "requirements": str(spec["requirements"].resolve()),
                "missing": [item["package"] for item in group_items if not item["available"]],
                "items": group_items,
            }
        )
    return {
        "status": "ok" if all(item["available"] for item in items) else "missing_optional",
        "groups": groups,
        "items": items,
        "requirements": {spec["group"]: str(spec["requirements"].resolve()) for spec in _dependency_specs()},
        "duplicate_requirements": duplicate_packages,
        "deduplicated": not duplicate_packages,
    }


def _dependency_specs() -> list[dict[str, Any]]:
    return [
        {
            "group": "document_runtime",
            "runtime": "main_python",
            "python": Path(sys.executable),
            "requirements": Path("requirements-document.txt"),
            "modules": {
                "PyMuPDF": "fitz",
                "pypdf": "pypdf",
                "pdfminer.six": "pdfminer",
                "openpyxl": "openpyxl",
            },
        },
        {
            "group": "ocr_runtime",
            "runtime": "vendor_ocr_python",
            "python": Path("vendor/ocr-python/Scripts/python.exe"),
            "venv": Path("vendor/ocr-python"),
            "requirements": Path("requirements-ocr-light.txt"),
            "modules": {
                "rapidocr-onnxruntime": "rapidocr_onnxruntime",
                "Pillow": "PIL",
                "numpy": "numpy",
                "opencv-python": "cv2",
            },
        },
        {
            "group": "asr_runtime",
            "runtime": "vendor_asr_python",
            "python": Path("vendor/asr-python/Scripts/python.exe"),
            "venv": Path("vendor/asr-python"),
            "requirements": Path("requirements-asr-light.txt"),
            "modules": {
                "faster-whisper": "faster_whisper",
                "soundfile": "soundfile",
            },
        },
        {
            "group": "windows_ui_runtime",
            "runtime": "vendor_windows_ui",
            "python": Path(sys.executable),
            "target": Path("vendor/windows-ui"),
            "requirements": Path("requirements-windows-ui.txt"),
            "modules": {"comtypes": "comtypes"},
        },
    ]


def _dependency_python(spec: dict[str, Any]) -> Path:
    return Path(spec.get("python") or sys.executable)


def _ensure_dependency_python(spec: dict[str, Any], python_executable: Path) -> bool:
    if python_executable.exists():
        return True
    venv = spec.get("venv")
    if not venv:
        return False
    completed = subprocess.run(
        [sys.executable, "-m", "venv", str(Path(venv))],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return completed.returncode == 0 and python_executable.exists()


def _dependency_module_available(spec: dict[str, Any], python_executable: Path, module: str) -> bool:
    if spec["runtime"] == "main_python":
        return find_spec(module) is not None
    target = spec.get("target")
    if target:
        try:
            completed = subprocess.run(
                [str(python_executable), "-c", f"import sys; sys.path.insert(0, {str(Path(target).resolve())!r}); import {module}"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0
    return _subprocess_module_available(python_executable, module)


def _dependency_requirement_duplicates() -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for spec in _dependency_specs():
        path = Path(spec["requirements"])
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            package = _normalize_requirement_name(line)
            if not package:
                continue
            seen.setdefault(package, []).append(spec["group"])
    return {package: groups for package, groups in seen.items() if len(groups) > 1}


def _normalize_requirement_name(line: str) -> str:
    value = line.strip()
    if not value or value.startswith("#") or value.startswith("-"):
        return ""
    match = re.split(r"[<>=~!;\[]", value, maxsplit=1)[0].strip().lower()
    return re.sub(r"[-_.]+", "-", match)


def build_sidebar_runtime_cards(data_dir: str | Path = "data") -> dict[str, Any]:
    return RuntimeCardStore(data_dir).state()


def sidebar_runtime_card_action(data_dir: str | Path, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = RuntimeCardStore(data_dir).apply_action(action, payload)
    return {"status": "ok", "runtime_cards": RuntimeCardStore(data_dir).state(), "result": result}


def ack_sidebar_bridge_item(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    bridge_id = str(payload.get("bridge_id") or payload.get("bridgeId") or "").strip()
    if not bridge_id:
        raise ValueError("bridge_id is required")
    status = str(payload.get("status", "")).strip()
    if not is_terminal_bridge_ack_status(status):
        raise ValueError("status must be sent, failed, or blocked")
    reason = str(payload.get("reason", "")).strip()
    external_message_id = str(payload.get("external_message_id") or payload.get("externalMessageId") or "").strip()
    extra = payload.get("payload")
    result = bridge_ack(
        data_dir,
        bridge_id,
        status=status,
        reason=reason,
        external_message_id=external_message_id,
        payload=extra if isinstance(extra, dict) else {},
    )
    effective_ack = result.get("effective_ack") if isinstance(result.get("effective_ack"), dict) else {}
    result["send_sync"] = sync_bridge_ack_to_send_state(
        data_dir,
        bridge_id,
        status=str(result.get("effective_status") or status),
        reason=str(effective_ack.get("reason", reason)),
        external_message_id=str(effective_ack.get("external_message_id", external_message_id)),
    )
    return result


def sidebar_queue_action(data_dir: str | Path, action: str, queue_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    reviewer = str(payload.get("reviewer", "sidebar"))
    note = str(payload.get("note", ""))
    if action == "approve":
        return approve_confirm_item(data_dir, queue_id, reviewer=reviewer, note=note)
    if action == "reject":
        return reject_confirm_item(data_dir, queue_id, reviewer=reviewer, note=note)
    if action in {"remove", "delete"}:
        return remove_confirm_item(data_dir, queue_id, reviewer=reviewer, note=note)
    if action == "send-approved":
        _start_bridge_worker(Path(data_dir).resolve(), dict(payload))
        return send_approved_confirm_item(data_dir, queue_id)
    raise ValueError(f"unknown queue action: {action}")


def _build_weflow_pull_context(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Build the reusable runtime/driver/bridge/runner for a WeFlow puller.

    These components are built once and reused across pull ticks so the backend
    driver keeps its in-memory dedup state (``_seen_event_ids`` /
    ``_seen_message_raw_ids``) alive. Rebuilding them every tick would make the
    driver re-read and re-process the entire ``backend_events.jsonl`` on each
    loop, which grows unbounded and re-triggers link fetches and memory
    maintenance for already-processed history.
    """

    params = _weflow_params(data_dir, payload)
    config = ensure_config(data_dir)
    runtime = build_runtime(config)
    weflow_ready = require_weflow_ready(
        params["base_url"],
        token=params["token"],
        allow_non_local=params["allow_non_local"],
    )
    media_roots = _weflow_media_roots(weflow_ready)
    roots = config.file_read_roots + config.wechat_voice_roots + params["extra_roots"] + media_roots
    driver = BackendEventJsonlDriver(
        params["backend_event_file"],
        runtime.file_index,
        allowed_input_roots=resolve_allowed_roots(config.data_dir, roots),
        allowed_extensions=config.file_allowed_extensions,
        max_input_bytes=config.file_max_bytes,
        attachment_parser=BackendAttachmentParser(
            build_default_ocr_engine(mode=config.ocr_mode),
            LocalAsrSubprocessEngine(mode=config.asr_mode),
        ),
        file_workspace=runtime.file_workspace,
        session_store=runtime.session_store,
        voice_cache_resolver=_voice_cache_resolver(config, extra_roots=params["extra_roots"] + media_roots),
    )
    runtime.active_driver = driver
    bridge = WeFlowHttpBridge(
        params["base_url"],
        token=params["token"],
        hook_event_file=params["hook_event_file"],
        state_path=params["weflow_state_file"],
        allow_non_local=params["allow_non_local"],
    )
    runner = HookMessagePullRunner(
        HookEventJsonlImporter(
            params["hook_event_file"],
            params["backend_event_file"],
            state_path=params["hook_state_file"],
        ),
        PollingRunner(
            runtime,
            driver,
            poll_interval_seconds=0,
            workload="background" if params["context_only"] else "interactive",
        ),
        hook_event_file=params["hook_event_file"],
        backend_event_file=params["backend_event_file"],
        # Serialize the consume step across processes (background worker tick,
        # ad-hoc pull-once, async backfill, and any CLI consumer) so they take
        # turns over the shared hook offset + deduper instead of overlapping.
        consume_lock_enabled=True,
    )
    return {
        "params": params,
        "bridge": bridge,
        "runner": runner,
        "runtime": runtime,
        "config_path": Path(config.data_dir) / "config.json",
        "config_mtime": _config_mtime(Path(config.data_dir)),
        "weflow_ready": weflow_ready,
        "media_roots": media_roots,
    }


def _config_mtime(data_dir: Path) -> float:
    try:
        return (Path(data_dir) / "config.json").stat().st_mtime
    except OSError:
        return 0.0


def _refresh_weflow_send_controls(context: dict[str, Any]) -> bool:
    """Pick up live send-control changes without tearing down the pull context.

    The pull driver's in-memory dedup state must survive across ticks, so we do
    NOT rebuild the whole context. Instead, when ``config.json`` changes on disk,
    we re-read it and refresh only the send side of the runtime (send backend
    driver + executor + reply-gate mode). ``GuardedSendExecutor`` reads config
    live off its ``config`` attribute, so swapping it is enough for
    ``send_enabled`` / ``send_driver`` / ``send_confirm_required`` changes to
    take effect on the next reply.
    """

    runtime = context.get("runtime")
    if runtime is None:
        return False
    data_dir = Path(getattr(runtime.config, "data_dir", context.get("config_path", Path("data")).parent))
    current_mtime = _config_mtime(data_dir)
    if current_mtime == context.get("config_mtime"):
        return False
    context["config_mtime"] = current_mtime
    try:
        new_config = ensure_config(data_dir)
    except Exception:
        return False
    runtime.config = new_config
    send_driver = build_send_driver(new_config)
    runtime.reply_gate.mode = new_config.mode
    runtime.reply_gate.auto_executor = GuardedSendExecutor(new_config, send_driver)
    return True


def _run_weflow_pull_tick(
    context: dict[str, Any],
    *,
    cancel_event: threading.Event | None = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    params = context["params"]
    bridge: WeFlowHttpBridge = context["bridge"]
    runner: HookMessagePullRunner = context["runner"]
    lock_root = Path(params.get("hook_state_file") or params.get("hook_event_file") or "data").parent
    with _weflow_exclusive_operation(lock_root, label="weflow_pull_tick"):
        _emit_weflow_progress(progress_callback, event="source_started", phase="拉取 WeFlow 消息页")
        source = bridge.pull_once(
            talkers=params["talkers"],
            session_limit=params["session_limit"],
            message_limit=params["message_limit"],
            max_pages=params["max_pages"],
            max_messages=params["max_messages"],
            since=params["since"],
            lookback_seconds=params["lookback_seconds"],
            workers=params["workers"],
            media=params["media"],
            context_only=params["context_only"],
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
        _emit_weflow_progress(
            progress_callback,
            event="source_completed",
            phase="WeFlow 消息页拉取完成",
            scanned_count=getattr(source, "scanned_count", 0),
            appended_count=getattr(source, "appended_count", 0),
        )
        if cancel_event is not None and cancel_event.is_set():
            pull = {
                "status": "cancelled",
                "hook_event_file": str(params["hook_event_file"]),
                "backend_event_file": str(params["backend_event_file"]),
                "processed_count": 0,
                "processed": [],
                "send_enabled": False,
            }
        else:
            # Single-instance the consume step so a concurrent worker tick / backfill
            # / pull-once never race the shared hook offset and message deduper.
            with _WEFLOW_CONSUMER_LOCK:
                _emit_weflow_progress(progress_callback, event="consume_started", phase="导入事件并运行 agent")
                pull = runner.run_once()
                _emit_weflow_progress(
                    progress_callback,
                    event="consume_completed",
                    phase="事件导入与 agent 处理完成",
                    processed_count=pull.get("processed_count", 0),
                )
        status = "ok" if source.status == "ok" and pull.get("status") == "ok" else "partial_error"
        if source.status == "cancelled" or (cancel_event is not None and cancel_event.is_set()):
            status = "cancelled"
        result = {
            "status": status,
            "base_url": bridge.base_url,
            "workers": params["workers"],
            "hook_event_file": str(params["hook_event_file"]),
            "backend_event_file": str(params["backend_event_file"]),
            "weflow_ready": context["weflow_ready"],
            "source": _jsonable(source),
            "pull": pull,
            "media_roots": context["media_roots"],
            "send_enabled": False,
        }
        recovery = _maybe_recover_empty_weflow_pull(
            context,
            result,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
        return recovery if recovery is not None else result


def _maybe_recover_empty_weflow_pull(
    context: dict[str, Any],
    result: dict[str, Any],
    *,
    cancel_event: threading.Event | None = None,
    progress_callback: Any = None,
) -> dict[str, Any] | None:
    """Bootstrap channels whose local derived state was cleared but source cursor remains advanced."""
    if cancel_event is not None and cancel_event.is_set():
        return None
    params = context["params"]
    if params.get("since") is not None or params.get("context_only"):
        return None
    source = result.get("source") if isinstance(result.get("source"), dict) else {}
    pull = result.get("pull") if isinstance(result.get("pull"), dict) else {}
    if (
        int(source.get("scanned_count", 0) or 0) > 0
        or int(source.get("appended_count", 0) or 0) > 0
        or int(pull.get("processed_count", 0) or 0) > 0
    ):
        return None
    if str(source.get("status") or "") != "ok" or str(pull.get("status") or "") != "ok":
        return None
    root = Path(params.get("hook_event_file") or params.get("backend_event_file") or "data").parent
    candidates = _weflow_bootstrap_candidates(root, params.get("talkers", []))
    if not candidates:
        return None

    bridge: WeFlowHttpBridge = context["bridge"]
    runner: HookMessagePullRunner = context["runner"]
    talkers = [item["talker"] for item in candidates]
    bootstrap_source = bridge.pull_once(
        talkers=talkers,
        session_limit=len(talkers),
        message_limit=params["message_limit"],
        max_pages=max(1, int(params.get("max_pages", 1) or 1)),
        max_messages=params["max_messages"],
        since=0,
        lookback_seconds=params["lookback_seconds"],
        workers=params["workers"],
        media=params["media"],
        context_only=True,
        ignore_seen=True,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
    )
    if cancel_event is not None and cancel_event.is_set():
        bootstrap_pull = {
            "status": "cancelled",
            "hook_event_file": str(params["hook_event_file"]),
            "backend_event_file": str(params["backend_event_file"]),
            "processed_count": 0,
            "processed": [],
            "send_enabled": False,
        }
    else:
        with _WEFLOW_CONSUMER_LOCK:
            bootstrap_pull = runner.run_once()
    recovered = int(bootstrap_source.appended_count or 0) > 0 or int(bootstrap_pull.get("processed_count", 0) or 0) > 0
    status = "ok" if bootstrap_source.status == "ok" and bootstrap_pull.get("status") == "ok" else "partial_error"
    return {
        **result,
        "status": "cancelled" if cancel_event is not None and cancel_event.is_set() else status,
        "source": _jsonable(bootstrap_source),
        "pull": bootstrap_pull,
        "recovery": {
            "status": "recovered" if recovered else "attempted_empty",
            "reason": "empty_local_conversation_state",
            "context_only": True,
            "ignore_seen": True,
            "candidates": candidates,
            "original_source": source,
            "original_pull": pull,
        },
    }


def _weflow_bootstrap_candidates(data_dir: str | Path, talkers: Any) -> list[dict[str, Any]]:
    requested = {str(item).strip() for item in _string_list(talkers) if str(item).strip()}
    if not (Path(data_dir) / "conversation_channels").exists():
        return []
    try:
        channels = _channel_store(data_dir).list_channels()
    except Exception:
        return []
    ledger = ConversationLedgerStore(data_dir)
    candidates: list[dict[str, Any]] = []
    for channel in channels:
        source_names = {str(item).strip() for item in channel.source_names if str(item).strip()}
        if "weflow_discovery" not in source_names:
            continue
        talker = channel.conversation_key or next((str(item).strip() for item in channel.sender_wechat_ids if str(item).strip()), "")
        if not talker or is_system_account(talker):
            continue
        if requested and talker not in requested:
            continue
        try:
            has_ledger = bool(ledger.read_entries(channel.conversation_id, include_removed=True))
        except Exception:
            has_ledger = False
        if has_ledger:
            continue
        candidates.append(
            {
                "talker": talker,
                "conversation_id": channel.conversation_id,
                "chat_title": channel.chat_title,
                "missing": ["conversation_ledger"],
            }
        )
    return candidates


def _run_sidebar_weflow_once(
    data_dir: str | Path,
    payload: dict[str, Any],
    *,
    cancel_event: threading.Event | None = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    context = _build_weflow_pull_context(data_dir, payload)
    return _run_weflow_pull_tick(context, cancel_event=cancel_event, progress_callback=progress_callback)


def _bridge_worker_state(root: Path) -> dict[str, Any]:
    """Public snapshot of the in-process send-bridge worker for this data dir."""
    key = str(root)
    with _BRIDGE_LOCK:
        worker = _BRIDGE_WORKERS.get(key)
        if not worker:
            return {"running": False, "last_status": "stopped"}
        thread = worker.get("thread")
        return {
            "running": bool(isinstance(thread, threading.Thread) and thread.is_alive()),
            "last_status": str(worker.get("last_status") or ""),
            "last_error": str(worker.get("last_error") or ""),
            "started_at": worker.get("started_at"),
            "restart_count": int(worker.get("restart_count", 0) or 0),
            "last_tick_at": worker.get("last_tick_at"),
        }


def _bridge_worker_supervisor(root: Path, payload: dict[str, Any], stop_event: threading.Event) -> None:
    """Supervise the send-bridge worker loop, mirroring the WeFlow supervisor.

    Runs ``run_bridge_worker`` (its own single-instance ProcessLock guards
    against a second in-process or CLI worker) until a stop is requested. If it
    dies unexpectedly (uncaught error, or returns without a stop), respawn with
    capped exponential backoff up to a max restart count. A held lock (another
    worker legitimately running) is a deliberate refusal, not a crash: record it
    and stop supervising without resurrection.
    """
    from app.personal_wechat_bot.runtime.process_lock import ProcessLockError
    from app.personal_wechat_bot.runtime.send_bridge_worker import run_bridge_worker

    key = str(root)
    interval = max(0.5, float(payload.get("bridge_interval_seconds") or 2.0))
    max_restarts = int(payload.get("bridge_max_restarts", _BRIDGE_MAX_RESTARTS) or _BRIDGE_MAX_RESTARTS)
    restart_count = 0
    final_status = ""

    def _mark(status: str, error: str = "") -> None:
        with _BRIDGE_LOCK:
            worker = _BRIDGE_WORKERS.get(key, {})
            worker["last_status"] = status
            if error:
                worker["last_error"] = error
            worker["last_tick_at"] = time.time()
            worker["restart_count"] = restart_count
            _BRIDGE_WORKERS[key] = worker

    while not stop_event.is_set():
        _mark("running")
        try:
            # poll while stop_event is clear; the worker checks it each tick.
            run_bridge_worker(
                root,
                poll_interval_seconds=interval,
                once=False,
                lock_enabled=True,
                stop_event=stop_event,
            )
        except ProcessLockError as exc:
            # Another bridge worker (CLI or a second sidebar) holds the lock:
            # a legitimate single-instance refusal, not a crash. Do not restart.
            _mark("lock_held", f"{type(exc).__name__}: {exc}")
            final_status = "lock_held"
            stop_event.set()
            break
        except Exception as exc:  # the loop should not raise, but be safe
            logger.exception("send bridge worker loop crashed")
            _mark("crashed", f"loop_crashed:{type(exc).__name__}: {exc}")
        if stop_event.is_set():
            final_status = "stopped"
            break
        restart_count += 1
        if restart_count > max_restarts:
            _mark("crashed", f"max_restarts_exceeded:{max_restarts}")
            final_status = "crashed"
            logger.error("send bridge worker exceeded %d restarts; giving up", max_restarts)
            break
        backoff = min(60.0, interval * (2 ** min(restart_count - 1, 5)))
        _mark("restarting")
        logger.warning(
            "send bridge worker died; restart %d/%d after %.1fs backoff", restart_count, max_restarts, backoff
        )
        if stop_event.wait(backoff):
            final_status = "stopped"
            break
    if stop_event.is_set() and not final_status:
        final_status = "stopped"
    if final_status == "stopped":
        _mark("stopped")


def _start_bridge_worker(root: Path, payload: dict[str, Any]) -> None:
    """Start the supervised send-bridge worker for this data dir if not running.

    Only starts when the active send driver is bridge_outbox — that is the only
    driver whose replies need a bridge to deliver them. Idempotent: a live worker
    is left as-is.
    """
    key = str(root)
    try:
        config = load_config(root)
    except Exception:
        return
    if str(getattr(config, "send_driver", "")) != "bridge_outbox":
        return
    with _BRIDGE_LOCK:
        existing = _BRIDGE_WORKERS.get(key)
        thread = existing.get("thread") if existing else None
        if isinstance(thread, threading.Thread) and thread.is_alive():
            return
        stop_event = threading.Event()
        worker_thread = threading.Thread(
            target=_bridge_worker_supervisor,
            args=(root, dict(payload), stop_event),
            name="sidebar-send-bridge",
            daemon=True,
        )
        _BRIDGE_WORKERS[key] = {
            "thread": worker_thread,
            "stop": stop_event,
            "started_at": time.time(),
            "last_status": "starting",
            "restart_count": 0,
        }
        worker_thread.start()


def _stop_bridge_worker(root: Path) -> None:
    """Signal the send-bridge worker to stop and briefly join it."""
    key = str(root)
    thread: threading.Thread | None = None
    with _BRIDGE_LOCK:
        worker = _BRIDGE_WORKERS.get(key)
        if worker and isinstance(worker.get("stop"), threading.Event):
            worker["stop"].set()
            worker["stop_requested"] = True
            thread = worker.get("thread") if isinstance(worker.get("thread"), threading.Thread) else None
    if thread is not None:
        thread.join(timeout=1.0)


def _weflow_background_loop(root: Path, payload: dict[str, Any], stop_event: threading.Event) -> None:
    """Supervise the worker loop: restart it if it dies while still intended-running.

    The inner loop (`_weflow_worker_loop`) can terminate on a genuine stop, or die
    from an uncaught exception outside the per-tick handler (e.g. the consumer-lock
    acquire, or a crash between ticks). Without supervision the sidebar would show
    running=false forever with no recovery. Here we respawn with capped exponential
    backoff, bounded by a max restart count, and clear the intent on stop() so a
    stopped worker is never resurrected.
    """
    key = str(root)
    interval = max(1.0, float(payload.get("interval_seconds") or 5.0))
    max_restarts = int(payload.get("max_restarts", _WEFLOW_MAX_RESTARTS) or _WEFLOW_MAX_RESTARTS)
    restart_count = 0
    while not stop_event.is_set():
        try:
            _weflow_worker_loop(root, payload, stop_event)
        except Exception as exc:  # the loop itself should not raise, but be safe
            logger.exception("weflow worker loop crashed")
            with _WEFLOW_LOCK:
                worker = _WEFLOW_WORKERS.get(key, {})
                worker["last_status"] = "crashed"
                worker["last_error"] = f"loop_crashed:{type(exc).__name__}: {exc}"
                worker["last_tick_at"] = time.time()
                _WEFLOW_WORKERS[key] = worker
        # A clean stop request means we're done — don't resurrect.
        if stop_event.is_set():
            break
        # The loop returned/died without a stop request: supervise a restart.
        restart_count += 1
        if restart_count > max_restarts:
            with _WEFLOW_LOCK:
                worker = _WEFLOW_WORKERS.get(key, {})
                worker["last_status"] = "crashed"
                worker["last_error"] = f"max_restarts_exceeded:{max_restarts}"
                worker["restart_count"] = restart_count - 1
                worker["last_tick_at"] = time.time()
                _WEFLOW_WORKERS[key] = worker
            logger.error("weflow worker exceeded %d restarts; giving up", max_restarts)
            break
        backoff = min(60.0, interval * (2 ** min(restart_count - 1, 5)))
        with _WEFLOW_LOCK:
            worker = _WEFLOW_WORKERS.get(key, {})
            worker["last_status"] = "restarting"
            worker["restart_count"] = restart_count
            worker["last_restart_at"] = time.time()
            worker["last_tick_at"] = time.time()
            _WEFLOW_WORKERS[key] = worker
        logger.warning("weflow worker died; restart %d/%d after %.1fs backoff", restart_count, max_restarts, backoff)
        # Interruptible backoff: a stop during the wait aborts the restart.
        if stop_event.wait(backoff):
            break


def _weflow_worker_loop(root: Path, payload: dict[str, Any], stop_event: threading.Event) -> None:
    interval = max(1.0, float(payload.get("interval_seconds") or 5.0))
    key = str(root)
    # Single-instance guard: refuse to run if another consumer (a CLI puller or
    # a second worker) already holds the hook consumer lock, so two loops never
    # race the shared import offset. Lock path matches HookMessagePullRunner.
    lock_path = Path(root) / "hook_events_state.json.consumer.lock"
    consumer_lock = ProcessLock(lock_path, label="sidebar_weflow_worker", stale_after_seconds=max(30.0, interval * 4))
    try:
        consumer_lock.acquire()
    except ProcessLockError as exc:
        error = f"{type(exc).__name__}: {exc}"
        with _WEFLOW_LOCK:
            worker = _WEFLOW_WORKERS.get(key, {})
            worker["last_status"] = "error"
            worker["last_error"] = error
            worker["last_tick_at"] = time.time()
            _WEFLOW_WORKERS[key] = worker
        _write_weflow_sidebar_state(root, {"last_error": error, **_weflow_public_params(_weflow_params(root, payload))})
        # A held consumer lock means another consumer is legitimately running.
        # This is a deliberate single-instance refusal, NOT a crash — signal the
        # supervisor to stop (no restart) by setting the stop event.
        stop_event.set()
        return
    context: dict[str, Any] | None = None
    try:
        while not stop_event.is_set():
            tick_started = time.monotonic()
            try:
                if context is None:
                    context = _build_weflow_pull_context(root, payload)
                else:
                    # Pick up live send-control edits (mode / send_enabled /
                    # send_driver / send_backend) without rebuilding the pull
                    # context, so the driver's dedup state survives.
                    _refresh_weflow_send_controls(context)
                def worker_progress(update: dict[str, Any]) -> None:
                    _update_weflow_task_progress(root, "worker", update)

                result = _run_weflow_pull_tick(context, progress_callback=worker_progress)
                duration = time.monotonic() - tick_started
                source_status = str(result.get("source", {}).get("status") or "")
                if source_status == "error":
                    # A total pull failure usually means WeFlow went away. Drop the
                    # context so the next tick rebuilds it and re-runs the health /
                    # fork-marker check before pulling again.
                    context = None
                consumer_lock.heartbeat()
                tick_record: dict[str, Any] = {}
                with _WEFLOW_LOCK:
                    worker = _WEFLOW_WORKERS.get(key, {})
                    metrics = worker.get("metrics")
                    if isinstance(metrics, WeflowWorkerMetrics):
                        tick_record = metrics.record_tick(result, duration).to_dict()
                    worker["last_status"] = result.get("status", "")
                    worker["last_tick_at"] = time.time()
                    _WEFLOW_WORKERS[key] = worker
                _record_weflow_state_safely(
                    root,
                    {"last_pull": result, "last_error": "", **_weflow_public_params(_weflow_params(root, payload))},
                    action="background-tick",
                    result={**result, "background": True, "tick": tick_record},
                )
            except Exception as exc:
                # A per-tick failure must never kill the daemon thread: record it
                # and continue to the next tick. The recording itself is wrapped
                # so that even a state-file write error (Windows lock, disk full)
                # or a params-build failure cannot escape and terminate the loop.
                context = None
                duration = time.monotonic() - tick_started
                error = f"{type(exc).__name__}: {exc}"
                try:
                    consumer_lock.heartbeat()
                    tick_record = {}
                    with _WEFLOW_LOCK:
                        worker = _WEFLOW_WORKERS.get(key, {})
                        metrics = worker.get("metrics")
                        if isinstance(metrics, WeflowWorkerMetrics):
                            tick_record = metrics.record_error(error, duration).to_dict()
                        worker["last_status"] = "error"
                        worker["last_error"] = error
                        worker["last_tick_at"] = time.time()
                        _WEFLOW_WORKERS[key] = worker
                    _record_weflow_state_safely(
                        root,
                        {"last_error": error, **_weflow_public_params(_weflow_params(root, payload))},
                        action="background-tick",
                        result={"status": "error", "error": error, "background": True, "tick": tick_record},
                    )
                except Exception:  # pragma: no cover - last-resort thread survival
                    logger.exception("weflow worker error-handler failed; continuing loop")
            stop_event.wait(interval)
    finally:
        consumer_lock.release()
        with _WEFLOW_LOCK:
            worker = _WEFLOW_WORKERS.get(key, {})
            worker["stop_requested"] = False
            if str(worker.get("last_status") or "") not in {"error"}:
                worker["last_status"] = "stopped"
            worker["last_tick_at"] = time.time()
            _WEFLOW_WORKERS[key] = worker


def _weflow_params(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(data_dir)
    token_env = str(payload.get("token_env") or payload.get("tokenEnv") or "WEFLOW_API_TOKEN").strip()
    direct_token = str(payload.get("token") or "").strip()
    env_token = _env_value(token_env) or _env_value("WEFLOW_API_TOKEN")
    token = direct_token or env_token
    token_source = "payload" if direct_token else ("environment" if env_token else "missing")
    since_value = payload.get("since")
    since = int(since_value) if since_value not in (None, "") else None
    # Normal incremental pulls must never inherit a stale sidebar "context only"
    # checkbox: that flag records messages but intentionally skips topic/reply
    # generation. Only backfill / explicit forced history paths may set it.
    force_context_only = bool(
        payload.get("force_context_only")
        or payload.get("forceContextOnly")
        or payload.get("history_backfill")
        or payload.get("historyBackfill")
    )
    allow_context_only = bool(payload.get("allow_context_only") or payload.get("allowContextOnly"))
    requested_context_only = bool(payload.get("context_only") or payload.get("contextOnly"))
    context_only = bool(
        force_context_only
        or (allow_context_only and requested_context_only)
        or (since is not None and since <= 0)
    )
    return {
        "base_url": str(payload.get("base_url") or payload.get("baseUrl") or "http://127.0.0.1:5031").strip(),
        "token": token,
        "token_env": token_env,
        "token_source": token_source,
        "allow_non_local": bool(payload.get("allow_non_local") or payload.get("allowNonLocal") or False),
        "hook_event_file": str(root / "hook_events.jsonl"),
        "backend_event_file": str(root / "backend_events.jsonl"),
        "hook_state_file": str(root / "hook_events_state.json"),
        "weflow_state_file": str(root / "weflow_bridge_state.json"),
        "talkers": _string_list(payload.get("talkers") or payload.get("talker") or []),
        "session_limit": _bounded_int(payload.get("session_limit"), 100, 1, 10000),
        "message_limit": _bounded_int(payload.get("message_limit"), 100, 1, 10000),
        "max_pages": _bounded_int(payload.get("max_pages"), 1, 0, 10000),
        "max_messages": _bounded_int(payload.get("max_messages"), 0, 0, 1000000),
        "since": since,
        "lookback_seconds": _bounded_int(payload.get("lookback_seconds"), 300, 0, 30 * 24 * 3600),
        "workers": _bounded_int(payload.get("workers"), 2, 1, 16),
        "media": not bool(payload.get("no_media") or payload.get("noMedia") or False),
        "context_only": context_only,
        "extra_roots": _string_list(payload.get("extra_roots") or payload.get("extraRoots") or []),
    }


def _env_value(name: str) -> str:
    key = str(name or "").strip()
    if not key:
        return ""
    value = os.environ.get(key, "")
    if value:
        return value
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as env_key:
            raw, _ = winreg.QueryValueEx(env_key, key)
    except Exception:
        return ""
    return str(raw or "").strip()


@contextmanager
def _weflow_exclusive_operation(
    data_dir: str | Path,
    *,
    label: str,
    wait_timeout_seconds: float = 900.0,
):
    root = Path(data_dir)
    lock_path = root / "weflow_global_operation.lock"
    with _WEFLOW_OPERATION_LOCK:
        with blocking_process_lock(
            lock_path,
            label=label,
            stale_after_seconds=1800.0,
            wait_timeout_seconds=wait_timeout_seconds,
            poll_interval_seconds=0.05,
        ):
            yield


def _normalize_weflow_session(session: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(session, dict):
        return {}
    session_id = str(
        session.get("id")
        or session.get("username")
        or session.get("talker")
        or session.get("sessionId")
        or session.get("session_id")
        or ""
    ).strip()
    name = _preferred_weflow_display_name(
        session.get("remark"),
        session.get("displayName"),
        session.get("display_name"),
        session.get("nickName"),
        session.get("nickname"),
        session.get("groupName"),
        session.get("group_name"),
        session.get("name"),
        session.get("username"),
        session_id,
    )
    session_type = str(session.get("type") or session.get("sessionType") or session.get("session_type") or "").strip()
    if not session_type:
        session_type = "group" if session_id.endswith("@chatroom") else "private"
    return {
        **session,
        "id": session_id,
        "name": name or session_id,
        "type": session_type,
        "unread_count": session.get("unread_count", session.get("unreadCount", session.get("unread"))),
        "last_message_time": session.get(
            "last_message_time",
            session.get("lastMessageTime", session.get("lastActiveTime", session.get("updateTime"))),
        ),
    }


def _register_weflow_sessions(data_dir: str | Path, sessions: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        store = _channel_store(data_dir)
    except Exception as exc:
        return {
            "registered_count": 0,
            "registered_channels": [],
            "registration_errors": [{"type": type(exc).__name__, "message": str(exc)}],
        }
    registered: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for session in sessions:
        session_id = str(session.get("id") or "").strip()
        if not session_id:
            continue
        try:
            channel = store.ensure_channel(_weflow_session_channel_message(session))
            registered.append(
                {
                    "id": session_id,
                    "name": str(session.get("name") or session_id),
                    "conversation_id": channel.conversation_id,
                    "conversation_type": channel.conversation_type,
                    "chat_title": channel.chat_title,
                }
            )
        except Exception as exc:
            errors.append({"session": session_id, "type": type(exc).__name__, "message": str(exc)})
    return {
        "registered_count": len(registered),
        "registered_channels": registered,
        "registration_errors": errors[:20],
    }


def _register_weflow_result_sessions(data_dir: str | Path, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    sessions = _weflow_sessions_from_payload_and_result(payload, result)
    if not sessions:
        return {"status": "empty", "updated_count": 0, "registered_count": 0}
    registration = _register_weflow_sessions(data_dir, sessions)
    return _upsert_weflow_sessions(data_dir, sessions, source="weflow_pull", registration=registration)


def _weflow_sessions_from_payload_and_result(payload: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    sessions: list[dict[str, Any]] = []

    def add(session_id: str, name: str = "", session_type: str = "") -> None:
        session_id = str(session_id or "").strip()
        if not session_id or is_system_account(session_id) or session_id in seen:
            return
        seen.add(session_id)
        sessions.append(
            _normalize_weflow_session(
                {
                    "id": session_id,
                    "name": name or session_id,
                    "type": session_type or ("group" if session_id.endswith("@chatroom") else "private"),
                }
            )
        )

    for talker in _string_list(payload.get("talkers") or payload.get("talker") or []):
        add(talker)
    source = result.get("source") if isinstance(result.get("source"), dict) else {}
    for key in ("sessions", "pulled_sessions"):
        values = source.get(key)
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict):
                    normalized = _normalize_weflow_session(item)
                    add(str(normalized.get("id") or ""), str(normalized.get("name") or ""), str(normalized.get("type") or ""))
                else:
                    add(str(item))
    errors = source.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                add(str(item.get("session") or item.get("talker") or ""))
    state_path = str(source.get("state_path") or "").strip()
    if state_path:
        state_payload = _read_json(Path(state_path), {})
        sessions_state = state_payload.get("sessions") if isinstance(state_payload, dict) else {}
        if isinstance(sessions_state, dict):
            for session_id in sessions_state:
                add(str(session_id))
    return sessions


def _upsert_weflow_sessions(
    data_dir: str | Path,
    sessions: list[dict[str, Any]],
    *,
    source: str,
    registration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(data_dir)
    path = _weflow_session_store_path(root)
    current = _read_json(path, {})
    items = current.get("sessions") if isinstance(current, dict) else {}
    if not isinstance(items, dict):
        items = {}
    channels = {
        str(item.get("id") or ""): item
        for item in (registration or {}).get("registered_channels", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    updated = 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for session in sessions:
        normalized = _normalize_weflow_session(session)
        session_id = str(normalized.get("id") or "").strip()
        if not session_id or is_system_account(session_id):
            continue
        existing = items.get(session_id) if isinstance(items.get(session_id), dict) else {}
        channel = channels.get(session_id, {})
        display_name = _preferred_weflow_display_name(
            normalized.get("name"),
            existing.get("name") if isinstance(existing, dict) else "",
            channel.get("chat_title") if isinstance(channel, dict) else "",
            session_id,
        )
        merged = {
            **existing,
            **normalized,
            "id": session_id,
            "name": display_name,
            "type": str(normalized.get("type") or existing.get("type") or ("group" if session_id.endswith("@chatroom") else "private")),
            "conversation_id": channel.get("conversation_id") or existing.get("conversation_id") or "",
            "conversation_type": channel.get("conversation_type") or existing.get("conversation_type") or "",
            "chat_title": _preferred_weflow_display_name(
                channel.get("chat_title") if isinstance(channel, dict) else "",
                existing.get("chat_title") if isinstance(existing, dict) else "",
                display_name,
                session_id,
            ),
            "cached": True,
            "source": source,
            "updated_at": now,
        }
        items[session_id] = merged
        updated += 1
    payload = {
        "version": 1,
        "sessions": items,
        "updated_at": now,
    }
    _write_json(path, payload)
    return {
        "status": "ok",
        "store": str(path),
        "updated_count": updated,
        "registered_count": int((registration or {}).get("registered_count", 0) or 0),
    }


def _preferred_weflow_display_name(*values: Any) -> str:
    fallback = ""
    for value in values:
        text = str(value or "").strip()
        if not text or _looks_like_placeholder_display_name(text):
            continue
        if not fallback:
            fallback = text
        if not _looks_like_wechat_receiver(text):
            return text
    return fallback


def _looks_like_placeholder_display_name(value: str) -> bool:
    return str(value or "").strip().lower() in {
        "unknown",
        "unknown contact",
        "未知",
        "未知联系人",
        "system",
        "none",
        "null",
    }


def _weflow_session_channel_message(session: dict[str, Any]) -> NormalizedMessage:
    session_id = str(session.get("id") or "").strip()
    if not session_id:
        raise ValueError("session id is required")
    conversation_type = "group" if _weflow_session_is_group(session) else "private"
    name = str(session.get("name") or session_id).strip() or session_id
    return NormalizedMessage(
        message_id=f"weflow-discovery:{session_id}",
        conversation_id=conversation_id_for(conversation_type, session_id),
        conversation_type=conversation_type,
        chat_title=name,
        sender_name=name,
        text="",
        is_self=False,
        received_at=utc_now_iso(),
        sender_wechat_id=session_id,
        metadata={
            "source": "weflow_discovery",
            "trusted_channel_source": True,
            "conversation_key": session_id,
            "talker": session_id,
            "weflow_session": session,
        },
    )


def _weflow_cached_sessions_from_channels(data_dir: str | Path, *, limit: int) -> list[dict[str, Any]]:
    if not (Path(data_dir) / "conversation_channels").exists():
        return []
    try:
        channels = _channel_store(data_dir).list_channels()
    except Exception:
        return []
    sessions: list[dict[str, Any]] = []
    for channel in channels:
        source_names = {str(item).strip() for item in channel.source_names if str(item).strip()}
        if "weflow_discovery" not in source_names:
            continue
        session_id = next((str(item).strip() for item in channel.sender_wechat_ids if str(item).strip()), "")
        if not session_id or is_system_account(session_id):
            continue
        sessions.append(
            {
                "id": session_id,
                "name": channel.chat_title or session_id,
                "type": channel.conversation_type,
                "conversation_id": channel.conversation_id,
                "cached": True,
                "updated_at": channel.updated_at,
            }
        )
    return sorted(sessions, key=lambda item: str(item.get("updated_at", "")), reverse=True)[:limit]


def _weflow_cached_sessions(data_dir: str | Path, *, limit: int) -> list[dict[str, Any]]:
    root = Path(data_dir)
    stored = _weflow_cached_sessions_from_store(root, limit=limit)
    by_id = {str(item.get("id") or ""): item for item in stored if str(item.get("id") or "").strip()}
    for item in _weflow_cached_sessions_from_channels(root, limit=limit):
        session_id = str(item.get("id") or "").strip()
        if not session_id:
            continue
        existing = by_id.get(session_id, {})
        merged = {**item, **existing}
        merged["name"] = _preferred_weflow_display_name(item.get("name"), existing.get("name"), session_id)
        merged["chat_title"] = _preferred_weflow_display_name(
            item.get("chat_title"),
            existing.get("chat_title"),
            merged.get("name"),
            session_id,
        )
        by_id[session_id] = merged
    return sorted(by_id.values(), key=lambda item: str(item.get("updated_at", "")), reverse=True)[:limit]


def _weflow_cached_sessions_from_store(data_dir: str | Path, *, limit: int) -> list[dict[str, Any]]:
    payload = _read_json(_weflow_session_store_path(data_dir), {})
    items = payload.get("sessions") if isinstance(payload, dict) else {}
    if not isinstance(items, dict):
        return []
    sessions = [item for item in items.values() if isinstance(item, dict) and str(item.get("id") or "").strip()]
    return sorted(sessions, key=lambda item: str(item.get("updated_at", "")), reverse=True)[:limit]


def _weflow_session_store_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "weflow_sessions.json"


def _weflow_session_is_group(session: dict[str, Any]) -> bool:
    session_id = str(session.get("id") or session.get("sessionId") or session.get("username") or "").strip()
    session_type = str(session.get("type") or session.get("sessionType") or session.get("session_type") or "").strip()
    if session_type in {"group", "2"}:
        return True
    if session_type in {"private", "friend", "other", "1"}:
        return False
    return session_id.endswith("@chatroom")


def _weflow_public_params(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "base_url": params.get("base_url", ""),
        "token_env": params.get("token_env", "WEFLOW_API_TOKEN"),
        "token_present": bool(params.get("token")),
        "token_source": params.get("token_source", "missing"),
        "allow_non_local": bool(params.get("allow_non_local")),
    }


def _weflow_media_roots(weflow_ready: dict[str, Any]) -> list[str]:
    health = weflow_ready.get("health") if isinstance(weflow_ready.get("health"), dict) else {}
    media_path = str(health.get("mediaExportPath") or health.get("media_export_path") or "").strip()
    return [media_path] if media_path else []


def _voice_cache_resolver(config: Any, *, extra_roots: list[str] | None = None) -> WeChatVoiceCacheResolver | None:
    roots = config.wechat_voice_roots + list(extra_roots or [])
    if not roots:
        return None
    return WeChatVoiceCacheResolver(
        resolve_allowed_roots(config.data_dir, roots),
        allowed_extensions=config.file_allowed_extensions,
        max_bytes=config.file_max_bytes,
    )


def _weflow_pull_job_loop(root: Path, payload: dict[str, Any], job_id: str) -> None:
    def progress(update: dict[str, Any]) -> None:
        _update_pull_job(root, job_id, progress=update, force=False)
        _update_weflow_task_progress(root, job_id, update)

    try:
        _update_pull_job(root, job_id, status="running", progress={"event": "started"}, force=True)
        _update_weflow_task_progress(root, job_id, {"event": "started", "phase": "后台拉取任务已启动"})
        result = _run_sidebar_weflow_once(root, payload, progress_callback=progress)
        result["session_store"] = _register_weflow_result_sessions(root, payload, result)
        final_status = "completed" if result.get("status") != "error" else "error"
        _update_pull_job(
            root,
            job_id,
            status=final_status,
            result=result,
            progress={
                "scanned_count": result.get("source", {}).get("scanned_count", 0),
                "appended_count": result.get("source", {}).get("appended_count", 0),
                "processed_count": result.get("pull", {}).get("processed_count", 0),
                "event": final_status,
            },
            force=True,
        )
        _record_weflow_state_safely(
            root,
            {"last_pull": result, "last_error": "" if result.get("status") != "error" else str(result.get("error") or ""), "pull_job": _weflow_pull_job_state(root), **_weflow_public_params(_weflow_params(root, payload))},
            action="pull-once",
            result=result,
        )
        _update_pull_job(root, job_id, status=final_status, progress={"event": final_status}, force=True)
        _update_weflow_task_progress(root, job_id, {"event": final_status, "phase": "后台拉取任务结束"}, final=True)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        result = {"status": "error", "error": error}
        _update_pull_job(root, job_id, status="error", result=result, last_error=error, force=True)
        _update_weflow_task_progress(root, job_id, {"event": "error", "phase": error}, final=True)
        _record_weflow_state_safely(
            root,
            {"last_pull": result, "last_error": error, "pull_job": _weflow_pull_job_state(root), **_weflow_public_params(_weflow_params(root, payload))},
            action="pull-once",
            result=result,
        )


def _update_weflow_task_progress(root: Path, job_id: str, update: dict[str, Any], *, final: bool = False) -> None:
    event = str(update.get("event") or "update")
    session_id = str(update.get("session_id") or "").strip()
    task_id = _weflow_task_id(job_id, session_id)
    conversation_id = _conversation_id_for_weflow_session(session_id)
    progress = _weflow_task_progress(event, update, final=final)
    status = "completed" if final and event in {"completed", "ok"} else ("failed" if final or event == "error" else "running")
    phase = str(update.get("phase") or _weflow_event_phase(event))
    detail = _weflow_progress_detail(update)
    store = TaskStatusStore(root)
    payload = {
        "task_id": task_id,
        "title": f"WeFlow 拉取：{session_id}" if session_id else "WeFlow 拉取任务",
        "kind": "WeFlow",
        "status": status,
        "priority": 70,
        "progress": progress,
        "phase": phase,
        "detail": detail,
        "conversation_id": conversation_id,
        "session_id": session_id or "system",
        "concurrency_key": f"weflow:pull:{session_id or job_id}",
        "resource_class": "wechat_io",
        "external_id": job_id,
        "metadata": {
            "scope_label": "WeFlow后台拉取",
            "job_id": job_id,
            "session_id": session_id,
            "event": event,
        },
    }
    try:
        store.create(payload)
        if final:
            store.finish_external(
                job_id,
                {
                    "status": status,
                    "progress": 100,
                    "phase": phase,
                    "detail": detail,
                },
            )
    except Exception:
        return


def _weflow_task_id(job_id: str, session_id: str) -> str:
    if not session_id:
        return f"weflow-{job_id}"
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:10]
    return f"weflow-{job_id}-{digest}"


def _conversation_id_for_weflow_session(session_id: str) -> str:
    talker = str(session_id or "").strip()
    if not talker:
        return ""
    conversation_type = "group" if talker.endswith("@chatroom") else "private"
    return conversation_id_for(conversation_type, talker)


def _weflow_task_progress(event: str, update: dict[str, Any], *, final: bool) -> int:
    if final:
        return 100
    if event == "started":
        return 5
    if event == "source_started":
        return 10
    if event == "page":
        return min(45, 18 + int(update.get("page_count") or 0) * 6)
    if event == "message":
        return min(58, 35 + int(update.get("appended_count") or 0))
    if event == "delete":
        return 58
    if event == "source_completed":
        return 62
    if event == "consume_started":
        return 72
    if event == "consume_completed":
        return 92
    return 50


def _weflow_event_phase(event: str) -> str:
    return {
        "started": "后台拉取任务已启动",
        "source_started": "正在读取 WeFlow 消息页",
        "page": "正在扫描消息页",
        "message": "正在写入新消息事件",
        "delete": "正在同步本地删除/撤回",
        "source_completed": "WeFlow 消息页读取完成",
        "consume_started": "正在导入事件并运行 agent",
        "consume_completed": "事件导入与 agent 处理完成",
        "completed": "后台拉取完成",
        "error": "后台拉取失败",
    }.get(event, event or "更新中")


def _weflow_progress_detail(update: dict[str, Any]) -> str:
    parts = []
    for key, label in (
        ("session_id", "通道"),
        ("page_count", "页"),
        ("scanned_count", "扫描"),
        ("appended_count", "写入"),
        ("processed_count", "处理"),
        ("last_raw_id", "消息"),
    ):
        value = update.get(key)
        if value not in ("", None, 0):
            parts.append(f"{label}={value}")
    return " / ".join(parts)


def _update_pull_job(
    root: Path,
    job_id: str,
    *,
    status: str | None = None,
    progress: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    last_error: str | None = None,
    force: bool = False,
) -> None:
    key = str(root.resolve())
    should_write = force
    with _WEFLOW_LOCK:
        job = _WEFLOW_PULL_JOBS.get(key)
        if not job or str(job.get("job_id")) != job_id:
            return
        now = time.time()
        if status is not None:
            job["status"] = status
        if result is not None:
            job["result"] = result
        if last_error is not None:
            job["last_error"] = last_error
        if progress:
            _merge_weflow_progress(job, progress)
        job["updated_at"] = now
        last_write = float(job.get("_last_state_write", 0) or 0)
        if should_write or now - last_write >= 0.75:
            job["_last_state_write"] = now
            should_write = True
        snapshot = _public_pull_job(job)
    if should_write:
        _write_weflow_sidebar_state(root, {"pull_job": snapshot})


def _weflow_pull_job_state(root: Path, persisted: dict[str, Any] | None = None) -> dict[str, Any]:
    key = str(root.resolve())
    with _WEFLOW_LOCK:
        job = _WEFLOW_PULL_JOBS.get(key)
        if job:
            return _public_pull_job(job)
    persisted_job = (persisted or {}).get("pull_job") if isinstance(persisted, dict) else {}
    if not isinstance(persisted_job, dict):
        return {}
    if bool(persisted_job.get("running")):
        status = str(persisted_job.get("status") or "")
        interrupted = status in {"running", ""}
        return {**persisted_job, "running": False, "status": "interrupted" if interrupted else status}
    return persisted_job


def _public_pull_job(job: dict[str, Any]) -> dict[str, Any]:
    status = str(job.get("status") or "")
    age = max(0.0, time.time() - float(job.get("started_at") or time.time()))
    running = _thread_alive(job.get("thread")) or (status == "running" and not job.get("result") and age < 3.0)
    status = status or ("running" if running else "idle")
    return {
        "job_id": str(job.get("job_id") or ""),
        "status": status,
        "running": running,
        "talkers": list(job.get("talkers") or []),
        "started_at": job.get("started_at", 0),
        "updated_at": job.get("updated_at", 0),
        "seconds_running": round(max(0.0, time.time() - float(job.get("started_at") or time.time())), 3) if running else 0,
        "progress": job.get("progress") if isinstance(job.get("progress"), dict) else {},
        "last_error": str(job.get("last_error") or ""),
        "result": _compact_weflow_history_payload(job.get("result") or {}),
    }


def _emit_weflow_progress(progress_callback: Any, **payload: Any) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback({key: value for key, value in payload.items() if value not in ("", None)})
    except Exception:
        return


def _merge_weflow_progress(job: dict[str, Any], progress: dict[str, Any]) -> None:
    current = job.get("progress")
    if not isinstance(current, dict):
        current = {}
    event = str(progress.get("event") or "")
    history = current.get("events")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "event": event or "update",
            "phase": str(progress.get("phase") or ""),
            "session_id": str(progress.get("session_id") or ""),
            "raw_id": str(progress.get("last_raw_id") or ""),
            "at": time.time(),
        }
    )
    current["events"] = history[-30:]
    if event == "page":
        current["page_count"] = int(progress.get("page_count") or current.get("page_count") or 0)
        current["current_session_id"] = str(progress.get("session_id") or current.get("current_session_id") or "")
        current["page_messages"] = int(progress.get("page_messages") or 0)
        current["total_messages"] = int(progress.get("total_messages") or current.get("total_messages") or 0)
        current["has_more"] = bool(progress.get("has_more"))
    elif event in {"message", "delete"}:
        current["current_session_id"] = str(progress.get("session_id") or current.get("current_session_id") or "")
        current["scanned_count"] = max(int(current.get("scanned_count") or 0), int(progress.get("scanned_count") or 0))
        current["appended_count"] = max(int(current.get("appended_count") or 0), int(progress.get("appended_count") or 0))
        current["last_raw_id"] = str(progress.get("last_raw_id") or current.get("last_raw_id") or "")
        if event == "delete":
            current["delete_count"] = int(current.get("delete_count") or 0) + 1
    elif event == "sessions":
        current["session_count"] = int(progress.get("session_count") or current.get("session_count") or 0)
    else:
        current.update({key: value for key, value in progress.items() if value not in ("", None)})
    current["event"] = event or str(current.get("event") or "")
    job["progress"] = current


def _weflow_backfill_job_loop(root: Path, payload: dict[str, Any], job_id: str, stop_event: threading.Event) -> None:
    def progress(update: dict[str, Any]) -> None:
        _update_backfill_job(root, job_id, progress=update, force=False)

    try:
        _update_backfill_job(root, job_id, status="running", progress={"event": "started"}, force=True)
        result = _run_sidebar_weflow_once(root, payload, cancel_event=stop_event, progress_callback=progress)
        result = {**result, "backfill": True, "backfilled_talkers": payload.get("talkers", [])}
        result["session_store"] = _register_weflow_result_sessions(root, payload, result)
        if stop_event.is_set() and result.get("status") != "cancelled":
            result = {**result, "status": "cancelled"}
        final_status = "cancelled" if result.get("status") == "cancelled" else "completed"
        _update_backfill_job(
            root,
            job_id,
            status=final_status,
            result=result,
            progress={
                "scanned_count": result.get("source", {}).get("scanned_count", 0),
                "appended_count": result.get("source", {}).get("appended_count", 0),
                "processed_count": result.get("pull", {}).get("processed_count", 0),
                "event": final_status,
            },
            force=True,
        )
        _record_weflow_state_safely(
            root,
            {"last_backfill": result, "last_error": "", "backfill_job": _weflow_backfill_job_state(root), **_weflow_public_params(_weflow_params(root, payload))},
            action="backfill",
            result=result,
        )
        _update_backfill_job(root, job_id, status=final_status, progress={"event": final_status}, force=True)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        result = {"status": "error", "error": error, "backfill": True, "backfilled_talkers": payload.get("talkers", [])}
        _update_backfill_job(root, job_id, status="error", result=result, last_error=error, force=True)
        _record_weflow_state_safely(
            root,
            {"last_backfill": result, "last_error": error, "backfill_job": _weflow_backfill_job_state(root), **_weflow_public_params(_weflow_params(root, payload))},
            action="backfill",
            result=result,
        )


def _update_backfill_job(
    root: Path,
    job_id: str,
    *,
    status: str | None = None,
    progress: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    last_error: str | None = None,
    force: bool = False,
) -> None:
    key = str(root.resolve())
    should_write = force
    final_with_result = result is not None and str(status or "") in {"completed", "cancelled", "error"}
    with _WEFLOW_LOCK:
        job = _WEFLOW_BACKFILL_JOBS.get(key)
        if not job or str(job.get("job_id")) != job_id:
            return
        now = time.time()
        if status is not None and not final_with_result:
            job["status"] = status
        if result is not None:
            job["result"] = result
        if last_error is not None:
            job["last_error"] = last_error
        if progress:
            current = job.get("progress")
            if not isinstance(current, dict):
                current = {}
            event = str(progress.get("event") or "")
            if event == "page":
                current["page_count"] = int(progress.get("page_count") or current.get("page_count") or 0)
                current["current_session_id"] = str(progress.get("session_id") or current.get("current_session_id") or "")
                current["page_messages"] = int(progress.get("page_messages") or 0)
                current["total_messages"] = int(progress.get("total_messages") or current.get("total_messages") or 0)
                current["has_more"] = bool(progress.get("has_more"))
            elif event == "message":
                current["current_session_id"] = str(progress.get("session_id") or current.get("current_session_id") or "")
                current["scanned_count"] = max(int(current.get("scanned_count") or 0), int(progress.get("scanned_count") or 0))
                current["appended_count"] = max(int(current.get("appended_count") or 0), int(progress.get("appended_count") or 0))
                current["last_raw_id"] = str(progress.get("last_raw_id") or current.get("last_raw_id") or "")
            elif event == "sessions":
                current["session_count"] = int(progress.get("session_count") or current.get("session_count") or 0)
            else:
                current.update({key: value for key, value in progress.items() if value not in ("", None)})
            current["event"] = event or str(current.get("event") or "")
            job["progress"] = current
        job["updated_at"] = now
        last_write = float(job.get("_last_state_write", 0) or 0)
        if should_write or now - last_write >= 0.75:
            job["_last_state_write"] = now
            should_write = True
        snapshot_job = {**job, "status": status} if final_with_result and status is not None else job
        snapshot = _public_backfill_job(snapshot_job)
        state_patch: dict[str, Any] = {"backfill_job": snapshot}
        if final_with_result:
            state_patch["last_backfill"] = result
            state_patch["last_error"] = str(result.get("error") or "") if result.get("status") == "error" else ""
    if should_write:
        _write_weflow_sidebar_state(root, state_patch)
    if final_with_result and status is not None:
        with _WEFLOW_LOCK:
            job = _WEFLOW_BACKFILL_JOBS.get(key)
            if job and str(job.get("job_id")) == job_id:
                job["status"] = status
                job["updated_at"] = time.time()


def _weflow_backfill_job_state(root: Path, persisted: dict[str, Any] | None = None) -> dict[str, Any]:
    key = str(root.resolve())
    with _WEFLOW_LOCK:
        job = _WEFLOW_BACKFILL_JOBS.get(key)
        if job:
            return _public_backfill_job(job)
    persisted_job = (persisted or {}).get("backfill_job") if isinstance(persisted, dict) else {}
    if not isinstance(persisted_job, dict):
        return {}
    # No live thread backs a persisted snapshot (e.g. the server restarted while
    # a backfill was mid-flight). Never report it as still running, or the UI
    # would keep the Backfill button disabled and Cancel enabled forever with
    # nothing left to clear the flag.
    running = bool(persisted_job.get("running"))
    if running:
        status = str(persisted_job.get("status") or "")
        interrupted = status in {"running", "cancel_requested", ""}
        return {
            **persisted_job,
            "running": False,
            "status": "interrupted" if interrupted else status,
        }
    return persisted_job


def _public_backfill_job(job: dict[str, Any]) -> dict[str, Any]:
    status = str(job.get("status") or "")
    age = max(0.0, time.time() - float(job.get("started_at") or time.time()))
    running = _thread_alive(job.get("thread")) or (status == "running" and not job.get("result") and age < 3.0)
    status = status or ("running" if running else "idle")
    return {
        "job_id": str(job.get("job_id") or ""),
        "status": status,
        "running": running,
        "cancel_requested": bool(job.get("cancel_requested")),
        "talkers": list(job.get("talkers") or []),
        "started_at": job.get("started_at", 0),
        "updated_at": job.get("updated_at", 0),
        "seconds_running": round(max(0.0, time.time() - float(job.get("started_at") or time.time())), 3) if running else 0,
        "progress": job.get("progress") if isinstance(job.get("progress"), dict) else {},
        "last_error": str(job.get("last_error") or ""),
        "result": _compact_weflow_history_payload(job.get("result") or {}),
    }


def _thread_alive(value: Any) -> bool:
    return isinstance(value, threading.Thread) and value.is_alive()


def _weflow_worker_state(root: Path) -> dict[str, Any]:
    key = str(root.resolve())
    with _WEFLOW_LOCK:
        worker = _WEFLOW_WORKERS.get(key, {})
        thread = worker.get("thread")
        running = bool(thread and thread.is_alive())
        metrics = worker.get("metrics")
        state = {
            "running": running,
            "started_at": worker.get("started_at", 0),
            "loops": int(worker.get("loops", 0) or 0),
            "last_status": str(worker.get("last_status", "")),
            "last_error": str(worker.get("last_error", "")),
            "last_tick_at": worker.get("last_tick_at", 0),
            "stop_requested": bool(worker.get("stop_requested")),
            "restart_count": int(worker.get("restart_count", 0) or 0),
            "last_restart_at": worker.get("last_restart_at", 0),
        }
        if isinstance(metrics, WeflowWorkerMetrics):
            snapshot = metrics.snapshot(running=running)
            state["loops"] = snapshot["loops"]
            state["metrics"] = snapshot
    return state


def _weflow_readiness_snapshot(
    persisted: dict[str, Any],
    worker: dict[str, Any],
    token_present: bool,
    token_source: str,
) -> dict[str, Any]:
    last_health = persisted.get("last_health") if isinstance(persisted.get("last_health"), dict) else {}
    health_ok = str(last_health.get("status") or "") == "ok"
    fork_ok = bool(last_health.get("fork_ok"))
    service_reachable = health_ok
    running = bool(worker.get("running"))
    if running and not service_reachable:
        status = "worker_running_unchecked"
    elif health_ok and token_present and fork_ok:
        status = "ready"
    elif health_ok and not token_present:
        status = "token_missing"
    elif health_ok and not fork_ok:
        status = "fork_marker_missing"
    elif last_health:
        status = "error"
    else:
        status = "unchecked"
    return {
        "status": status,
        "service_reachable": service_reachable,
        "token_present": token_present,
        "token_source": token_source,
        "fork_ok": fork_ok,
        "worker_running": running,
        "last_health_status": str(last_health.get("status") or ""),
        "message": str(last_health.get("message") or last_health.get("error") or ""),
        "updated_at": persisted.get("updated_at", ""),
    }


def _write_weflow_sidebar_state(data_dir: str | Path, update: dict[str, Any]) -> None:
    path = Path(data_dir) / "weflow_sidebar_state.json"
    with _WEFLOW_STATE_FILE_LOCK:
        current = _read_json(path, {})
        payload = current if isinstance(current, dict) else {}
        payload.update(update)
        payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_json(path, payload)


def _record_weflow_state_safely(
    data_dir: str | Path,
    update: dict[str, Any],
    *,
    action: str,
    result: dict[str, Any],
) -> None:
    """Persist sidebar state/history without turning a successful WeFlow action
    into a failed HTTP response when Windows temporarily locks the state file."""

    try:
        _write_weflow_sidebar_state(data_dir, update)
    except Exception as exc:
        if isinstance(result, dict):
            result["state_write_error"] = f"{type(exc).__name__}: {exc}"
    try:
        _append_weflow_operation_history(data_dir, action, result)
    except Exception as exc:
        if isinstance(result, dict):
            result["history_write_error"] = f"{type(exc).__name__}: {exc}"


def _append_weflow_operation_history(data_dir: str | Path, action: str, result: dict[str, Any]) -> None:
    path = Path(data_dir) / "weflow_sidebar_state.json"
    with _WEFLOW_STATE_FILE_LOCK:
        current = _read_json(path, {})
        payload = current if isinstance(current, dict) else {}
        existing = payload.get("operation_history")
        history = existing if isinstance(existing, list) else []
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "action": str(action),
            "status": str(result.get("status", "")) if isinstance(result, dict) else "",
            "summary": _weflow_operation_summary(result),
            "result": _compact_weflow_history_payload(result),
        }
        payload["operation_history"] = [entry, *[item for item in history if isinstance(item, dict)]][:50]
        payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_json(path, payload)


def _weflow_operation_summary(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return ""
    message = str(result.get("message") or result.get("error") or "").strip()
    parts = []
    if result.get("count") is not None:
        parts.append(f"会话数={result.get('count')}")
    if "backfilled_talkers" in result:
        talkers = result.get("backfilled_talkers")
        if isinstance(talkers, list):
            parts.append(f"回填对象={len(talkers)}个")
        else:
            parts.append(f"回填对象={talkers}")
    if result.get("workers") is not None:
        parts.append(f"workers={result.get('workers')}")
    source = result.get("source") if isinstance(result.get("source"), dict) else {}
    pull = result.get("pull") if isinstance(result.get("pull"), dict) else {}
    imported = pull.get("import") if isinstance(pull.get("import"), dict) else {}
    if source:
        if source.get("status") is not None:
            parts.append(f"源={source.get('status')}")
        if source.get("scanned_count") is not None:
            parts.append(f"源扫描={source.get('scanned_count')}")
        if source.get("appended_count") is not None:
            parts.append(f"源新增={source.get('appended_count')}")
    if imported and imported.get("appended_count") is not None:
        parts.append(f"导入后端={imported.get('appended_count')}")
    if pull:
        if pull.get("processed_count") is not None:
            parts.append(f"写入对话={pull.get('processed_count')}")
    if message:
        parts.append(message)
    return " / ".join(part for part in parts if part and not part.endswith("=None"))[:500]


def _compact_weflow_history_payload(value: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return "<truncated>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 30:
                result["<truncated_keys>"] = max(0, len(value) - index)
                break
            result[str(key)] = _compact_weflow_history_payload(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_compact_weflow_history_payload(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return value if len(value) <= 1000 else value[:1000] + "...<truncated>"
    return value


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.03 * (attempt + 1))
        except OSError as exc:
            last_error = exc
            if getattr(exc, "winerror", None) not in {5, 32}:
                raise
            time.sleep(0.03 * (attempt + 1))
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    if last_error is not None:
        raise last_error


def _jsonable(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",")]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _bounded_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return min(max(parsed, lower), upper)


def _cleanup_note(cleanup: dict[str, Any]) -> str:
    policy = str(cleanup.get("cleanup_policy", ""))
    if policy == "wechat_preserve":
        return "微信可信通道已清除注册，对话文件、文件中间层和 session 已保留"
    if policy == "non_wechat_purge":
        return "非微信来源通道已完全清理，包括对话文件、文件中间层和 session"
    return "通道不存在或已被清理"


def _retained_config_paths(root: Path) -> set[Path]:
    names = {
        "config.json",
        "accepted_contacts.json",
        "accepted_groups.json",
        "contacts_whitelist.json",
        "groups_whitelist.json",
        "topic_rules.json",
        "search_blocklist.json",
        "api_keys.local.md",
        "api_key_models.local.json",
    }
    retained = {(root / name).resolve() for name in names}
    retained.add((root / "runtime").resolve())
    return retained


def _remove_history_path(
    root: Path,
    relative: str,
    removed: list[dict[str, Any]],
    retained_locked: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    retained: set[Path],
) -> None:
    target = (root / relative).resolve()
    if target in retained or any(parent in retained for parent in target.parents):
        return
    if target != root and root not in target.parents:
        raise ValueError(f"history reset target escapes data_dir: {relative}")
    if not target.exists():
        return
    try:
        if target.is_dir():
            shutil.rmtree(target)
            kind = "dir"
        else:
            target.unlink()
            kind = "file"
    except OSError as exc:
        if not target.is_dir() and relative in _HISTORY_RESET_LOCK_TOLERANT_FILES and _is_windows_locked_file_error(exc):
            retained_locked.append(_locked_history_record(relative, target, exc))
            _truncate_locked_history_file(target, relative, retained_locked[-1])
            return
        errors.append(
            {
                "relative_path": relative,
                "path": str(target),
                "kind": "dir" if target.is_dir() else "file",
                "error": f"{type(exc).__name__}: {exc}",
                "winerror": getattr(exc, "winerror", None),
            }
        )
        return
    removed.append({"relative_path": relative, "path": str(target), "kind": kind})


def _reinitialize_history_runtime_files(root: Path) -> list[str]:
    """Recreate empty queue/bridge files after a history reset.

    The reset may clear historical contents, but the sidebar should come back
    with the send-review and bridge paths readable instead of looking as if the
    feature was removed.
    """

    created: list[str] = []
    for relative in ("confirm_queue.jsonl", "send_audit.jsonl"):
        path = root / relative
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("", encoding="utf-8")
                created.append(relative)
        except OSError:
            continue
    try:
        bridge_snapshot = bridge_state(root, limit=1)
        for key in ("outbox_path", "ack_path"):
            value = str(bridge_snapshot.get(key) or "")
            if value:
                created.append(str(Path(value).relative_to(root)))
    except Exception:
        pass
    try:
        TaskStatusStore(root).state()
    except Exception:
        pass
    return sorted(dict.fromkeys(created))


def _is_windows_locked_file_error(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32}


def _locked_history_record(relative: str, target: Path, exc: OSError) -> dict[str, Any]:
    return {
        "relative_path": relative,
        "path": str(target),
        "kind": "file",
        "reason": "locked_by_running_process",
        "error": f"{type(exc).__name__}: {exc}",
        "winerror": getattr(exc, "winerror", None),
    }


def _truncate_locked_history_file(target: Path, relative: str, record: dict[str, Any]) -> None:
    try:
        target.write_bytes(b"")
        record["fallback"] = "truncated"
    except OSError as exc:
        record["fallback"] = "retained"
        record["fallback_error"] = f"{type(exc).__name__}: {exc}"
        record["fallback_winerror"] = getattr(exc, "winerror", None)


def _schedule_sidebar_history_reset_shutdown(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    runtime_dir = root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    active = _active_sidebar_history_reset_shutdown(runtime_dir)
    if active:
        return active
    lock_path = runtime_dir / "history_reset_shutdown.lock"
    status_path = runtime_dir / "history_reset_shutdown.json"
    if not _try_acquire_sidebar_history_reset_shutdown_lock(lock_path):
        active = _active_sidebar_history_reset_shutdown(runtime_dir)
        if active:
            return active
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return _deduped_sidebar_history_reset_shutdown(lock_path, _read_json(status_path, {}))
        if not _try_acquire_sidebar_history_reset_shutdown_lock(lock_path):
            return _deduped_sidebar_history_reset_shutdown(lock_path, _read_json(status_path, {}))

    launch_state = _read_json(root / "runtime" / "sidebar_launch.json", {})
    launch_state = launch_state if isinstance(launch_state, dict) else {}
    weflow_result = launch_state.get("weflow_result") if isinstance(launch_state.get("weflow_result"), dict) else {}
    parent_pid = _int_value(payload.get("parent_pid") or payload.get("parentPid") or launch_state.get("pid"), os.getpid())
    weflow_pid = _int_value(
        payload.get("weflow_pid") or payload.get("weflowPid") or launch_state.get("weflow_pid") or weflow_result.get("pid"),
        0,
    )
    weflow_mode = str(payload.get("weflow") or launch_state.get("weflow") or "auto")
    if weflow_mode not in {"auto", "on", "off"}:
        weflow_mode = "auto"
    weflow_port = _int_value(payload.get("weflow_port") or payload.get("weflowPort") or launch_state.get("weflow_port"), 5031)
    helper = Path(__file__).resolve().parents[3] / "scripts" / "sidebar_history_reset_shutdown.py"
    command = [
        sys.executable,
        str(helper),
        "--data-dir",
        str(root),
        "--parent-pid",
        str(parent_pid),
        "--weflow",
        weflow_mode,
        "--weflow-port",
        str(weflow_port),
        "--weflow-pid",
        str(weflow_pid),
    ]
    status = {
        "status": "shutdown_scheduled",
        "phase": "scheduled",
        "parent_pid": parent_pid,
        "weflow_pid": weflow_pid,
        "weflow": weflow_mode,
        "weflow_port": weflow_port,
        "manual_reopen_required": True,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_json(status_path, status)
    stdout_path = runtime_dir / "history_reset_shutdown.out.log"
    stderr_path = runtime_dir / "history_reset_shutdown.err.log"
    creationflags = 0
    if os.name == "nt":
        creationflags = 0x00000008 | 0x00000200 | 0x08000000
    try:
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = subprocess.Popen(
                command,
                cwd=str(Path(__file__).resolve().parents[3]),
                stdout=stdout,
                stderr=stderr,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creationflags,
            )
    except Exception:
        _remove_sidebar_history_reset_shutdown_lock(lock_path)
        raise
    status["helper_pid"] = process.pid
    _write_json(status_path, status)
    _write_json(
        lock_path,
        {
            "helper_pid": process.pid,
            "owner_pid": os.getpid(),
            "updated_at_epoch": time.time(),
            "status_file": str(status_path),
        },
    )
    return {
        "status": "shutdown_scheduled",
        "message": "Sidebar and WeFlow will stop and clear history. Reopen the sidebar manually after it closes.",
        "helper_pid": process.pid,
        "parent_pid": parent_pid,
        "weflow_pid": weflow_pid,
        "manual_reopen_required": True,
        "shutdown_status_file": str(status_path),
    }


def _try_acquire_sidebar_history_reset_shutdown_lock(lock_path: Path) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "helper_pid": 0,
                "owner_pid": os.getpid(),
                "updated_at_epoch": time.time(),
            },
            handle,
            ensure_ascii=False,
        )
    return True


def _active_sidebar_history_reset_shutdown(runtime_dir: Path) -> dict[str, Any] | None:
    lock_path = runtime_dir / "history_reset_shutdown.lock"
    if not lock_path.exists():
        return None
    lock = _read_json(lock_path, {})
    status = _read_json(runtime_dir / "history_reset_shutdown.json", {})
    helper_pid = _int_value(
        (lock.get("helper_pid") if isinstance(lock, dict) else 0)
        or (status.get("helper_pid") if isinstance(status, dict) else 0),
        0,
    )
    lock_age = _shutdown_lock_age_seconds(lock if isinstance(lock, dict) else {})
    if helper_pid > 0 and _pid_exists(helper_pid):
        return _deduped_sidebar_history_reset_shutdown(lock_path, status if isinstance(status, dict) else {}, helper_pid=helper_pid)
    if helper_pid <= 0 and lock_age <= 20.0:
        return _deduped_sidebar_history_reset_shutdown(lock_path, status if isinstance(status, dict) else {}, helper_pid=0)
    _remove_sidebar_history_reset_shutdown_lock(lock_path)
    return None


def _deduped_sidebar_history_reset_shutdown(
    lock_path: Path,
    status: dict[str, Any],
    *,
    helper_pid: int = 0,
) -> dict[str, Any]:
    return {
        "status": "shutdown_scheduled",
        "message": "Sidebar and WeFlow shutdown/cleanup is already in progress.",
        "deduplicated": True,
        "helper_pid": helper_pid or _int_value(status.get("helper_pid") if isinstance(status, dict) else 0, 0),
        "parent_pid": _int_value(status.get("parent_pid") if isinstance(status, dict) else 0, 0),
        "weflow_pid": _int_value(status.get("weflow_pid") if isinstance(status, dict) else 0, 0),
        "phase": str(status.get("phase") or "scheduled") if isinstance(status, dict) else "scheduled",
        "manual_reopen_required": True,
        "shutdown_status_file": str(lock_path.with_name("history_reset_shutdown.json")),
    }


def _shutdown_lock_age_seconds(lock: dict[str, Any]) -> float:
    try:
        updated_at = float(lock.get("updated_at_epoch") or 0)
    except Exception:
        updated_at = 0.0
    if updated_at <= 0:
        return float("inf")
    return max(0.0, time.time() - updated_at)


def _remove_sidebar_history_reset_shutdown_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            return False
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _bridge_worker_alive(data_dir: str | Path) -> bool:
    """True if a send-bridge worker holds a fresh single-instance lock.

    Reads ``send_bridge/.bridge_worker.lock`` and treats the holder as alive
    when its heartbeat is within the lock's stale window. Covers both the
    in-process supervised worker and a separately-launched CLI worker, since
    both write the same lock file with periodic heartbeats.
    """
    lock_path = Path(data_dir) / "send_bridge" / ".bridge_worker.lock"
    if not lock_path.exists():
        return False
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    heartbeat = payload.get("heartbeat_at")
    if not isinstance(heartbeat, (int, float)):
        return False
    # run_bridge_worker uses stale_after_seconds=60; consider the worker alive
    # only if its heartbeat is fresher than that window.
    return (time.time() - float(heartbeat)) <= 60.0


def _background_send_status(config: Any, bridge: dict[str, Any], data_dir: str | Path | None = None) -> str:
    if str(getattr(config, "send_driver", "")) == "bridge_outbox":
        if not bool(getattr(config, "send_enabled", False)):
            return "bridge_outbox_configured_disabled"
        # send_enabled + bridge_outbox: replies are queued, but nothing is
        # delivered unless a worker is actually consuming the outbox. Report the
        # real worker liveness instead of a config-only "ready", and flag a
        # backlog that is piling up with no live worker to drain it.
        if data_dir is not None and not _bridge_worker_alive(data_dir):
            pending = int(bridge.get("pending_count", 0) or 0) if isinstance(bridge, dict) else 0
            if pending > 0:
                return "bridge_outbox_worker_down_backlog"
            return "bridge_outbox_worker_down"
        return "bridge_outbox_ready"
    return "bridge_outbox_available"


def _backend_event_file_path(data_dir: str | Path, payload: dict[str, Any]) -> Path:
    root = Path(data_dir).resolve()
    raw = str(payload.get("event_file") or payload.get("eventFile") or "").strip()
    path = Path(raw) if raw else root / "backend_events.jsonl"
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("event_file must stay inside data_dir")
    return resolved


def _build_agent_backend_driver(
    config: Any,
    runtime: Any,
    event_file: Path,
    *,
    extra_roots: list[str] | None = None,
) -> BackendEventJsonlDriver:
    extra_roots = list(extra_roots or [])
    roots = config.file_read_roots + config.wechat_voice_roots + extra_roots
    return BackendEventJsonlDriver(
        event_file,
        runtime.file_index,
        allowed_input_roots=resolve_allowed_roots(config.data_dir, roots),
        allowed_extensions=config.file_allowed_extensions,
        max_input_bytes=config.file_max_bytes,
        attachment_parser=BackendAttachmentParser(
            build_default_ocr_engine(mode=config.ocr_mode),
            LocalAsrSubprocessEngine(mode=config.asr_mode),
        ),
        file_workspace=runtime.file_workspace,
        session_store=runtime.session_store,
        voice_cache_resolver=_voice_cache_resolver(config, extra_roots=extra_roots),
    )


def _agent_session_snapshot(
    root: Path,
    *,
    runtime: Any,
    conversation_ids: list[str] | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    ids = _agent_conversation_ids(root, runtime=runtime, requested=conversation_ids or [])
    conversations = [_agent_conversation_snapshot(runtime, conversation_id, limit=limit) for conversation_id in ids[:50]]
    conversations = [item for item in conversations if item]
    return {
        "schema": "dialog_agent_session_snapshot_v1",
        "conversation_count": len(conversations),
        "entry_count": sum(int(item.get("entry_count", 0) or 0) for item in conversations),
        "pending_user_count": sum(int(item.get("pending_user_count_since_last_assistant", 0) or 0) for item in conversations),
        "topic_candidates": _agent_merged_topics(conversations),
        "conversations": conversations,
    }


def _agent_conversation_ids(root: Path, *, runtime: Any, requested: list[str]) -> list[str]:
    ids: list[str] = []
    ids.extend(requested)
    try:
        ids.extend(channel.conversation_id for channel in runtime.channel_store.list_channels())
    except Exception:
        pass
    ids.extend(_agent_conversation_ids_from_ledgers(root))
    return _dedupe_strings(ids)


def _agent_conversation_ids_from_ledgers(root: Path) -> list[str]:
    ids: list[str] = []
    ledger_root = root / "conversation_ledgers"
    if not ledger_root.exists():
        return ids
    for messages_jsonl in ledger_root.glob("*/messages.jsonl"):
        try:
            with messages_jsonl.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    if not raw_line.strip():
                        continue
                    payload = json.loads(raw_line)
                    if isinstance(payload, dict) and payload.get("conversation_id"):
                        ids.append(str(payload.get("conversation_id")))
                    break
        except (OSError, json.JSONDecodeError):
            continue
    return ids


def _agent_conversation_snapshot(runtime: Any, conversation_id: str, *, limit: int) -> dict[str, Any]:
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        return {}
    try:
        entries = [_dataclass_payload(entry) for entry in runtime.ledger_store.read_entries(conversation_id)]
    except Exception:
        entries = []
    try:
        session_id = runtime.session_store.current_session_id(conversation_id)
    except Exception:
        session_id = "session_default"
    session_entries = [
        entry
        for entry in entries
        if str(entry.get("session_id") or "session_default") == session_id
    ]
    markdown_path = runtime.ledger_store.conversation_markdown_path(conversation_id)
    recent = session_entries[-limit:]
    user_texts = [_agent_entry_text(entry) for entry in session_entries if str(entry.get("role") or "user") == "user"]
    assistant_texts = [_agent_entry_text(entry) for entry in session_entries if str(entry.get("role") or "") == "assistant"]
    pending_user_count = 0
    for entry in reversed(session_entries):
        role = str(entry.get("role") or "user")
        if role == "assistant":
            break
        if role == "user":
            pending_user_count += 1
    last_entry = session_entries[-1] if session_entries else {}
    return {
        "conversation_id": conversation_id,
        "conversation_type": str(last_entry.get("conversation_type") or ""),
        "chat_title": str(last_entry.get("chat_title") or ""),
        "session_id": session_id,
        "ledger_markdown": str(markdown_path),
        "ledger_messages": str(markdown_path.with_name("messages.jsonl")),
        "entry_count": len(session_entries),
        "total_entry_count": len(entries),
        "last_message_at": str(last_entry.get("received_at") or last_entry.get("updated_at") or ""),
        "last_user_message": _agent_compact_text(user_texts[-1] if user_texts else "", 240),
        "last_assistant_reply": _agent_compact_text(assistant_texts[-1] if assistant_texts else "", 240),
        "pending_user_count_since_last_assistant": pending_user_count,
        "topic_candidates": _agent_topic_candidates(user_texts[-10:]),
        "recent_turns": [
            {
                "role": str(entry.get("role") or "user"),
                "sender_name": str(entry.get("sender_name") or ""),
                "received_at": str(entry.get("received_at") or ""),
                "text": _agent_compact_text(_agent_entry_text(entry), 240),
                "attachment_count": len(entry.get("attachments") if isinstance(entry.get("attachments"), list) else []),
            }
            for entry in recent
        ],
    }


def _agent_processed_conversation_ids(result: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    processed = result.get("processed") if isinstance(result.get("processed"), list) else []
    for item in processed:
        if not isinstance(item, dict):
            continue
        message = item.get("message") if isinstance(item.get("message"), dict) else {}
        conversation_id = str(message.get("conversation_id") or "").strip()
        if conversation_id:
            ids.append(conversation_id)
    return _dedupe_strings(ids)


def _agent_entry_text(entry: dict[str, Any]) -> str:
    blocks = entry.get("text_blocks") if isinstance(entry.get("text_blocks"), list) else []
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _agent_topic_candidates(texts: list[str]) -> list[dict[str, Any]]:
    keyword_groups = [
        ("通道与 Agent 控制", ("agent", "weflow", "通道", "总台", "管道", "模型", "启动", "停止")),
        ("任务推进", ("任务", "计划", "推进", "残留", "下一步", "验收", "分发")),
        ("缺陷修复", ("bug", "问题", "报错", "卡住", "修复", "失败", "异常")),
        ("上下文与记忆", ("上下文", "记忆", "session", "会话", "聚合", "主题")),
        ("文件与媒体", ("文件", "附件", "图片", "语音", "文档", "ocr", "asr")),
    ]
    scores: dict[str, dict[str, Any]] = {}
    for text in texts:
        compact = _agent_compact_text(text, 320)
        lowered = compact.lower()
        for title, keywords in keyword_groups:
            hits = [keyword for keyword in keywords if keyword.lower() in lowered]
            if not hits:
                continue
            item = scores.setdefault(
                title,
                {"topic_id": _agent_topic_id(title), "title": title, "score": 0, "evidence": []},
            )
            item["score"] = int(item["score"]) + len(hits)
            if compact and len(item["evidence"]) < 2:
                item["evidence"].append(compact)
    if not scores and texts:
        fallback = _agent_compact_text(texts[-1], 28) or "日常闲聊"
        scores[fallback] = {
            "topic_id": _agent_topic_id(fallback),
            "title": fallback,
            "score": 1,
            "evidence": [_agent_compact_text(texts[-1], 160)],
        }
    return sorted(scores.values(), key=lambda item: int(item.get("score") or 0), reverse=True)[:5]


def _agent_merged_topics(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for conversation in conversations:
        topics = conversation.get("topic_candidates")
        if not isinstance(topics, list):
            continue
        for topic in topics:
            title = str(topic.get("title") or "").strip()
            if not title:
                continue
            current = merged.setdefault(
                title,
                {"topic_id": str(topic.get("topic_id") or _agent_topic_id(title)), "title": title, "score": 0, "conversations": []},
            )
            current["score"] = int(current["score"]) + int(topic.get("score") or 0)
            conversation_id = str(conversation.get("conversation_id") or "")
            if conversation_id and conversation_id not in current["conversations"]:
                current["conversations"].append(conversation_id)
    return sorted(merged.values(), key=lambda item: int(item.get("score") or 0), reverse=True)[:8]


def _agent_topic_id(title: str) -> str:
    return "topic-" + hashlib.sha256(str(title).encode("utf-8")).hexdigest()[:12]


def _agent_compact_text(text: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _key_pool(data_dir: str | Path) -> ApiKeyPool:
    config = ensure_config(data_dir)
    root = Path(data_dir)
    chat_provider = config.providers.get("chat", config.llm)
    return ApiKeyPool(chat_provider, root)


def _optional_positive_int(payload: dict[str, Any], *names: str) -> int | None:
    for name in names:
        if name not in payload or payload.get(name) in (None, ""):
            continue
        try:
            value = int(payload.get(name))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if value < 1 or value > 64:
            raise ValueError(f"{name} must be between 1 and 64")
        return value
    return None


def list_api_keys(data_dir: str | Path) -> dict[str, Any]:
    """List the LLM API key pool with masked previews (never raw values)."""
    pool = _key_pool(data_dir)
    keys = pool.describe()
    key_file = pool.key_file_path()
    return {
        "status": "ok",
        "keys": keys,
        "available_count": sum(1 for item in keys if item.get("available")),
        "key_file": str(key_file) if key_file else "",
        "key_file_writable": key_file is not None,
    }


def add_api_key(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Append a literal secret key to the pool file. Returns the new masked ref."""
    value = str(payload.get("value") or payload.get("key") or "").strip()
    name = str(payload.get("name") or "").strip() or None
    if not value:
        raise ValueError("value is required")
    pool = _key_pool(data_dir)
    ref = pool.add_key(value, name=name)
    if any(key in payload for key in ("provider", "model", "base_url", "baseUrl", "max_wait_seconds", "max_concurrency", "maxConcurrency", "enabled")):
        pool.set_key_model_config(
            ref.ref,
            provider=str(payload.get("provider")).strip().lower() if payload.get("provider") is not None else None,
            model=str(payload.get("model")).strip() if payload.get("model") is not None else None,
            base_url=str(payload.get("base_url") or payload.get("baseUrl") or "").strip()
            if payload.get("base_url") is not None or payload.get("baseUrl") is not None
            else None,
            max_wait_seconds=int(payload.get("max_wait_seconds"))
            if payload.get("max_wait_seconds") not in (None, "")
            else None,
            max_concurrency=_optional_positive_int(payload, "max_concurrency", "maxConcurrency"),
            enabled=bool(payload.get("enabled")) if payload.get("enabled") is not None else None,
        )
    return {"status": "ok", "ref": ref.ref, "source": ref.source, **list_api_keys(data_dir)}


def remove_api_key(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Remove a file-backed key from the pool by its anonymized ref."""
    ref = str(payload.get("ref") or "").strip()
    if not ref:
        raise ValueError("ref is required")
    pool = _key_pool(data_dir)
    removed = pool.remove_key(ref)
    if not removed:
        raise ValueError("key not found or not file-backed")
    return {"status": "ok", "removed": ref, **list_api_keys(data_dir)}


def _key_refs_inheriting_provider_concurrency(data_dir: str | Path) -> list[str]:
    config = ensure_config(data_dir)
    provider = config.providers.get("chat", config.llm)
    old_limit = int(provider.max_concurrency or DEFAULT_LLM_MAX_CONCURRENCY)
    refs: list[str] = []
    for item in _key_pool(data_dir).describe():
        model_config = item.get("model_config") if isinstance(item.get("model_config"), dict) else {}
        raw_limit = model_config.get("max_concurrency")
        try:
            key_limit = int(raw_limit) if raw_limit not in (None, "") else old_limit
        except (TypeError, ValueError):
            key_limit = old_limit
        if key_limit == old_limit:
            refs.append(str(item.get("ref") or ""))
    return [ref for ref in refs if ref]


# Provider request formats the model-config panel can select. Both are
# OpenAI-compatible; the value drives normalize_openai_base_url's /v1 handling.
_MODEL_PROVIDER_FORMATS = ["deepseek", "relay"]


def get_model_config(data_dir: str | Path) -> dict[str, Any]:
    """Return the current chat provider's model/endpoint/format for the panel."""
    config = ensure_config(data_dir)
    provider = config.providers.get("chat", config.llm)
    pool = _key_pool(data_dir)
    keys = pool.describe()
    return {
        "status": "ok",
        "provider": provider.provider,
        "model": provider.model,
        "base_url": provider.base_url,
        "api_key_env": provider.api_key_env,
        "max_wait_seconds": provider.max_wait_seconds,
        "max_concurrency": provider.max_concurrency,
        "effective_concurrency_limit": pool.concurrency_limit(),
        "recommended_max_concurrency": DEFAULT_LLM_MAX_CONCURRENCY,
        "provider_formats": list(_MODEL_PROVIDER_FORMATS),
        "key_pool_available_count": pool.available_count(),
        "keys": keys,
        "key_model_configs": {str(item.get("ref")): item.get("model_config", {}) for item in keys},
        "config_scope": "default_profile_and_per_key_overrides",
        "async_summary_follows_chat": True,
    }


def set_model_config(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist edited model/endpoint/format for the chat provider."""
    ensure_config(data_dir)
    key_ref = str(payload.get("ref") or payload.get("key_ref") or payload.get("keyRef") or "").strip()
    provider = payload.get("provider")
    if provider is not None:
        provider = str(provider).strip().lower()
        if provider not in _MODEL_PROVIDER_FORMATS:
            raise ValueError(f"provider must be one of {_MODEL_PROVIDER_FORMATS}")
    model = payload.get("model")
    if model is not None and not str(model).strip():
        raise ValueError("model must not be empty")
    base_url = payload.get("base_url")
    api_key_env = payload.get("api_key_env")
    max_wait = payload.get("max_wait_seconds")
    max_concurrency = _optional_positive_int(payload, "max_concurrency", "maxConcurrency")
    if key_ref:
        pool = _key_pool(data_dir)
        updated_key = pool.set_key_model_config(
            key_ref,
            provider=provider,
            model=str(model).strip() if model is not None else None,
            base_url=str(base_url).strip() if base_url is not None else None,
            api_key_env=str(api_key_env).strip() if api_key_env is not None else None,
            max_wait_seconds=int(max_wait) if max_wait not in (None, "") else None,
            max_concurrency=max_concurrency,
            enabled=bool(payload.get("enabled")) if payload.get("enabled") is not None else None,
        )
        return {
            "status": "ok",
            "ref": key_ref,
            "key_model_config": asdict(updated_key),
            "model_config": get_model_config(data_dir),
            "model": updated_key.model,
        }
    key_refs_to_sync = _key_refs_inheriting_provider_concurrency(data_dir) if max_concurrency is not None else []
    updated = set_model_provider(
        data_dir,
        provider=provider,
        model=str(model).strip() if model is not None else None,
        base_url=str(base_url).strip() if base_url is not None else None,
        api_key_env=str(api_key_env).strip() if api_key_env is not None else None,
        max_wait_seconds=int(max_wait) if max_wait not in (None, "") else None,
        max_concurrency=max_concurrency,
    )
    if key_refs_to_sync and max_concurrency is not None:
        pool = _key_pool(data_dir)
        for ref in key_refs_to_sync:
            try:
                pool.set_key_model_config(ref, max_concurrency=max_concurrency)
            except ValueError:
                continue
    return {"status": "ok", "model_config": get_model_config(data_dir), "model": updated.model}


def probe_model_fetch(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Test-fetch the model list from the provider (GET /v1/models).

    Uses the panel's supplied base_url/provider (falling back to saved config)
    plus a key from the pool. Read-only, no token cost. Returns the available
    model ids, and whether the currently-configured model is among them.
    """
    from app.personal_wechat_bot.llm.openai_client import DEFAULT_USER_AGENT, normalize_openai_base_url

    config = ensure_config(data_dir)
    provider_cfg = config.providers.get("chat", config.llm)
    key_ref = str(payload.get("ref") or payload.get("key_ref") or payload.get("keyRef") or "").strip()
    pool = _key_pool(data_dir)
    if key_ref:
        provider_cfg = pool.provider_for_ref(key_ref)
    base_url = str(payload.get("base_url") or provider_cfg.base_url or "").strip()
    provider = str(payload.get("provider") or provider_cfg.provider or "relay").strip().lower()
    target_model = str(payload.get("model") or provider_cfg.model or "").strip()
    if not base_url:
        raise ValueError("base_url is required to probe models")
    # Validate the target BEFORE attaching the live API key, so a malformed or
    # non-http(s) base_url can never cause the key to egress somewhere unexpected
    # (e.g. file://, ftp://, or a schemeless string).
    url = normalize_openai_base_url(base_url, provider) + "/models"
    url_error = _validate_probe_url(url)
    if url_error:
        return {"status": "error", "reachable": False, "error": url_error, "url": url}
    api_key = pool.key_for_ref(key_ref) if key_ref else pool.default_key()
    if not api_key:
        return {
            "status": "error",
            "reachable": False,
            "error": "no_api_key_available",
            "hint": "先在密钥池中添加至少一个可用密钥",
        }
    import json as _json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": DEFAULT_USER_AGENT,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            # Cap the read: this hits a self-supplied URL, so don't let a hostile
            # or misbehaving endpoint stream an unbounded body into memory.
            body = response.read(2_000_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return {"status": "error", "reachable": True, "http_status": exc.code, "error": f"http_{exc.code}", "url": url}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"status": "error", "reachable": False, "error": f"{type(exc).__name__}: {exc}", "url": url}
    try:
        parsed = _json.loads(body)
    except _json.JSONDecodeError:
        return {"status": "error", "reachable": True, "error": "invalid_json_response", "url": url}
    models = _extract_model_ids(parsed)
    return {
        "status": "ok",
        "reachable": True,
        "url": url,
        "model_count": len(models),
        "models": models[:200],
        "configured_model": target_model,
        "configured_model_available": target_model in models if target_model else None,
    }


def _validate_probe_url(url: str) -> str:
    """Return an error string if the probe URL is unsafe to send the key to.

    Delegates to the shared endpoint validator so the model-probe path and the
    live chat send path enforce the same rule (http/https + real host) before
    the API key is attached — a bad base_url can never leak the key to file://,
    ftp://, or a schemeless/hostless destination.
    """
    from app.personal_wechat_bot.llm.openai_client import validate_endpoint_url

    return validate_endpoint_url(url)


def _extract_model_ids(payload: Any) -> list[str]:
    data = payload.get("data") if isinstance(payload, dict) else None
    items = data if isinstance(data, list) else (payload if isinstance(payload, list) else [])
    models: list[str] = []
    for item in items:
        if isinstance(item, dict):
            model_id = str(item.get("id") or item.get("model") or "").strip()
        else:
            model_id = str(item).strip()
        if model_id:
            models.append(model_id)
    return models


def _channel_store(data_dir: str | Path) -> ConversationChannelStore:
    config = ensure_config(data_dir)
    root = Path(data_dir)
    chat_provider = config.providers.get("chat", config.llm)
    key_pool = ApiKeyPool(chat_provider, root)
    return ConversationChannelStore(
        root,
        key_pool,
        file_workspace_root=root / "file_workspace",
        context_root=root / "conversation_ledgers",
    )


def _channel_state(data_dir: str | Path) -> dict[str, Any]:
    config = ensure_config(data_dir)
    root = Path(data_dir)
    channel_root = root / "conversation_channels"
    channel_items = _channel_store(root).list_channels() if channel_root.exists() else []
    task_state = build_sidebar_task_manager(root)
    task_groups = _tasks_by_conversation(task_state.get("tasks", []))
    ledger_store = ConversationLedgerStore(root) if channel_items and (root / "conversation_ledgers").exists() else None
    state_path = root / "channel_state.sqlite"
    state_store = ChannelStateStore(root) if channel_items or state_path.exists() else None
    if not channel_items:
        persisted_states = state_store.list_states() if state_store is not None else []
        return {
            "status": "ok",
            "policy": "auto_accept_wechat_contacts_and_groups",
            "count": 0,
            "total_count": 0,
            "state_schema": "channel_state_v1",
            "state_storage": str(state_path),
            "states": sorted(persisted_states, key=lambda item: item.get("updated_at", ""), reverse=True),
            "hidden_count": 0,
            "hidden_reasons": {},
            "private_count": 0,
            "group_count": 0,
            "items": [],
            "hidden_items": [],
            "hidden_items_all": [],
        }
    visible_policy = _visible_channel_policy(root, config)
    channels = []
    hidden = []
    state_records = []
    for channel in channel_items:
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
            "conversation_key": channel.conversation_key,
            "segment": channel.segment,
            "source_names": channel.source_names,
            "trusted_channel_source": channel.trusted_channel_source,
            "updated_at": channel.updated_at,
        }
        projected_state = build_channel_state_projection(
            channel=payload,
            tasks=task_groups.get(channel.conversation_id, []),
            ledger_entries=_ledger_payloads(ledger_store, channel.conversation_id),
        ).to_dict()
        existing_state = state_store.get(channel.conversation_id) if state_store is not None else None
        channel_state = merge_channel_state_projection(projected_state, existing_state)
        state_records.append(channel_state)
        payload["state"] = channel_state
        if visible:
            channels.append(payload)
        else:
            hidden.append({**payload, "hidden_reason": reason})
    persisted_states = state_store.replace_all(state_records) if state_store is not None else []
    return {
        "status": "ok",
        "policy": "auto_accept_wechat_contacts_and_groups",
        "count": len(channels),
        "total_count": len(channels) + len(hidden),
        "state_schema": "channel_state_v1",
        "state_storage": str(state_path),
        "states": sorted(persisted_states, key=lambda item: item.get("updated_at", ""), reverse=True),
        "hidden_count": len(hidden),
        "hidden_reasons": _reason_counts(hidden),
        "private_count": sum(1 for item in channels if item["conversation_type"] == "private"),
        "group_count": sum(1 for item in channels if item["conversation_type"] == "group"),
        "items": sorted(channels, key=lambda item: item.get("updated_at", ""), reverse=True),
        "hidden_items": sorted(hidden, key=lambda item: item.get("updated_at", ""), reverse=True)[:20],
        "hidden_items_all": sorted(hidden, key=lambda item: item.get("updated_at", ""), reverse=True),
    }


def _tasks_by_conversation(tasks: Any) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(tasks, list):
        return grouped
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if _is_non_channel_lane_task(task):
            continue
        conversation_id = str(task.get("conversation_id") or task.get("conversationId") or "").strip()
        if not conversation_id:
            continue
        grouped.setdefault(conversation_id, []).append(dict(task))
    return grouped


def _is_non_channel_lane_task(task: dict[str, Any]) -> bool:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    if metadata.get("local_ui") is True:
        return True
    scope = str(task.get("concurrency_key") or task.get("scope") or "")
    return scope.startswith(
        (
            "diagnostic:",
            "agent:",
            "ui:",
            "weflow:",
            "queue:",
            "send-review:",
            "settings:",
            "audit:",
            "history:",
            "channels:",
        )
    )


def _ledger_payloads(ledger_store: ConversationLedgerStore | None, conversation_id: str) -> list[dict[str, Any]]:
    if ledger_store is None:
        return []
    try:
        entries = ledger_store.read_entries(conversation_id)
    except Exception:
        return []
    payloads: list[dict[str, Any]] = []
    for entry in entries:
        try:
            payloads.append(asdict(entry))
        except Exception:
            if isinstance(entry, dict):
                payloads.append(dict(entry))
    return payloads


def _sidebar_bridge_state(
    data_dir: str | Path,
    *,
    channels_state: dict[str, Any] | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    state = bridge_state(data_dir, limit=limit)
    channels_state = channels_state if isinstance(channels_state, dict) else _channel_state(data_dir)
    channels = channels_state.get("items") if isinstance(channels_state.get("items"), list) else []
    bridge_channels = [_bridge_channel_payload(item) for item in channels if isinstance(item, dict)]
    state["channels"] = bridge_channels
    state["channel_count"] = len(bridge_channels)
    state["contract"] = {
        **(state.get("contract") if isinstance(state.get("contract"), dict) else {}),
        "channel_sync": "visible service channels are projected into send_bridge.channels; outbox records carry receiver wxid/roomid from the channel registry when available",
    }
    return state


def _bridge_channel_payload(channel: dict[str, Any]) -> dict[str, Any]:
    conversation_id = str(channel.get("conversation_id") or "").strip()
    conversation_type = str(channel.get("conversation_type") or "")
    sender_ids = channel.get("sender_wechat_ids") if isinstance(channel.get("sender_wechat_ids"), list) else []
    conversation_key = str(channel.get("conversation_key") or "").strip()
    # Mirror bridge_send._channel_receiver so the panel shows the true receiver:
    # prefer the persisted talker id; for groups only a @chatroom id is valid
    # (a member wxid would misroute the reply privately).
    if _looks_like_wechat_receiver(conversation_key):
        receiver = conversation_key
    elif conversation_type == "group":
        receiver = next((str(item).strip() for item in sender_ids if str(item).strip().endswith("@chatroom")), "")
        if not receiver and conversation_id.endswith("@chatroom"):
            receiver = conversation_id
    else:
        receiver = next((str(item).strip() for item in sender_ids if _looks_like_wechat_receiver(str(item).strip())), "")
        if not receiver and _looks_like_wechat_receiver(conversation_id):
            receiver = conversation_id
    return {
        "conversation_id": conversation_id,
        "conversation_type": conversation_type,
        "display_name": str(channel.get("chat_title") or ""),
        "receiver": receiver,
        "bridge_ready": bool(receiver),
        "updated_at": str(channel.get("updated_at") or ""),
        "source_names": channel.get("source_names") if isinstance(channel.get("source_names"), list) else [],
    }


def _looks_like_wechat_receiver(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text.startswith("wxid_") or text.startswith("gh_") or text.endswith("@chatroom"))


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

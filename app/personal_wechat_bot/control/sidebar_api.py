from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.conversation.channel_store import ConversationChannelStore
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.config.loader import load_config, migrate_file_allowed_extensions, set_model_provider
from app.personal_wechat_bot.domain.models import NormalizedMessage, utc_now_iso
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool
from app.personal_wechat_bot.normalizer.normalizer import conversation_id_for
from app.personal_wechat_bot.persona.runtime_cards import RuntimeCardStore
from app.personal_wechat_bot.reply_gate.send_executor import GuardedSendExecutor
from app.personal_wechat_bot.runtime.hook_pull_runner import HookMessagePullRunner
from app.personal_wechat_bot.runtime.polling_runner import PollingRunner
from app.personal_wechat_bot.runtime.process_lock import ProcessLock, ProcessLockError, blocking_process_lock
from app.personal_wechat_bot.runtime.weflow_state_summary import summarize_weflow_bridge_state
from app.personal_wechat_bot.runtime.weflow_worker_metrics import WeflowWorkerMetrics
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
    send_approved_confirm_item,
    set_send_controls,
    sync_bridge_ack_to_send_state,
)
from app.personal_wechat_bot.control.send_readiness import build_send_readiness_report
from app.personal_wechat_bot.wechat_driver.window_introspection import build_wechat_window_probe
from app.personal_wechat_bot.wechat_driver.window_binding import WeChatWindowBindingStore
from app.personal_wechat_bot.wechat_driver.backend_events import BackendEventJsonlDriver
from app.personal_wechat_bot.wechat_driver.backend_events import append_backend_event_payload
from app.personal_wechat_bot.wechat_driver.bridge_send import bridge_ack, bridge_state, is_terminal_bridge_ack_status
from app.personal_wechat_bot.wechat_driver.hook_events import HookEventJsonlImporter
from app.personal_wechat_bot.wechat_driver.system_accounts import is_system_account
from app.personal_wechat_bot.wechat_driver.hook_source_bridge import (
    WeFlowHttpBridge,
    require_weflow_ready,
    weflow_health_status,
)
from app.personal_wechat_bot.wechat_driver.voice_cache_resolver import WeChatVoiceCacheResolver


QUEUE_STATUSES = ("pending", "approved", "queued_to_bridge", "rejected", "sent", "failed")
logger = logging.getLogger(__name__)
_WEFLOW_WORKERS: dict[str, dict[str, Any]] = {}
_WEFLOW_BACKFILL_JOBS: dict[str, dict[str, Any]] = {}
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
    config = load_config(data_dir)
    queues = {status: list_confirm_queue(data_dir, status=status) for status in QUEUE_STATUSES}
    channels = _channel_state(data_dir)
    send_bridge = _sidebar_bridge_state(data_dir, channels_state=channels, limit=12)
    return {
        "status": "ok",
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
        },
        "channels": channels,
        "runtime_cards": build_sidebar_runtime_cards(data_dir),
        "queues": queues,
        "readiness": build_send_readiness_report(data_dir),
        "driver_probe": probe_send_controls(data_dir)["probe"],
        "send_bridge": send_bridge,
        "weflow": build_sidebar_weflow_state(data_dir),
        "wechat_window_probe": build_wechat_window_probe(max_children=80, max_controls=160, data_dir=data_dir),
        "audit": list_send_audit(data_dir, limit=30),
    }


def build_sidebar_wechat_probe(data_dir: str | Path = "data") -> dict[str, Any]:
    return build_wechat_window_probe(data_dir=data_dir)


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


def build_sidebar_bridge_state(data_dir: str | Path = "data") -> dict[str, Any]:
    return _sidebar_bridge_state(data_dir, limit=50)


def clear_sidebar_send_audit(data_dir: str | Path) -> dict[str, Any]:
    return clear_send_audit(data_dir)


def build_sidebar_weflow_state(data_dir: str | Path = "data") -> dict[str, Any]:
    root = Path(data_dir)
    persisted = _read_json(root / "weflow_sidebar_state.json", {})
    worker = _weflow_worker_state(root)
    cached_sessions = _weflow_cached_sessions_from_channels(root, limit=200)
    try:
        migration = migrate_file_allowed_extensions(root)
    except Exception as exc:
        migration = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "ok",
        "base_url": str(persisted.get("base_url") or "http://127.0.0.1:5031"),
        "token_env": str(persisted.get("token_env") or "WEFLOW_API_TOKEN"),
        "token_present": bool(persisted.get("token_present")),
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
        "backfill_job": _weflow_backfill_job_state(root, persisted),
        "bridge_state": summarize_weflow_bridge_state(root / "weflow_bridge_state.json"),
        "last_health": persisted.get("last_health", {}) if isinstance(persisted, dict) else {},
        "last_discover": persisted.get("last_discover", {}) if isinstance(persisted, dict) else {},
        "discovered_sessions": {
            "status": "ok",
            "source": "channel_store",
            "count": len(cached_sessions),
            "sessions": cached_sessions,
        },
        "last_pull": persisted.get("last_pull", {}) if isinstance(persisted, dict) else {},
        "last_backfill": persisted.get("last_backfill", {}) if isinstance(persisted, dict) else {},
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
    _write_weflow_sidebar_state(data_dir, {"last_health": result, **_weflow_public_params(params)})
    _append_weflow_operation_history(data_dir, "health", result)
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
        result = {
            "status": "ok",
            "source": "weflow_live",
            "sessions": sessions,
            "count": len(sessions),
            **registration,
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
        cached_sessions = _weflow_cached_sessions_from_channels(data_dir, limit=limit)
        if cached_sessions:
            result = {
                "status": "ok",
                "source": "channel_store_cache",
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
    result = _run_sidebar_weflow_once(data_dir, payload)
    _write_weflow_sidebar_state(data_dir, {"last_pull": result, **_weflow_public_params(_weflow_params(data_dir, payload))})
    _append_weflow_operation_history(data_dir, "pull-once", result)
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
    _write_weflow_sidebar_state(
        data_dir,
        {
            "last_backfill": result,
            "last_error": "" if result.get("status") != "error" else str(result.get("error") or ""),
            **_weflow_public_params(_weflow_params(data_dir, backfill_payload)),
        },
    )
    _append_weflow_operation_history(data_dir, "backfill", result)
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
        job_snapshot = _public_backfill_job(job)

    result = {
        "status": "started",
        "message": "WeFlow history backfill started",
        "backfill_job": job_snapshot,
        "backfilled_talkers": talkers,
    }
    _write_weflow_sidebar_state(
        data_dir,
        {"last_backfill": result, "backfill_job": result["backfill_job"], **_weflow_public_params(_weflow_params(data_dir, backfill_payload))},
    )
    _append_weflow_operation_history(data_dir, "backfill-start", result)
    thread.start()
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
    result = {
        "status": "ok",
        "worker": worker_state,
        "message": "WeFlow 后台拉取已停止" if not worker_state.get("running") else "WeFlow 后台拉取停止信号已发送，当前 tick 会先收尾",
    }
    _append_weflow_operation_history(data_dir, "stop", result)
    return result


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
    completed = subprocess.run(
        [str(python_executable), "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return completed.returncode == 0


def sidebar_weflow_install_deps(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    if not bool(payload.get("confirm_install", False)):
        raise ValueError("confirm_install=true is required")
    requirements = Path("requirements-ocr.txt").resolve()
    with _weflow_exclusive_operation(data_dir, label="weflow_install_deps", wait_timeout_seconds=1200.0):
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        result = {
            "status": "ok" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
            "dependencies": _weflow_dependency_status_snapshot(),
            "exclusive_operation": True,
        }
    _append_weflow_operation_history(data_dir, "install-deps", result)
    return result


def _weflow_dependency_status_snapshot() -> dict[str, Any]:
    modules = {
        "PyMuPDF": "fitz",
        "pypdf": "pypdf",
        "pdfminer.six": "pdfminer",
        "openpyxl": "openpyxl",
    }
    items = [
        {"package": package, "module": module, "runtime": "main_python", "available": find_spec(module) is not None}
        for package, module in modules.items()
    ]
    rapidocr_python = Path("vendor/ocr-python/Scripts/python.exe")
    items.append(
        {
            "package": "rapidocr-onnxruntime",
            "module": "rapidocr_onnxruntime",
            "runtime": str(rapidocr_python),
            "available": _subprocess_module_available(rapidocr_python, "rapidocr_onnxruntime"),
        }
    )
    return {
        "status": "ok" if all(item["available"] for item in items) else "missing_optional",
        "items": items,
        "requirements": str(Path("requirements-ocr.txt").resolve()),
    }


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
    if action == "send-approved":
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
    config = load_config(data_dir)
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
        PollingRunner(runtime, driver, poll_interval_seconds=0),
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
        new_config = load_config(data_dir)
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
                pull = runner.run_once()
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
                result = _run_weflow_pull_tick(context)
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
    token = str(payload.get("token") or "").strip() or _env_value(token_env) or _env_value("WEFLOW_API_TOKEN")
    since_value = payload.get("since")
    since = int(since_value) if since_value not in (None, "") else None
    return {
        "base_url": str(payload.get("base_url") or payload.get("baseUrl") or "http://127.0.0.1:5031").strip(),
        "token": token,
        "token_env": token_env,
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
        "context_only": bool(payload.get("context_only") or payload.get("contextOnly") or (since is not None and since <= 0)),
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
    name = str(
        session.get("name")
        or session.get("displayName")
        or session.get("display_name")
        or session.get("remark")
        or session.get("nickName")
        or session.get("nickname")
        or session.get("groupName")
        or session.get("group_name")
        or session_id
    ).strip()
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


def _weflow_backfill_job_loop(root: Path, payload: dict[str, Any], job_id: str, stop_event: threading.Event) -> None:
    def progress(update: dict[str, Any]) -> None:
        _update_backfill_job(root, job_id, progress=update, force=False)

    try:
        _update_backfill_job(root, job_id, status="running", progress={"event": "started"}, force=True)
        result = _run_sidebar_weflow_once(root, payload, cancel_event=stop_event, progress_callback=progress)
        result = {**result, "backfill": True, "backfilled_talkers": payload.get("talkers", [])}
        if stop_event.is_set() and result.get("status") != "cancelled":
            result = {**result, "status": "cancelled"}
        final_status = "cancelled" if result.get("status") == "cancelled" else "completed"
        _update_backfill_job(
            root,
            job_id,
            status="finalizing",
            result=result,
            progress={
                "scanned_count": result.get("source", {}).get("scanned_count", 0),
                "appended_count": result.get("source", {}).get("appended_count", 0),
                "processed_count": result.get("pull", {}).get("processed_count", 0),
                "event": final_status,
            },
            force=True,
        )
        _write_weflow_sidebar_state(
            root,
            {"last_backfill": result, "last_error": "", "backfill_job": _weflow_backfill_job_state(root), **_weflow_public_params(_weflow_params(root, payload))},
        )
        _append_weflow_operation_history(root, "backfill", result)
        _update_backfill_job(root, job_id, status=final_status, progress={"event": final_status}, force=True)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        result = {"status": "error", "error": error, "backfill": True, "backfilled_talkers": payload.get("talkers", [])}
        _update_backfill_job(root, job_id, status="error", result=result, last_error=error, force=True)
        _write_weflow_sidebar_state(
            root,
            {"last_backfill": result, "last_error": error, "backfill_job": _weflow_backfill_job_state(root), **_weflow_public_params(_weflow_params(root, payload))},
        )
        _append_weflow_operation_history(root, "backfill", result)


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
    with _WEFLOW_LOCK:
        job = _WEFLOW_BACKFILL_JOBS.get(key)
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
        snapshot = _public_backfill_job(job)
    if should_write:
        _write_weflow_sidebar_state(root, {"backfill_job": snapshot})


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
    running = _thread_alive(job.get("thread"))
    status = str(job.get("status") or ("running" if running else "idle"))
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


def _key_pool(data_dir: str | Path) -> ApiKeyPool:
    config = load_config(data_dir)
    root = Path(data_dir)
    chat_provider = config.providers.get("chat", config.llm)
    return ApiKeyPool(chat_provider, root)


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


# Provider request formats the model-config panel can select. Both are
# OpenAI-compatible; the value drives normalize_openai_base_url's /v1 handling.
_MODEL_PROVIDER_FORMATS = ["deepseek", "relay"]


def get_model_config(data_dir: str | Path) -> dict[str, Any]:
    """Return the current chat provider's model/endpoint/format for the panel."""
    config = load_config(data_dir)
    provider = config.providers.get("chat", config.llm)
    pool = _key_pool(data_dir)
    return {
        "status": "ok",
        "provider": provider.provider,
        "model": provider.model,
        "base_url": provider.base_url,
        "api_key_env": provider.api_key_env,
        "max_wait_seconds": provider.max_wait_seconds,
        "provider_formats": list(_MODEL_PROVIDER_FORMATS),
        "key_pool_available_count": pool.available_count(),
        "async_summary_follows_chat": True,
    }


def set_model_config(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist edited model/endpoint/format for the chat provider."""
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
    updated = set_model_provider(
        data_dir,
        provider=provider,
        model=str(model).strip() if model is not None else None,
        base_url=str(base_url).strip() if base_url is not None else None,
        api_key_env=str(api_key_env).strip() if api_key_env is not None else None,
        max_wait_seconds=int(max_wait) if max_wait not in (None, "") else None,
    )
    return {"status": "ok", "model_config": get_model_config(data_dir), "model": updated.model}


def probe_model_fetch(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Test-fetch the model list from the provider (GET /v1/models).

    Uses the panel's supplied base_url/provider (falling back to saved config)
    plus a key from the pool. Read-only, no token cost. Returns the available
    model ids, and whether the currently-configured model is among them.
    """
    from app.personal_wechat_bot.llm.openai_client import DEFAULT_USER_AGENT, normalize_openai_base_url

    config = load_config(data_dir)
    provider_cfg = config.providers.get("chat", config.llm)
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
    api_key = _key_pool(data_dir).default_key()
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
    config = load_config(data_dir)
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
    config = load_config(data_dir)
    root = Path(data_dir)
    store = _channel_store(root)
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
            "conversation_key": channel.conversation_key,
            "segment": channel.segment,
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
        "hidden_items_all": sorted(hidden, key=lambda item: item.get("updated_at", ""), reverse=True),
    }


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

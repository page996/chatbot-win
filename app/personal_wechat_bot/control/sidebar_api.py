from __future__ import annotations

import base64
import json
import hashlib
import logging
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.config.schema import DEFAULT_LLM_MAX_CONCURRENCY
from app.personal_wechat_bot.conversation.channel_admission import (
    channel_admission_for_session,
    channel_allows_private_receiver,
)
from app.personal_wechat_bot.conversation.channel_store import CHANNEL_POLICY, ConversationChannelStore
from app.personal_wechat_bot.conversation.channel_state_store import (
    ChannelStateStore,
    build_channel_state_projection,
    merge_channel_state_projection,
)
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.config.loader import (
    config_update_lock,
    ensure_config,
    load_config,
    set_model_provider,
    update_config,
)
from app.personal_wechat_bot.control.audit import (
    DISPOSABLE_ARTIFACTS,
    DISPOSABLE_DIRECTORIES,
    build_storage_migration_status,
)
from app.personal_wechat_bot.control.sidebar_browser_runtime import sidebar_browser_runtime_blockers
from app.personal_wechat_bot.domain.errors import ConfigError
from app.personal_wechat_bot.domain.models import NormalizedMessage, ReplyCandidate, SpeakDecision, utc_now_iso
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool
from app.personal_wechat_bot.logging.jsonl_rotation import DEFAULT_KEEP as JSONL_ROTATION_KEEP
from app.personal_wechat_bot.normalizer.normalizer import conversation_id_for
from app.personal_wechat_bot.persona.runtime_cards import RuntimeCardStore
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue, ConfirmQueueClaimConflict
from app.personal_wechat_bot.reply_gate.send_audit import SendAuditLog
from app.personal_wechat_bot.reply_gate.send_executor import (
    GuardedSendExecutor,
    send_result_bridge_ids,
    send_result_non_bridge_part_statuses,
)
from app.personal_wechat_bot.runtime.hook_pull_runner import HookMessagePullRunner
from app.personal_wechat_bot.runtime.polling_runner import PollingRunner
from app.personal_wechat_bot.runtime.process_lock import (
    ProcessLock,
    ProcessLockError,
    blocking_process_lock,
    process_pid_alive,
    process_start_marker,
    short_process_lock,
)
from app.personal_wechat_bot.runtime.history_fence import (
    HistoryWriterLease,
    active_history_writer_leases,
    history_writer_fence,
    history_writer_fence_if_owned,
    history_writer_lease_if_owned,
    register_history_writer_lease_if_owned,
)
from app.personal_wechat_bot.runtime.resource_governor import audit_local_resources
from app.personal_wechat_bot.runtime.resource_gate import gpu_gate_snapshot, llm_gate_snapshot
from app.personal_wechat_bot.runtime.resource_scheduler import ResourceScheduler
from app.personal_wechat_bot.runtime.send_bridge_worker import (
    BRIDGE_WORKER_LOCK_STALE_SECONDS,
    bridge_worker_config_signature,
    bridge_worker_lock_alive,
    bridge_worker_lock_path,
)
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
    retry_bridge_item,
    send_approved_confirm_item,
    set_send_controls,
    sync_bridge_ack_to_send_state,
)
from app.personal_wechat_bot.control.send_readiness import build_send_readiness_report
from app.personal_wechat_bot.control.sidebar_state_store import SidebarStateStore
from app.personal_wechat_bot.wechat_driver.window_introspection import build_wechat_window_probe
from app.personal_wechat_bot.wechat_driver.backend_events import BackendEventJsonlDriver
from app.personal_wechat_bot.wechat_driver.backend_events import append_backend_event_payload
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import BackendAttachmentParser
from app.personal_wechat_bot.wechat_driver.bridge_send import (
    BridgeOutboxStore,
    bridge_ack_if_queued,
    bridge_state,
    effective_bridge_ack_states,
    is_terminal_bridge_ack_status,
)
from app.personal_wechat_bot.wechat_driver.send_backends import (
    wechat_native_http_status,
    weflow_http_status,
)
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


QUEUE_STATUSES = ("pending", "approved", "queued_to_bridge", "accepted", "rejected", "sent", "failed")
AUDIT_PHASES = QUEUE_STATUSES + ("blocked", "resolved", "other")
BRIDGE_ITEM_STATUSES = ("queued", "inflight", "accepted", "sent", "failed", "blocked", "retry")
_WEFLOW_HEALTH_TTL_SECONDS = 60.0
_HISTORY_RESET_DIRS = (
    "agent_workspace",
    "conversation_channels",
    "conversation_ledgers",
    "conversation_sessions",
    "diagnostics",
    "file_workspace",
    "native_diagnostics",
    "tool_outputs",
    "task_manager",
    *sorted(
        relative
        for relative in DISPOSABLE_DIRECTORIES
        if len(Path(relative).parts) == 1
    ),
)
_HISTORY_RESET_NESTED_DIRS = tuple(
    sorted(
        relative
        for relative in DISPOSABLE_DIRECTORIES
        if len(Path(relative).parts) > 1
    )
)
_HISTORY_RESET_SQLITE_FILES = (
    "backend_file_watcher.sqlite",
    "confirm_queue.sqlite",
    "conversation_cooldowns.sqlite",
    "channel_state.sqlite",
    "conversation_channels.sqlite",
    "conversation_ledger.sqlite",
    "conversation_sessions.sqlite",
    "file_index.sqlite",
    "processed_messages.sqlite",
    "scheduler.sqlite",
    "send_audit.sqlite",
)
_HISTORY_RESET_SQLITE_PATHS = tuple(
    f"{relative}{suffix}"
    for relative in _HISTORY_RESET_SQLITE_FILES
    for suffix in ("", "-wal", "-shm", "-journal")
)
_HISTORY_RESET_ROTATED_LOG_PATHS = tuple(
    f"{relative}.{generation}"
    for relative in ("logs.jsonl", "send_audit.jsonl")
    for generation in range(1, JSONL_ROTATION_KEEP + 1)
)
_HISTORY_RESET_FIXED_ORPHAN_TMP_PATHS = (
    "backend_events.jsonl.raw_ids.json.tmp",
    "confirm_queue.jsonl.tmp",
    "hook_events.jsonl.raw_ids.json.tmp",
    "hook_events_state.json.tmp",
    "weflow_bridge_state.json.tmp",
)
_HISTORY_RESET_UUID_TMP_BASES = (
    "runtime/agent_state.json",
    "weflow_bridge_state.json",
    "weflow_sessions.json",
    "weflow_sidebar_state.json",
)
_HISTORY_RESET_FILES = (
    "backend_events.jsonl",
    "backend_events.jsonl.raw_ids.json",
    "confirm_queue.jsonl",
    "hook_events.jsonl",
    "hook_events.jsonl.raw_ids.json",
    "hook_events_state.json",
    "logs.jsonl",
    "runtime/agent_state.json",
    "send_audit.jsonl",
    "weflow_sessions.json",
    "weflow_process.err.log",
    "weflow_process.out.log",
    *_HISTORY_RESET_SQLITE_PATHS,
    *_HISTORY_RESET_ROTATED_LOG_PATHS,
    *_HISTORY_RESET_FIXED_ORPHAN_TMP_PATHS,
    *sorted(DISPOSABLE_ARTIFACTS),
)
_HISTORY_RESET_LOCK_TOLERANT_FILES = {
    "weflow_process.err.log",
    "weflow_process.out.log",
}
_HISTORY_CLEAR_WRITABLE_CONTROL_FILES = (
    "sidebar_state.sqlite",
    "sidebar_state.sqlite-shm",
    "sidebar_state.sqlite-wal",
    "sidebar_state.sqlite-journal",
    "weflow_sidebar_state.json",
    "weflow_bridge_state.json",
)
_HISTORY_CLEAR_MUTATED_PRESERVED_FILES = (
    "send_bridge/acks.jsonl",
    "send_bridge/synced_acks.json",
    "send_bridge/accepted_reverify.json",
)
_HISTORY_PRESERVED_RUNTIME_PATHS = (
    "send_bridge/outbox.jsonl",
    "send_bridge/acks.jsonl",
    "send_bridge/synced_acks.json",
    "send_bridge/accepted_reverify.json",
    "send_bridge/.bridge_worker.lock",
    "send_bridge/.outbox_rw.lock",
    "send_bridge/.outbox_rw.lock.guard",
)
logger = logging.getLogger(__name__)
_SIDEBAR_API_SCHEMA_VERSION = "20260707-runtime-probe-v2"
_SIDEBAR_API_LOADED_AT = utc_now_iso()
_WEFLOW_WORKERS: dict[str, dict[str, Any]] = {}
_WEFLOW_BACKFILL_JOBS: dict[str, dict[str, Any]] = {}
_WEFLOW_PULL_JOBS: dict[str, dict[str, Any]] = {}
_WEFLOW_LOCK = threading.RLock()
_WEFLOW_STATE_FILE_LOCK = threading.RLock()
_AGENT_WORKERS: dict[str, dict[str, Any]] = {}
_AGENT_LOCK = threading.RLock()
_AGENT_TICK_ADMISSION_LOCK = threading.Lock()
_AGENT_TICK_LOCK = threading.Lock()
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
_NATIVE_MIGRATION_LOCK = threading.Lock()
_SUPPORTED_WECHAT_NATIVE_VERSIONS = ("4.1.10.53",)
_NATIVE_MESSAGE_ROOT_ENV_NAMES = (
    "WECHAT_NATIVE_MESSAGE_ROOT",
    "WECHAT_MESSAGE_ROOT",
    "WECHAT_FILES_DIR",
    "WEFLOW_DATA_ROOT",
)


class HistoryResetNotScheduledError(RuntimeError):
    """A reset scheduling failure proven to have occurred before helper spawn."""


def _run_history_leased_thread(
    target: Any,
    args: tuple[Any, ...],
    history_lease: HistoryWriterLease | None,
) -> None:
    """Run a legacy-compatible thread target under a pre-registered lease."""

    try:
        target(*args)
    finally:
        if history_lease is not None:
            history_lease.release()


def _normalize_send_backend(value: str) -> str:
    return str(value or "dry_run").strip().lower()


def build_sidebar_state(data_dir: str | Path = "data") -> dict[str, Any]:
    config = ensure_config(data_dir)
    channels = _channel_state(data_dir)
    queues = _sidebar_queue_state(data_dir, channels_state=channels)
    send_bridge = _sidebar_bridge_state(data_dir, channels_state=channels, limit=12)
    audit = _sidebar_audit_state(data_dir, channels_state=channels, queues_state=queues)
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
            "send_driver_boundary": "bridge_outbox queues replies for the configured non-foreground send backend (delivered by wxid/roomid); backend events can receive multiple conversations without page OCR",
            "input_pipeline": "POST /api/backend-events or append-backend-event -> backend_events.jsonl -> run-agent/poll-backend-events -> conversation_ledgers",
            "background_send_status": _background_send_status(
                config,
                send_bridge,
                data_dir,
                active_backend_probe=False,
            ),
        },
        "config": {
            "mode": config.mode,
            "send_enabled": config.send_enabled,
            "send_driver": config.send_driver,
            "send_backend": _normalize_send_backend(str(getattr(config, "send_backend", "dry_run") or "dry_run")),
            "weflow_base_url": str(getattr(config, "weflow_base_url", "http://127.0.0.1:5031") or "http://127.0.0.1:5031"),
            "weflow_token_env": str(getattr(config, "weflow_token_env", "WEFLOW_API_TOKEN") or "WEFLOW_API_TOKEN"),
            "weflow_send_text_path": str(getattr(config, "weflow_send_text_path", "/send/text") or "/send/text"),
            "weflow_send_file_path": str(getattr(config, "weflow_send_file_path", "/send/file") or "/send/file"),
            "weflow_send_timeout_seconds": float(getattr(config, "weflow_send_timeout_seconds", 35.0) or 35.0),
            "wechat_native_base_url": str(
                getattr(config, "wechat_native_base_url", "http://127.0.0.1:30001") or "http://127.0.0.1:30001"
            ),
            "wechat_native_send_text_path": str(getattr(config, "wechat_native_send_text_path", "/SendTextMsg") or "/SendTextMsg"),
            "wechat_native_send_image_path": str(getattr(config, "wechat_native_send_image_path", "/SendImgMsg") or "/SendImgMsg"),
            "wechat_native_send_file_path": str(
                getattr(config, "wechat_native_send_file_path", "/send_file_msg") or "/send_file_msg"
            ),
            "wechat_native_status_path": str(getattr(config, "wechat_native_status_path", "/QueryDB/status") or "/QueryDB/status"),
            "wechat_native_timeout_seconds": float(getattr(config, "wechat_native_timeout_seconds", 15.0) or 15.0),
            "wechat_native_verify_timeout_seconds": float(getattr(config, "wechat_native_verify_timeout_seconds", 10.0) or 0.0),
            "wechat_native_file_verify_timeout_seconds": float(
                getattr(config, "wechat_native_file_verify_timeout_seconds", 45.0) or 0.0
            ),
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
        "agent": build_sidebar_agent_state(data_dir),
        "resource_audit": _last_resource_audit(data_dir),
        "resource_scheduler": _resource_scheduler_snapshot(data_dir),
        "queues": queues,
        "readiness": build_send_readiness_report(data_dir, active_backend_probe=False),
        "driver_probe": probe_send_controls(data_dir, active_backend_probe=False)["probe"],
        "send_bridge": send_bridge,
        "weflow": build_sidebar_weflow_state(data_dir),
        "native_migration": build_sidebar_native_migration_state(data_dir),
        "wechat_window_probe": _passive_wechat_window_probe(),
        "audit": audit,
        "history_reset": sidebar_history_reset_status(data_dir),
    }


def _history_reset_safe_status_text(value: Any, *, limit: int = 400) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(
        r"(?i)\b(api[_ -]?key|authorization|token|secret)\b(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2[redacted]",
        text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", text)
    return text[: max(0, int(limit))]


def _sanitized_history_reset_clear_result(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    if not source:
        return {}
    result = {
        "status": _history_reset_safe_status_text(source.get("status"), limit=40),
        "policy": _history_reset_safe_status_text(source.get("policy"), limit=80),
        "removed_count": max(0, _int_value(source.get("removed_count"), 0)),
        "retained_locked_count": max(0, _int_value(source.get("retained_locked_count"), 0)),
        "error_count": max(0, _int_value(source.get("error_count"), 0)),
        "history_reset_id": _history_reset_safe_status_text(source.get("history_reset_id"), limit=64),
        "history_reset_epoch": max(0, _int_value(source.get("history_reset_epoch"), 0)),
    }
    source_errors = source.get("errors") if isinstance(source.get("errors"), list) else []
    result["errors"] = [
        {
            "relative_path": _history_reset_safe_status_text(item.get("relative_path"), limit=160),
            "phase": _history_reset_safe_status_text(item.get("phase"), limit=100),
            "error": _history_reset_safe_status_text(item.get("error"), limit=240),
        }
        for item in source_errors[:20]
        if isinstance(item, dict)
    ]
    return result


def _unknown_history_reset_status(reason: str) -> dict[str, Any]:
    return {
        "status": "unknown",
        "phase": "status_unverifiable",
        "terminal": False,
        "active": True,
        "outcome_unknown": True,
        "manual_reopen_required": True,
        "updated_at": "",
        "error": _history_reset_safe_status_text(reason),
        "clear_result": {},
    }


_HISTORY_RESET_TERMINAL_STATUSES = frozenset(
    {"ok", "partial_error", "blocked", "error", "failed", "interrupted"}
)
_HISTORY_RESET_NONTERMINAL_STATUSES = frozenset({"running", "scheduled", "shutdown_scheduled"})


def _nonterminal_history_reset_helper_state(payload: dict[str, Any]) -> str:
    """Classify persisted helper identity without fencing out uncertain owners."""

    helper_pid = _int_value(payload.get("helper_pid"), 0)
    expected_process_start = str(payload.get("helper_process_start") or "").strip()
    if helper_pid <= 0 or not expected_process_start:
        return "unknown"
    if not _pid_exists(helper_pid):
        return "inactive"
    current_process_start = process_start_marker(helper_pid)
    if not current_process_start:
        return "unknown"
    if current_process_start != expected_process_start:
        return "inactive"
    return "active"


def _history_reset_payload_is_nonterminal(payload: dict[str, Any]) -> bool:
    return str(payload.get("status") or "").strip().lower() in _HISTORY_RESET_NONTERMINAL_STATUSES


def _history_reset_payload_is_terminal(payload: dict[str, Any]) -> bool:
    return str(payload.get("status") or "").strip().lower() in _HISTORY_RESET_TERMINAL_STATUSES


def _history_reset_shutdown_lock_state(root: Path) -> tuple[str, str]:
    lock_relative = "runtime/history_reset_shutdown.lock"
    try:
        lock_path = _validate_history_reset_target(root, lock_relative, expected_kind="file")
        lock_stat = _history_path_lstat(lock_path)
    except (OSError, ValueError) as exc:
        return "unknown", f"reset lock path is unsafe: {type(exc).__name__}"
    if lock_stat is None:
        return "missing", ""
    if not _history_path_is_private_regular_file(lock_stat):
        return "unknown", "reset lock file is not private and regular"
    lock = _read_json(lock_path, None)
    if not isinstance(lock, dict):
        return "unknown", "reset lock file is unreadable"
    helper_pid = _int_value(lock.get("helper_pid"), 0)
    if helper_pid <= 0:
        try:
            updated_at = float(lock.get("updated_at_epoch") or 0.0)
        except (TypeError, ValueError):
            updated_at = 0.0
        if updated_at <= 0:
            return "unknown", "reset lock owner identity is incomplete"
        return ("active", "") if max(0.0, time.time() - updated_at) <= 20.0 else ("inactive", "")
    if not _pid_exists(helper_pid):
        return "inactive", ""
    expected_process_start = str(lock.get("helper_process_start") or "").strip()
    if not expected_process_start:
        return "unknown", "reset helper process identity is incomplete"
    current_process_start = process_start_marker(helper_pid)
    if not current_process_start:
        return "unknown", "reset helper process identity cannot be queried"
    if current_process_start != expected_process_start:
        return "inactive", ""
    return "active", ""


def _idle_history_reset_status() -> dict[str, Any]:
    return {
        "status": "idle",
        "phase": "",
        "terminal": True,
        "active": False,
        "outcome_unknown": False,
        "manual_reopen_required": False,
        "updated_at": "",
        "error": "",
        "clear_result": {},
    }


def _sidebar_history_reset_status(data_dir: str | Path) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    status_relative = "runtime/history_reset_shutdown.json"
    try:
        status_path = _validate_history_reset_target(root, status_relative, expected_kind="file")
        status_stat = _history_path_lstat(status_path)
    except (OSError, ValueError) as exc:
        return _unknown_history_reset_status(f"reset status path is unsafe: {type(exc).__name__}")
    payload: dict[str, Any] | None = None
    status_error = ""
    raw_status = ""
    if status_stat is not None:
        if not _history_path_is_private_regular_file(status_stat):
            status_error = "reset status file is not private and regular"
        else:
            loaded = _read_json(status_path, None)
            if not isinstance(loaded, dict):
                status_error = "reset status file is unreadable"
            else:
                payload = loaded
                raw_status = _history_reset_safe_status_text(payload.get("status"), limit=40).lower()
                if raw_status not in _HISTORY_RESET_TERMINAL_STATUSES | _HISTORY_RESET_NONTERMINAL_STATUSES:
                    status_error = "reset status is not recognized"

    lock_state, lock_error = _history_reset_shutdown_lock_state(root)
    if lock_state == "unknown":
        return _unknown_history_reset_status(lock_error)
    if status_error:
        return _unknown_history_reset_status(status_error)
    if payload is None:
        if lock_state == "active":
            return _unknown_history_reset_status("reset lock is active before a matching status was recorded")
        return _idle_history_reset_status()

    result = {
        "status": raw_status,
        "phase": _history_reset_safe_status_text(payload.get("phase"), limit=100),
        "terminal": raw_status in _HISTORY_RESET_TERMINAL_STATUSES,
        "active": False,
        "outcome_unknown": False,
        "manual_reopen_required": bool(payload.get("manual_reopen_required")),
        "updated_at": _history_reset_safe_status_text(payload.get("updated_at"), limit=80),
        "error": _history_reset_safe_status_text(payload.get("error")),
        "clear_result": _sanitized_history_reset_clear_result(payload.get("clear_result")),
    }
    if result["terminal"]:
        if lock_state == "active":
            return _unknown_history_reset_status("reset lock is active while the persisted status is terminal")
        return result

    if lock_state == "active":
        result["active"] = True
        return result
    helper_state = _nonterminal_history_reset_helper_state(payload)
    if helper_state != "inactive":
        result.update(
            {
                "active": True,
                "outcome_unknown": True,
                "error": "history reset helper identity could not be fully reconciled after its lock disappeared",
            }
        )
        return result
    result.update(
        {
            "status": "error",
            "phase": "interrupted",
            "terminal": True,
            "active": False,
            "outcome_unknown": False,
            "error": "history reset stopped before recording a terminal result",
        }
    )
    return result


def sidebar_history_reset_status(data_dir: str | Path) -> dict[str, Any]:
    """Return the sanitized reset state used for cross-surface admission."""

    return _sidebar_history_reset_status(data_dir)


def _sidebar_queue_state(
    data_dir: str | Path,
    *,
    channels_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    queues: dict[str, Any] = {status: list_confirm_queue(data_dir, status=status) for status in QUEUE_STATUSES}
    channel_map = _queue_channel_lookup(channels_state or _channel_state(data_dir))
    grouped: dict[str, dict[str, Any]] = {}
    per_status_groups: dict[str, dict[str, dict[str, Any]]] = {status: {} for status in QUEUE_STATUSES}

    for status in QUEUE_STATUSES:
        payload = queues.get(status) if isinstance(queues.get(status), dict) else {}
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            conversation_id = _queue_item_conversation_id(item) or "unknown"
            group = grouped.get(conversation_id)
            if group is None:
                group = _new_queue_channel_group(conversation_id, channel_map.get(conversation_id, {}))
                grouped[conversation_id] = group
            status_bucket = group["statuses"][status]
            status_bucket["items"].append(item)
            status_bucket["count"] += 1
            group["status_counts"][status] += 1
            group["total_count"] += 1
            latest_at = _queue_item_time(item)
            if latest_at > group["latest_at"]:
                group["latest_at"] = latest_at
            per_status_groups[status][conversation_id] = group

    for status in QUEUE_STATUSES:
        payload = queues.get(status) if isinstance(queues.get(status), dict) else {}
        payload["channels"] = [
            _queue_status_channel_view(group, status)
            for group in sorted(
                per_status_groups[status].values(),
                key=lambda item: (str(item.get("latest_at") or ""), str(item.get("display_name") or "")),
                reverse=True,
            )
        ]
        queues[status] = payload
    queues["by_channel"] = {
        "status": "ok",
        "statuses": list(QUEUE_STATUSES),
        "count": len(grouped),
        "channels": [
            _queue_channel_view(group)
            for group in sorted(
                grouped.values(),
                key=lambda item: (str(item.get("latest_at") or ""), str(item.get("display_name") or "")),
                reverse=True,
            )
        ],
    }
    return queues


def _queue_channel_lookup(channels_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = channels_state.get("items") if isinstance(channels_state, dict) else []
    if not isinstance(items, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        conversation_id = str(item.get("conversation_id") or "").strip()
        if not conversation_id:
            continue
        result[conversation_id] = item
    return result


def _new_queue_channel_group(conversation_id: str, channel: dict[str, Any]) -> dict[str, Any]:
    display_name = str(channel.get("chat_title") or channel.get("display_name") or "").strip() or conversation_id
    return {
        "conversation_id": conversation_id,
        "display_name": display_name,
        "conversation_type": str(channel.get("conversation_type") or ""),
        "receiver": _receiver_from_channel(channel),
        "total_count": 0,
        "latest_at": "",
        "status_counts": {status: 0 for status in QUEUE_STATUSES},
        "statuses": {status: {"count": 0, "items": []} for status in QUEUE_STATUSES},
    }


def _queue_channel_view(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "conversation_id": group["conversation_id"],
        "display_name": group["display_name"],
        "conversation_type": group["conversation_type"],
        "receiver": group["receiver"],
        "total_count": group["total_count"],
        "latest_at": group["latest_at"],
        "status_counts": dict(group["status_counts"]),
        "statuses": {
            status: {"count": group["statuses"][status]["count"], "items": list(group["statuses"][status]["items"])}
            for status in QUEUE_STATUSES
            if group["statuses"][status]["count"]
        },
    }


def _queue_status_channel_view(group: dict[str, Any], status: str) -> dict[str, Any]:
    bucket = group["statuses"][status]
    return {
        "conversation_id": group["conversation_id"],
        "display_name": group["display_name"],
        "conversation_type": group["conversation_type"],
        "receiver": group["receiver"],
        "count": bucket["count"],
        "latest_at": group["latest_at"],
        "status_counts": dict(group["status_counts"]),
        "items": list(bucket["items"]),
    }


def _queue_item_conversation_id(item: dict[str, Any]) -> str:
    reply = item.get("reply") if isinstance(item.get("reply"), dict) else {}
    return str(reply.get("conversation_id") or item.get("conversation_id") or "").strip()


def _queue_item_time(item: dict[str, Any]) -> str:
    reply = item.get("reply") if isinstance(item.get("reply"), dict) else {}
    return str(item.get("updated_at") or item.get("reviewed_at") or item.get("created_at") or reply.get("created_at") or "")


def _sidebar_audit_state(
    data_dir: str | Path,
    *,
    channels_state: dict[str, Any] | None = None,
    queues_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    audit = list_send_audit(data_dir, limit=30, compact_transitions=True)
    items = audit.get("items") if isinstance(audit.get("items"), list) else []
    channel_map = _queue_channel_lookup(channels_state or _channel_state(data_dir))
    queue_lookup = _audit_queue_lookup(queues_state or _sidebar_queue_state(data_dir, channels_state=channels_state))
    grouped: dict[str, dict[str, Any]] = {}
    per_phase_groups: dict[str, dict[str, dict[str, Any]]] = {phase: {} for phase in AUDIT_PHASES}

    for item in items:
        if not isinstance(item, dict):
            continue
        phase = _audit_item_phase(item)
        conversation_id = _audit_item_conversation_id(item, queue_lookup) or "unknown"
        channel = channel_map.get(conversation_id, {})
        group = grouped.get(conversation_id)
        if group is None:
            group = _new_audit_channel_group(conversation_id, channel)
            grouped[conversation_id] = group
        item_view = _audit_item_view(item, phase, conversation_id, channel)
        bucket = group["phases"][phase]
        bucket["items"].append(item_view)
        bucket["count"] += 1
        group["phase_counts"][phase] += 1
        group["total_count"] += 1
        latest_at = _audit_item_time(item)
        if latest_at > group["latest_at"]:
            group["latest_at"] = latest_at
        per_phase_groups[phase][conversation_id] = group

    audit["channels"] = [
        _audit_channel_view(group)
        for group in sorted(
            grouped.values(),
            key=lambda item: (str(item.get("latest_at") or ""), str(item.get("display_name") or "")),
            reverse=True,
        )
    ]
    audit["phases"] = {
        phase: {
            "count": sum(group["phases"][phase]["count"] for group in per_phase_groups[phase].values()),
            "channels": [
                _audit_phase_channel_view(group, phase)
                for group in sorted(
                    per_phase_groups[phase].values(),
                    key=lambda item: (str(item.get("latest_at") or ""), str(item.get("display_name") or "")),
                    reverse=True,
                )
            ],
        }
        for phase in AUDIT_PHASES
    }
    return audit


def _new_audit_channel_group(conversation_id: str, channel: dict[str, Any]) -> dict[str, Any]:
    display_name = str(channel.get("chat_title") or channel.get("display_name") or "").strip() or conversation_id
    return {
        "conversation_id": conversation_id,
        "display_name": display_name,
        "conversation_type": str(channel.get("conversation_type") or ""),
        "receiver": _receiver_from_channel(channel),
        "total_count": 0,
        "latest_at": "",
        "phase_counts": {phase: 0 for phase in AUDIT_PHASES},
        "phases": {phase: {"count": 0, "items": []} for phase in AUDIT_PHASES},
    }


def _audit_channel_view(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "conversation_id": group["conversation_id"],
        "display_name": group["display_name"],
        "conversation_type": group["conversation_type"],
        "receiver": group["receiver"],
        "total_count": group["total_count"],
        "latest_at": group["latest_at"],
        "phase_counts": dict(group["phase_counts"]),
        "phases": {
            phase: {"count": group["phases"][phase]["count"], "items": list(group["phases"][phase]["items"])}
            for phase in AUDIT_PHASES
            if group["phases"][phase]["count"]
        },
    }


def _audit_phase_channel_view(group: dict[str, Any], phase: str) -> dict[str, Any]:
    bucket = group["phases"][phase]
    return {
        "conversation_id": group["conversation_id"],
        "display_name": group["display_name"],
        "conversation_type": group["conversation_type"],
        "receiver": group["receiver"],
        "count": bucket["count"],
        "latest_at": group["latest_at"],
        "phase_counts": dict(group["phase_counts"]),
        "items": list(bucket["items"]),
    }


def _audit_item_view(
    item: dict[str, Any],
    phase: str,
    conversation_id: str,
    channel: dict[str, Any],
) -> dict[str, Any]:
    display_name = str(channel.get("chat_title") or channel.get("display_name") or "").strip() or conversation_id
    view = dict(item)
    view["phase"] = phase
    view["conversation_id"] = conversation_id
    view["channel_display_name"] = display_name
    view["receiver"] = _receiver_from_channel(channel)
    return view


def _audit_queue_lookup(queues_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for status in QUEUE_STATUSES:
        payload = queues_state.get(status) if isinstance(queues_state, dict) else {}
        items = payload.get("items") if isinstance(payload, dict) and isinstance(payload.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            queue_id = str(item.get("queue_id") or "").strip()
            if queue_id:
                lookup[queue_id] = item
    return lookup


def _audit_item_conversation_id(item: dict[str, Any], queue_lookup: dict[str, dict[str, Any]]) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    send_result = payload.get("send_result") if isinstance(payload.get("send_result"), dict) else {}
    conversation_id = str(payload.get("conversation_id") or send_result.get("conversation_id") or "").strip()
    if conversation_id:
        return conversation_id
    queue_id = str(item.get("queue_id") or "").strip()
    if queue_id:
        return _queue_item_conversation_id(queue_lookup.get(queue_id, {}))
    return ""


def _audit_item_phase(item: dict[str, Any]) -> str:
    if bool(item.get("resolved")):
        return "resolved"
    action = str(item.get("action") or "").strip()
    # Projection errors describe the audit event, while ``status`` records the
    # business state that failed to project. Count the event as a failure rather
    # than a second approved/sent transition in the sidebar summary.
    if action == "ledger_sync_failed":
        return "failed"
    if action == "ledger_sync_recovered":
        return "resolved"
    if action == "confirm_send_blocked":
        return "blocked"
    if action == "bridge_ack_sync":
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        ack_status = str(payload.get("ack_status") or "").strip()
        if ack_status == "accepted":
            return "accepted"
        if ack_status == "sent":
            return "sent"
        if ack_status in {"failed", "blocked"}:
            return "failed"
    status = str(item.get("status") or "").strip()
    if status in AUDIT_PHASES:
        return status
    if action == "confirm_approve":
        return "approved"
    if action == "confirm_reject":
        return "rejected"
    return "other"


def _audit_item_time(item: dict[str, Any]) -> str:
    return str(item.get("timestamp") or item.get("created_at") or item.get("updated_at") or "")


def _receiver_from_channel(channel: dict[str, Any]) -> str:
    if str(channel.get("conversation_type") or "").strip() == "private" and not channel_allows_private_receiver(channel):
        return ""
    key = str(channel.get("conversation_key") or "").strip()
    if _looks_like_wechat_receiver(key):
        return key
    sender_ids = channel.get("sender_wechat_ids") if isinstance(channel.get("sender_wechat_ids"), list) else []
    for value in sender_ids:
        text = str(value or "").strip()
        if _looks_like_wechat_receiver(text):
            return text
    return ""


def build_sidebar_task_manager(data_dir: str | Path = "data") -> dict[str, Any]:
    root = Path(data_dir).resolve()
    _reconcile_sidebar_runtime_tasks(root)
    state = TaskStatusStore(root).state()
    _inject_runtime_resource_limits(state, data_dir)
    return state


def _reconcile_sidebar_runtime_tasks(root: Path) -> None:
    store = TaskStatusStore(root)
    agent_worker = _agent_worker_state(root)
    if not bool(agent_worker.get("running")):
        store.finish_external(
            "agent-worker",
            {
                "status": "completed",
                "progress": 100,
                "phase": "连续接话已停止",
                "detail": "worker_not_running",
                "actual_cost": 1,
            },
        )
    weflow_worker = _weflow_worker_state(root)
    if not bool(weflow_worker.get("running")):
        store.finish_external(
            "worker",
            {
                "status": "completed",
                "progress": 100,
                "phase": "后台拉取已停止",
                "detail": "worker_not_running",
                "actual_cost": 1,
            },
        )
    bridge_worker = _bridge_worker_public_state(root)
    if not bool(bridge_worker.get("running")):
        store.finish_external(
            "send-bridge-worker",
            {
                "status": "completed",
                "progress": 100,
                "phase": "发送桥 worker 已停止",
                "detail": "worker_not_running",
                "actual_cost": 1,
            },
        )


def _worker_task_metadata(
    *,
    scope_label: str,
    worker_kind: str,
    last_status: str = "",
    last_error: str = "",
    stale_after_seconds: float = 120.0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "scope_label": scope_label,
        "worker": True,
        "worker_kind": worker_kind,
        "worker_pid": os.getpid(),
        "worker_heartbeat_at": time.time(),
        "worker_stale_after_seconds": max(1.0, float(stale_after_seconds)),
        "last_status": last_status,
        "last_error": last_error,
        **(extra or {}),
    }


def build_sidebar_agent_state(data_dir: str | Path = "data") -> dict[str, Any]:
    return _agent_public_state(Path(data_dir).resolve())


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


def sidebar_diagnostics_export(data_dir: str | Path = "data", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a support bundle snapshot without exporting secret values."""

    payload = payload if isinstance(payload, dict) else {}
    root = Path(data_dir).resolve()
    limit = _bounded_int(payload.get("limit"), 50, 1, 500)
    config = ensure_config(root)
    queues = _sidebar_queue_state(root)
    bundle = {
        "schema": "sidebar_diagnostics_export_v1",
        "status": "ok",
        "created_at": utc_now_iso(),
        "data_dir": str(root),
        "config": _diagnostic_config_summary(config),
        "task_manager": build_sidebar_task_manager(root),
        "queue_counts": {status: int(queues.get(status, {}).get("count", 0)) for status in QUEUE_STATUSES},
        "queues": _sanitize_for_diagnostics(queues),
        "send_audit_tail": _sanitize_for_diagnostics(list_send_audit(root, limit=limit, compact_transitions=True)),
        "send_bridge": _sanitize_for_diagnostics(build_sidebar_bridge_state(root)),
        "readiness": _sanitize_for_diagnostics(build_send_readiness_report(root)),
        "driver_probe": _sanitize_for_diagnostics(probe_send_controls(root)),
        "weflow": _sanitize_for_diagnostics(build_sidebar_weflow_state(root)),
        "native_migration": _sanitize_for_diagnostics(build_sidebar_native_migration_state(root)),
        "storage_migration": _sanitize_for_diagnostics(
            build_storage_migration_status(root, include_sizes=False, max_entries_per_component=1000)
        ),
        "agent": _sanitize_for_diagnostics(build_sidebar_agent_state(root)),
        "resource_audit": _sanitize_for_diagnostics(_last_resource_audit(root)),
        "recent_backend_events": _sanitize_for_diagnostics(_tail_jsonl(root / "backend_events.jsonl", limit=limit)),
        "recent_send_audit_raw": _sanitize_for_diagnostics(_tail_jsonl(root / "send_audit.jsonl", limit=limit)),
        "preserved_send_bridge_files": _existing_relative_paths(root, _HISTORY_PRESERVED_RUNTIME_PATHS),
    }
    if bool(payload.get("persist", True)):
        out_dir = root / "diagnostics"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        out_path = out_dir / f"diagnostics-{stamp}.json"
        _write_json(out_path, bundle)
        bundle["export_path"] = str(out_path)
    return bundle


def sidebar_storage_migration_status(data_dir: str | Path = "data", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    return build_storage_migration_status(
        data_dir,
        include_sizes=bool(payload.get("include_sizes", payload.get("includeSizes", True))),
        max_entries_per_component=_bounded_int(payload.get("max_entries_per_component"), 5000, 100, 50000),
    )


def _diagnostic_config_summary(config: Any) -> dict[str, Any]:
    keys = (
        "mode",
        "send_enabled",
        "send_driver",
        "send_backend",
        "send_confirm_required",
        "wechat_native_base_url",
        "wechat_native_send_text_path",
        "wechat_native_send_image_path",
        "wechat_native_send_file_path",
        "wechat_native_status_path",
        "wechat_native_timeout_seconds",
        "wechat_native_verify_timeout_seconds",
        "wechat_native_file_verify_timeout_seconds",
        "weflow_base_url",
        "weflow_token_env",
        "weflow_send_text_path",
        "weflow_send_file_path",
        "weflow_send_timeout_seconds",
        "ocr_mode",
        "asr_mode",
    )
    return _sanitize_for_diagnostics({key: getattr(config, key, None) for key in keys})


def _sanitize_for_diagnostics(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _diagnostic_secret_key(key_text):
                sanitized[key_text] = "<redacted>"
            else:
                sanitized[key_text] = _sanitize_for_diagnostics(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_for_diagnostics(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_for_diagnostics(item) for item in value]
    return value


def _diagnostic_secret_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(marker in lowered for marker in ("api_key", "apikey", "token", "secret", "password", "authorization"))


def _last_resource_audit(data_dir: str | Path = "data") -> dict[str, Any]:
    payload = _read_json(_resource_audit_path(data_dir), {})
    return payload if isinstance(payload, dict) else {}


def _resource_audit_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "runtime" / "resource_audit.json"


def _resource_scheduler_snapshot(data_dir: str | Path) -> dict[str, Any]:
    try:
        config = ensure_config(data_dir)
        chat_provider = config.providers["chat"]
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
    return _safe_wechat_window_probe(max_children=200, max_controls=300)


def build_sidebar_native_migration_state(data_dir: str | Path = "data") -> dict[str, Any]:
    root = Path(data_dir).resolve()
    latest_path = root / "native_diagnostics" / "native-migration-latest.json"
    latest = _read_json(latest_path, {})
    if not isinstance(latest, dict) or not latest:
        return {
            "status": "empty",
            "schema": "native_migration_state_v1",
            "latest_path": str(latest_path),
            "message": "no native migration probe has been run",
        }
    summary_keys = (
        "status",
        "created_at",
        "base_url",
        "status_path",
        "version_gate",
        "http_status",
        "message_scan",
        "deploy_manifest",
        "report_path",
    )
    return {
        "status": "ok",
        "schema": "native_migration_state_v1",
        "latest_path": str(latest_path),
        "latest": {key: latest.get(key) for key in summary_keys if key in latest},
    }


def sidebar_native_migration_probe(data_dir: str | Path = "data", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Probe the portable PC WeChat native bridge deployment without sending.

    This is intentionally a dry-run style migration report: it checks the local
    native HTTP status endpoint, discovers the running Weixin executable/version,
    scans bounded local message-root candidates only after the version gate, and
    reports cleanup candidates without deleting anything.
    """

    payload = payload if isinstance(payload, dict) else {}
    root = Path(data_dir).resolve()
    config = ensure_config(root)
    timeout = _bounded_float(
        payload.get("timeout_seconds"),
        min(float(getattr(config, "wechat_native_timeout_seconds", 15.0) or 15.0), 3.0),
        0.2,
        10.0,
    )
    include_cleanup_sizes = bool(payload.get("include_cleanup_sizes", True))
    max_depth = _bounded_int(payload.get("max_depth"), 5, 0, 8)
    max_entries = _bounded_int(payload.get("max_entries"), 2500, 50, 20000)
    limit = _bounded_int(payload.get("limit"), 20, 1, 100)

    with _NATIVE_MIGRATION_LOCK:
        native_base_url = str(getattr(config, "wechat_native_base_url", "http://127.0.0.1:30001") or "http://127.0.0.1:30001")
        http_status = wechat_native_http_status(
            native_base_url,
            text_path=str(getattr(config, "wechat_native_send_text_path", "/SendTextMsg") or "/SendTextMsg"),
            image_path=str(getattr(config, "wechat_native_send_image_path", "/SendImgMsg") or "/SendImgMsg"),
            file_path=str(getattr(config, "wechat_native_send_file_path", "/send_file_msg") or "/send_file_msg"),
            status_path=str(getattr(config, "wechat_native_status_path", "/QueryDB/status") or "/QueryDB/status"),
            timeout_seconds=timeout,
        )
        extra_http_probes = _wechat_native_extra_http_probes(native_base_url, timeout_seconds=min(timeout, 0.8))
        discovery_status = _wechat_native_discovery_status(http_status, extra_http_probes)
        processes = _dedupe_native_processes(
            [*_wechat_native_processes(), *_wechat_native_http_owner_processes(native_base_url)]
        )
        install_candidates = _wechat_native_install_candidates(processes)
        version_gate = _wechat_native_version_gate(discovery_status, processes, install_candidates)
        force_scan = bool(payload.get("force_scan", False))
        deep_scan = force_scan or version_gate.get("gate") == "supported"
        message_roots = _wechat_message_root_seeds(discovery_status, processes, install_candidates)
        message_candidates = _wechat_message_path_candidates(
            message_roots,
            deep_scan=deep_scan,
            max_depth=max_depth,
            max_entries=max_entries,
            limit=limit,
        )
        artifact_inventory = _native_hook_artifact_inventory(include_sizes=include_cleanup_sizes)
        cleanup_manifest = _native_hook_cleanup_manifest(include_sizes=include_cleanup_sizes)
        blockers = _native_migration_blockers(http_status, version_gate, message_candidates, deep_scan=deep_scan)
        report_path = ""
        status = _native_migration_status(http_status, version_gate, blockers)
        deploy_manifest = _native_deploy_manifest(
            status=status,
            version_gate=version_gate,
            http_status=http_status,
            message_candidates=message_candidates,
            artifact_inventory=artifact_inventory,
            cleanup_manifest=cleanup_manifest,
        )
        report = {
            "schema": "native_migration_probe_v1",
            "status": status,
            "created_at": utc_now_iso(),
            "data_dir": str(root),
            "base_url": native_base_url,
            "status_path": str(getattr(config, "wechat_native_status_path", "/QueryDB/status") or "/QueryDB/status"),
            "send_paths": {
                "text": str(getattr(config, "wechat_native_send_text_path", "/SendTextMsg") or "/SendTextMsg"),
                "image": str(getattr(config, "wechat_native_send_image_path", "/SendImgMsg") or "/SendImgMsg"),
                "file": str(getattr(config, "wechat_native_send_file_path", "/send_file_msg") or "/send_file_msg"),
            },
            "supported_versions": list(_SUPPORTED_WECHAT_NATIVE_VERSIONS),
            "http_status": http_status,
            "extra_http_probes": extra_http_probes,
            "processes": processes,
            "install_candidates": install_candidates,
            "version_gate": version_gate,
            "message_scan": {
                "deep_scan": deep_scan,
                "force_scan": force_scan,
                "max_depth": max_depth,
                "max_entries": max_entries,
                "seed_count": len(message_roots),
                "candidate_count": len(message_candidates),
                "skipped_reason": "" if deep_scan else "version_not_supported_yet",
            },
            "message_path_candidates": message_candidates,
            "artifact_inventory": artifact_inventory,
            "cleanup_manifest": cleanup_manifest,
            "deploy_manifest": deploy_manifest,
            "blockers": blockers,
            "migration_plan": _native_migration_plan(status, version_gate, http_status, message_candidates),
        }
        if bool(payload.get("persist", True)):
            out_dir = root / "native_diagnostics"
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            out_path = out_dir / f"native-migration-{stamp}.json"
            latest_path = out_dir / "native-migration-latest.json"
            report_path = str(out_path)
            report["report_path"] = report_path
            _write_json(out_path, report)
            _write_json(latest_path, report)
        else:
            report["report_path"] = report_path
    return report


def _wechat_native_processes() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    powershell = _powershell_executable()
    if not powershell:
        return []
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$names = @('Weixin', 'WeChat')
Get-Process -Name $names -ErrorAction SilentlyContinue |
  Where-Object { -not [string]::IsNullOrWhiteSpace($_.Path) } |
  ForEach-Object {
    $info = $null
    try { $info = (Get-Item -LiteralPath $_.Path).VersionInfo } catch {}
    [pscustomobject]@{
      pid = $_.Id
      name = $_.ProcessName
      path = $_.Path
      product_version = if ($info) { $info.ProductVersion } else { '' }
      file_version = if ($info) { $info.FileVersion } else { '' }
      product_name = if ($info) { $info.ProductName } else { '' }
      company_name = if ($info) { $info.CompanyName } else { '' }
    }
  } | ConvertTo-Json -Compress -Depth 4
"""
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("Path") or "").strip()
        if not path:
            continue
        result.append(
            {
                "pid": item.get("pid") or item.get("Id") or item.get("PID") or 0,
                "name": str(item.get("name") or item.get("ProcessName") or "").strip(),
                "path": path,
                "root": str(Path(path).parent),
                "product_version": str(item.get("product_version") or "").strip(),
                "file_version": str(item.get("file_version") or "").strip(),
                "product_name": str(item.get("product_name") or "").strip(),
                "company_name": str(item.get("company_name") or "").strip(),
            }
        )
    return result


def _wechat_native_http_owner_processes(base_url: str) -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    parsed = urlparse(str(base_url or "").strip() or "http://127.0.0.1:30001")
    if parsed.scheme != "http" or (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
        return []
    port = parsed.port or 80
    powershell = _powershell_executable()
    if not powershell:
        return []
    env = os.environ.copy()
    env["CODEX_NATIVE_HTTP_PORT"] = str(port)
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$port = [int]$env:CODEX_NATIVE_HTTP_PORT
Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
  Select-Object -First 8 |
  ForEach-Object {
    $proc = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
    if ($null -ne $proc -and -not [string]::IsNullOrWhiteSpace($proc.Path)) {
      $info = $null
      try { $info = (Get-Item -LiteralPath $proc.Path).VersionInfo } catch {}
      [pscustomobject]@{
        pid = $proc.Id
        name = $proc.ProcessName
        path = $proc.Path
        root = Split-Path -Parent $proc.Path
        source = 'native_http_port_owner'
        product_version = if ($info) { $info.ProductVersion } else { '' }
        file_version = if ($info) { $info.FileVersion } else { '' }
        product_name = if ($info) { $info.ProductName } else { '' }
        company_name = if ($info) { $info.CompanyName } else { '' }
      }
    }
  } | ConvertTo-Json -Compress -Depth 4
"""
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        parsed_json = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    items = parsed_json if isinstance(parsed_json, list) else [parsed_json]
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        result.append(
            {
                "pid": item.get("pid") or 0,
                "name": str(item.get("name") or "").strip(),
                "path": path,
                "root": str(item.get("root") or Path(path).parent),
                "source": "native_http_port_owner",
                "product_version": str(item.get("product_version") or "").strip(),
                "file_version": str(item.get("file_version") or "").strip(),
                "product_name": str(item.get("product_name") or "").strip(),
                "company_name": str(item.get("company_name") or "").strip(),
            }
        )
    return result


def _dedupe_native_processes(processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for process in processes:
        if not isinstance(process, dict):
            continue
        identity = (str(process.get("pid") or ""), str(process.get("path") or "").lower())
        if identity in seen:
            continue
        seen.add(identity)
        result.append(process)
    return result


def _wechat_native_discovery_status(http_status: dict[str, Any], extra_http_probes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        **http_status,
        "health": {
            "status": http_status.get("health", {}),
            "extra_http_probes": extra_http_probes,
        },
    }


def _wechat_native_extra_http_probes(base_url: str, *, timeout_seconds: float) -> list[dict[str, Any]]:
    endpoints = (
        "/version",
        "/api/version",
        "/debug/version",
        "/debug/status",
        "/health",
        "/status",
        "/QueryDB/version",
    )
    results: list[dict[str, Any]] = []
    for endpoint in endpoints:
        url = _native_local_endpoint_url(base_url, endpoint)
        if not url:
            results.append({"endpoint": endpoint, "status": "skipped", "reason": "non_local_or_invalid_base_url"})
            continue
        try:
            payload = _native_http_json_get(url, timeout_seconds=timeout_seconds)
            results.append({"endpoint": endpoint, "url": url, "status": "ok", "payload": payload})
        except Exception as exc:
            results.append({"endpoint": endpoint, "url": url, "status": "error", "error": f"{type(exc).__name__}:{exc}"})
    return results


def _native_local_endpoint_url(base_url: str, endpoint: str) -> str:
    base = str(base_url or "").strip().rstrip("/") or "http://127.0.0.1:30001"
    parsed = urlparse(base)
    if parsed.scheme != "http" or (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
        return ""
    path = str(endpoint or "").strip() or "/"
    if path.startswith("http://") or path.startswith("https://"):
        parsed_path = urlparse(path)
        if parsed_path.scheme != "http" or (parsed_path.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
            return ""
        return path
    return base.rstrip("/") + "/" + path.lstrip("/")


def _native_http_json_get(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=max(0.2, float(timeout_seconds))) as response:
            raw = response.read(1_000_000).decode("utf-8", errors="replace")
    except HTTPError as exc:
        try:
            detail = exc.read(500).decode("utf-8", errors="replace")
        finally:
            exc.close()
        raise ValueError(f"http_{exc.code}:{detail}") from exc
    except URLError as exc:
        raise ValueError(f"url_error:{exc.reason}") from exc
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("response_not_json_object")
    return parsed


def _powershell_executable() -> str:
    return shutil.which("powershell") or shutil.which("pwsh") or ""


def _wechat_native_install_candidates(processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[tuple[str, Path]] = []
    for process in processes:
        root = Path(str(process.get("root") or "")).expanduser()
        if str(root):
            candidates.append(("running_process", root))
    env_root = _env_value("WECHAT_NATIVE_WEIXIN_ROOT")
    if env_root:
        candidates.append(("WECHAT_NATIVE_WEIXIN_ROOT", Path(env_root).expanduser()))
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = _env_value(env_name)
        if not base:
            continue
        base_path = Path(base).expanduser()
        for rel in (
            Path("Tencent") / "Weixin",
            Path("Tencent") / "WeChat",
            Path("Weixin"),
            Path("WeChat"),
        ):
            candidates.append((env_name, base_path / rel))

    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    process_by_root = {str(Path(str(item.get("root") or "")).resolve()).lower(): item for item in processes if item.get("root")}
    for source, root in candidates:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        exe = _first_existing_path((resolved / "Weixin.exe", resolved / "WeChat.exe"))
        process = process_by_root.get(key, {})
        version_info = (
            {
                "product_version": str(process.get("product_version") or ""),
                "file_version": str(process.get("file_version") or ""),
                "source": "running_process",
            }
            if process
            else _windows_file_version_info(exe)
        )
        hook = resolved / "version.dll"
        result.append(
            {
                "source": source,
                "root": str(resolved),
                "exists": resolved.exists(),
                "exe": str(exe) if exe else "",
                "exe_exists": bool(exe and exe.exists()),
                "version": _best_version_from_mapping(version_info),
                "version_info": version_info,
                "deployed_hook": str(hook),
                "deployed_hook_exists": hook.exists(),
                "deployed_hook_size": hook.stat().st_size if hook.exists() else 0,
            }
        )
    return result


def _first_existing_path(paths: tuple[Path, ...]) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def _windows_file_version_info(path: Path) -> dict[str, Any]:
    if os.name != "nt" or not path or not path.exists():
        return {}
    powershell = _powershell_executable()
    if not powershell:
        return {}
    env = os.environ.copy()
    env["CODEX_FILE_VERSION_PATH"] = str(path)
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$path = $env:CODEX_FILE_VERSION_PATH
$info = (Get-Item -LiteralPath $path).VersionInfo
[pscustomobject]@{
  product_version = $info.ProductVersion
  file_version = $info.FileVersion
  product_name = $info.ProductName
  company_name = $info.CompanyName
} | ConvertTo-Json -Compress -Depth 3
"""
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0 or not completed.stdout.strip():
        return {}
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _wechat_native_version_gate(
    http_status: dict[str, Any],
    processes: list[dict[str, Any]],
    install_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    detections: list[dict[str, Any]] = []
    for item in _version_candidates_from_value(http_status.get("health", {})):
        detections.append({"source": "native_http_health", **item})
    for process in processes:
        for key in ("product_version", "file_version"):
            value = str(process.get(key) or "").strip()
            normalized = _normalize_wechat_version(value)
            if normalized:
                detections.append({"source": f"process.{key}", "raw": value, "version": normalized, "path": process.get("path", "")})
    for candidate in install_candidates:
        value = str(candidate.get("version") or "").strip()
        normalized = _normalize_wechat_version(value)
        if normalized:
            detections.append(
                {"source": "install_candidate", "raw": value, "version": normalized, "path": candidate.get("exe", "")}
            )
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in detections:
        identity = (str(item.get("source") or ""), str(item.get("version") or ""))
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(item)
    supported = any(str(item.get("version") or "") in _SUPPORTED_WECHAT_NATIVE_VERSIONS for item in unique)
    has_version = any(item.get("version") for item in unique)
    gate = "supported" if supported else ("unsupported" if has_version else "unknown")
    return {
        "gate": gate,
        "supported": supported,
        "supported_versions": list(_SUPPORTED_WECHAT_NATIVE_VERSIONS),
        "detected_versions": unique,
        "best_version": str(unique[0].get("version") or "") if unique else "",
        "policy": "deep message path scan runs only for supported version unless force_scan=true",
    }


def _version_candidates_from_value(value: Any, *, path: str = "") -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if _looks_like_version_key(str(key)):
                normalized = _normalize_wechat_version(str(item))
                if normalized:
                    result.append({"raw": str(item), "version": normalized, "field": key_path})
            result.extend(_version_candidates_from_value(item, path=key_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.extend(_version_candidates_from_value(item, path=f"{path}[{index}]"))
    elif isinstance(value, (str, int, float)):
        normalized = _normalize_wechat_version(str(value))
        if normalized and _looks_like_version_key(path):
            result.append({"raw": str(value), "version": normalized, "field": path})
    return result


def _looks_like_version_key(value: str) -> bool:
    lowered = str(value or "").lower()
    return any(token in lowered for token in ("version", "ver", "build", "wechat", "weixin"))


def _normalize_wechat_version(value: str) -> str:
    text = str(value or "").strip()
    match = re.search(r"\b(\d+\.\d+\.\d+\.\d+)\b", text)
    if match:
        return match.group(1)
    compact = re.sub(r"[^0-9]", "", text)
    if compact == "411053":
        return "4.1.10.53"
    return ""


def _best_version_from_mapping(value: dict[str, Any]) -> str:
    for key in ("product_version", "file_version", "ProductVersion", "FileVersion"):
        normalized = _normalize_wechat_version(str(value.get(key) or ""))
        if normalized:
            return normalized
    return ""


def _wechat_message_root_seeds(
    http_status: dict[str, Any],
    processes: list[dict[str, Any]],
    install_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []

    def add(path: str | Path, source: str) -> None:
        text = str(path or "").strip()
        if not text:
            return
        candidate = Path(text).expanduser()
        seeds.append({"path": str(candidate), "source": source})

    for env_name in _NATIVE_MESSAGE_ROOT_ENV_NAMES:
        add(_env_value(env_name), env_name)
    for item in _wechat_configured_data_roots():
        add(item["path"], item["source"])
    for item in _path_candidates_from_value(http_status.get("health", {})):
        add(item["path"], f"native_http_health.{item['field']}")
    for process in processes:
        root = str(process.get("root") or "")
        if root:
            add(Path(root).parent, "process_parent")
    for candidate in install_candidates:
        root = str(candidate.get("root") or "")
        if root and candidate.get("exists"):
            add(Path(root).parent, "install_parent")

    home = Path.home()
    documents = home / "Documents"
    one_drive = _env_value("OneDrive")
    document_roots = [documents]
    if one_drive:
        document_roots.append(Path(one_drive) / "Documents")
    for docs in document_roots:
        for rel in ("xwechat_files", "WeChat Files", "Weixin Files"):
            add(docs / rel, "default_documents")
    appdata = _env_value("APPDATA")
    local_appdata = _env_value("LOCALAPPDATA")
    if appdata:
        for rel in (Path("Tencent") / "WeChat", Path("Tencent") / "Weixin"):
            add(Path(appdata) / rel, "default_appdata")
        add(Path(appdata) / "Tencent" / "xwechat", "default_appdata_xwechat")
    if local_appdata:
        for rel in (Path("Tencent") / "WeChat", Path("Tencent") / "Weixin", Path("Tencent") / "xwechat", "xwechat_files"):
            add(Path(local_appdata) / rel, "default_localappdata")

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for seed in seeds:
        try:
            key = str(Path(seed["path"]).resolve()).lower()
        except OSError:
            key = str(seed["path"]).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(seed)
    return unique


def _wechat_configured_data_roots() -> list[dict[str, str]]:
    roots: list[dict[str, str]] = []
    for env_name in ("APPDATA", "LOCALAPPDATA"):
        base = _env_value(env_name)
        if not base:
            continue
        config_dir = Path(base) / "Tencent" / "xwechat" / "config"
        if not config_dir.exists():
            continue
        try:
            files = [path for path in config_dir.iterdir() if path.is_file() and path.suffix.lower() in {".ini", ".cfg", ".txt"}]
        except OSError:
            continue
        for path in files[:20]:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines[:40]:
                text = line.strip().strip('"')
                if not _looks_like_local_wechat_path(text) and not re.match(r"^[a-z]:\\", text, flags=re.IGNORECASE):
                    continue
                candidate = Path(text)
                roots.append({"path": str(candidate), "source": f"xwechat_config:{path.name}"})
                roots.append({"path": str(candidate / "xwechat_files"), "source": f"xwechat_config:{path.name}:xwechat_files"})
    return roots


def _path_candidates_from_value(value: Any, *, field: str = "") -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_field = f"{field}.{key}" if field else str(key)
            result.extend(_path_candidates_from_value(item, field=child_field))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.extend(_path_candidates_from_value(item, field=f"{field}[{index}]"))
    elif isinstance(value, str):
        text = value.strip()
        if _looks_like_local_wechat_path(text):
            result.append({"path": text, "field": field})
    return result


def _looks_like_local_wechat_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text or len(text) > 500:
        return False
    lower = text.lower()
    if not (re.match(r"^[a-z]:\\", text, flags=re.IGNORECASE) or text.startswith("\\\\")):
        return False
    return any(token in lower for token in ("wechat", "weixin", "xwechat", "msg", "micro"))


def _wechat_message_path_candidates(
    seeds: list[dict[str, Any]],
    *,
    deep_scan: bool,
    max_depth: int,
    max_entries: int,
    limit: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for seed in seeds:
        path = Path(str(seed.get("path") or "")).expanduser()
        exists = path.exists()
        signals = _scan_wechat_message_signals(path, max_depth=max_depth, max_entries=max_entries) if exists and deep_scan else {}
        score = _score_wechat_message_path(path, exists=exists, signals=signals)
        candidates.append(
            {
                "path": str(path),
                "source": str(seed.get("source") or ""),
                "exists": exists,
                "kind": "message_root_candidate",
                "score": score,
                "scan_status": "scanned" if exists and deep_scan else ("missing" if not exists else "skipped_version_gate"),
                "signals": signals,
            }
        )
    candidates.sort(key=lambda item: (int(item.get("score", 0) or 0), str(item.get("exists"))), reverse=True)
    return candidates[:limit]


def _scan_wechat_message_signals(root: Path, *, max_depth: int, max_entries: int) -> dict[str, Any]:
    queue: list[tuple[Path, int]] = [(root, 0)]
    db_files: list[str] = []
    signal_dirs: list[str] = []
    entries = 0
    truncated = False
    while queue:
        current, depth = queue.pop(0)
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            entries += 1
            if entries > max_entries:
                truncated = True
                queue.clear()
                break
            name = child.name.lower()
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            if is_dir:
                if _wechat_signal_dir_name(name) and len(signal_dirs) < 40:
                    signal_dirs.append(str(child))
                if depth < max_depth:
                    queue.append((child, depth + 1))
            elif _wechat_signal_db_file(name):
                if len(db_files) < 40:
                    db_files.append(str(child))
    return {
        "scanned_entries": entries,
        "truncated": truncated,
        "db_file_count": len(db_files),
        "db_files": db_files,
        "signal_dir_count": len(signal_dirs),
        "signal_dirs": signal_dirs,
    }


def _wechat_signal_dir_name(name: str) -> bool:
    lowered = str(name or "").lower()
    return lowered in {"msg", "db", "filestorage", "files", "image", "video", "voice"} or any(
        token in lowered for token in ("xwechat", "wechat files", "weixin files", "micro", "message")
    )


def _wechat_signal_db_file(name: str) -> bool:
    lowered = str(name or "").lower()
    if lowered.endswith((".db", ".sqlite", ".sqlite3")):
        return any(token in lowered for token in ("msg", "micro", "media", "contact", "chat", "session", "db"))
    return lowered in {"micro_msg.db", "msg.db"}


def _score_wechat_message_path(path: Path, *, exists: bool, signals: dict[str, Any]) -> int:
    lower = str(path).lower()
    score = 0
    if exists:
        score += 20
    if "xwechat_files" in lower:
        score += 45
    elif "xwechat" in lower:
        score += 30
    if "wechat-doc" in lower:
        score += 32
    if "wechat files" in lower or "weixin files" in lower:
        score += 35
    if "\\tencent\\" in lower:
        score += 12
    score += min(40, int(signals.get("db_file_count", 0) or 0) * 6)
    score += min(25, int(signals.get("signal_dir_count", 0) or 0) * 4)
    return score


def _native_migration_blockers(
    http_status: dict[str, Any],
    version_gate: dict[str, Any],
    message_candidates: list[dict[str, Any]],
    *,
    deep_scan: bool,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if not bool(http_status.get("available")):
        blockers.append({"code": "native_http_unavailable", "reason": str(http_status.get("reason") or "")})
    if version_gate.get("gate") == "unsupported":
        blockers.append(
            {
                "code": "unsupported_wechat_version",
                "detected_versions": version_gate.get("detected_versions", []),
                "supported_versions": list(_SUPPORTED_WECHAT_NATIVE_VERSIONS),
            }
        )
    elif version_gate.get("gate") == "unknown":
        blockers.append({"code": "wechat_version_unknown", "supported_versions": list(_SUPPORTED_WECHAT_NATIVE_VERSIONS)})
    if deep_scan and not any(item.get("exists") and int(item.get("score", 0) or 0) >= 40 for item in message_candidates):
        blockers.append({"code": "message_path_not_confirmed", "candidate_count": len(message_candidates)})
    return blockers


def _native_migration_status(
    http_status: dict[str, Any],
    version_gate: dict[str, Any],
    blockers: list[dict[str, Any]],
) -> str:
    if version_gate.get("gate") == "unsupported":
        return "version_unsupported"
    if not bool(http_status.get("available")):
        return "native_http_unavailable"
    if version_gate.get("gate") == "unknown":
        return "version_unknown"
    if blockers:
        return "needs_attention"
    return "ready"


def _native_migration_plan(
    status: str,
    version_gate: dict[str, Any],
    http_status: dict[str, Any],
    message_candidates: list[dict[str, Any]],
) -> list[dict[str, str]]:
    steps = [
        {
            "step": "confirm_version",
            "status": "done" if version_gate.get("gate") == "supported" else "blocked",
            "detail": f"supported={','.join(_SUPPORTED_WECHAT_NATIVE_VERSIONS)} detected={version_gate.get('best_version') or 'unknown'}",
        },
        {
            "step": "confirm_native_http",
            "status": "done" if http_status.get("available") else "blocked",
            "detail": str(http_status.get("status") or http_status.get("reason") or ""),
        },
        {
            "step": "confirm_message_path",
            "status": "done" if any(item.get("exists") and int(item.get("score", 0) or 0) >= 40 for item in message_candidates) else "pending",
            "detail": "use the highest scoring existing xwechat_files / WeChat Files candidate",
        },
        {
            "step": "cleanup_historical_hook_residue",
            "status": "dry_run_only",
            "detail": "review cleanup_manifest before deleting reference/cache directories",
        },
    ]
    if status == "ready":
        steps.append({"step": "start_bridge_worker", "status": "next", "detail": "use bridge_outbox + wechat_native_http"})
    return steps


def _native_deploy_manifest(
    *,
    status: str,
    version_gate: dict[str, Any],
    http_status: dict[str, Any],
    message_candidates: list[dict[str, Any]],
    artifact_inventory: dict[str, Any],
    cleanup_manifest: dict[str, Any],
) -> dict[str, Any]:
    required_paths = [
        _deploy_path_item("app/personal_wechat_bot", role="runtime_package", required=True),
        _deploy_path_item("scripts/start_sidebar_frontend.py", role="sidebar_launcher", required=True),
        _deploy_path_item("scripts/send_bridge_worker.py", role="send_bridge_worker", required=True),
        _deploy_path_item("scripts/deploy_wechat_native_hook.ps1", role="native_hook_deploy_script", required=True),
        _deploy_path_item("scripts/build_wechat_native_hook.ps1", role="native_hook_build_script", required=True),
        _deploy_path_item("vendor/artifacts/wechat-native-411053/version.dll", role="current_native_hook_dll", required=True),
    ]
    optional_paths = [
        _deploy_path_item("requirements-document.txt", role="document_tools_requirements", required=False),
        _deploy_path_item("requirements-ocr-light.txt", role="light_ocr_requirements", required=False),
        _deploy_path_item("requirements-asr-light.txt", role="light_asr_requirements", required=False),
        _deploy_path_item("requirements-windows-ui.txt", role="windows_ui_requirements", required=False),
        _deploy_path_item("vendor/reference/WeFlow-gitcode/package.json", role="weflow_reference_package", required=False),
        _deploy_path_item("vendor/reference/WeFlow-gitcode/package-lock.json", role="weflow_reference_lockfile", required=False),
    ]
    missing_required = [item for item in required_paths if not item.get("exists")]
    cleanup_items = cleanup_manifest.get("items") if isinstance(cleanup_manifest.get("items"), list) else []
    cleanup_candidates = [
        {
            "relative_path": str(item.get("relative_path") or ""),
            "classification": str(item.get("classification") or ""),
            "safe_to_delete_after_review": bool(item.get("safe_to_delete_after_review", False)),
            "exists": bool(item.get("exists", False)),
        }
        for item in cleanup_items
        if isinstance(item, dict) and bool(item.get("exists", False))
    ]
    current_artifacts = artifact_inventory.get("current_artifacts")
    if not isinstance(current_artifacts, list):
        current_artifacts = []
    version_supported = version_gate.get("gate") == "supported"
    http_available = bool(http_status.get("available"))
    message_path_confirmed = any(
        item.get("exists") and int(item.get("score", 0) or 0) >= 40
        for item in message_candidates
        if isinstance(item, dict)
    )
    blockers: list[dict[str, Any]] = []
    if missing_required:
        blockers.append(
            {
                "code": "missing_required_deploy_paths",
                "paths": [str(item.get("relative_path") or "") for item in missing_required],
            }
        )
    if not version_supported:
        blockers.append(
            {
                "code": "wechat_version_not_supported",
                "detected": version_gate.get("best_version") or "unknown",
                "required": list(_SUPPORTED_WECHAT_NATIVE_VERSIONS),
            }
        )
    if not http_available:
        blockers.append({"code": "native_http_not_ready", "reason": str(http_status.get("reason") or "")})
    if not message_path_confirmed:
        blockers.append({"code": "message_path_not_confirmed"})
    return {
        "schema": "native_deploy_manifest_v1",
        "status": "ready" if not blockers and status == "ready" else "needs_attention",
        "github_checkout_policy": "required paths must be committed or documented; generated caches/node_modules/build outputs stay reinstallable and should not be treated as runtime truth",
        "required_paths": required_paths,
        "optional_dependency_paths": optional_paths,
        "current_artifacts": [
            {
                "relative_path": str(item.get("relative_path") or ""),
                "role": str(item.get("role") or ""),
                "exists": bool(item.get("exists", False)),
                "keep": bool(item.get("keep", False)),
            }
            for item in current_artifacts
            if isinstance(item, dict)
        ],
        "operator_steps": [
            {
                "step": "install_python_dependencies",
                "status": "manual",
                "command": "python -m pip install -r requirements-document.txt -r requirements-ocr-light.txt -r requirements-asr-light.txt -r requirements-windows-ui.txt",
            },
            {
                "step": "probe_pc_wechat",
                "status": "manual",
                "command": "python -m app.personal_wechat_bot.main --data-dir data native-migration-probe",
            },
            {
                "step": "deploy_native_hook",
                "status": "manual",
                "command": "powershell -ExecutionPolicy Bypass -File scripts/deploy_wechat_native_hook.ps1",
            },
            {
                "step": "start_sidebar_and_bridge",
                "status": "manual",
                "command": "python scripts/start_sidebar_frontend.py --data-dir data",
            },
        ],
        "cleanup_candidates": cleanup_candidates,
        "blockers": blockers,
    }


def _deploy_path_item(relative_path: str, *, role: str, required: bool) -> dict[str, Any]:
    path = _repo_root() / relative_path
    return {
        "relative_path": relative_path,
        "path": str(path),
        "role": role,
        "required": required,
        "exists": path.exists(),
    }


def _native_hook_artifact_inventory(*, include_sizes: bool) -> dict[str, Any]:
    items = [
        _native_path_inventory_item(
            "vendor/artifacts/wechat-native-411053",
            role="current_native_hook_artifact",
            keep=True,
            reason="preferred deploy artifact for the project-owned PC WeChat 4.1.10.53 native bridge",
            include_sizes=include_sizes,
        ),
        _native_path_inventory_item(
            "vendor/artifacts/wechat-hook-411053-text",
            role="verified_text_hook_artifact",
            keep=True,
            reason="known text-capable 4.1.10.53 artifact used as compatibility evidence",
            include_sizes=include_sizes,
        ),
        _native_path_inventory_item(
            "vendor/artifacts/wechat-hook-411053-image-experimental",
            role="experimental_media_hook",
            keep=False,
            reason="experimental image-path artifact; not part of the current real-send baseline",
            include_sizes=include_sizes,
        ),
        _native_path_inventory_item(
            "vendor/reference/WeChat-Hook-aixed",
            role="native_hook_source_reference",
            keep=False,
            reason="source/reference tree for rebuilding; can be archived outside SSD after artifact is frozen",
            include_sizes=include_sizes,
        ),
        _native_path_inventory_item(
            "vendor/reference/WeFlow-gitcode",
            role="weflow_reference_fork",
            keep=False,
            reason="large reference fork; node_modules/dist output are cache/build products",
            include_sizes=include_sizes,
        ),
        _native_path_inventory_item(
            "vendor/reference/WeChatFerry-gitee",
            role="deprecated_wcf_reference",
            keep=False,
            reason="deprecated WCF reference; do not resurrect as foreground/native send path",
            include_sizes=include_sizes,
        ),
        _native_path_inventory_item(
            "vendor/reference/wxbot",
            role="deprecated_bot_reference",
            keep=False,
            reason="legacy reference bot, not part of current bridge pipeline",
            include_sizes=include_sizes,
        ),
        _native_path_inventory_item(
            "vendor/reference/wechat-binaries",
            role="wechat_binary_reference_archive",
            keep=False,
            reason="large binary archive for reverse-engineering reference; not needed at runtime",
            include_sizes=include_sizes,
        ),
    ]
    return {
        "schema": "native_hook_artifact_inventory_v1",
        "items": items,
        "current_artifacts": [item for item in items if item.get("keep")],
        "historical_count": sum(1 for item in items if not item.get("keep")),
    }


def _native_hook_cleanup_manifest(*, include_sizes: bool) -> dict[str, Any]:
    items = [
        _native_cleanup_item(
            "vendor/reference/WeFlow-gitcode/node_modules",
            classification="reinstallable_cache",
            reason="npm dependencies are restorable from package-lock; not needed in a GitHub checkout",
            include_sizes=include_sizes,
        ),
        _native_cleanup_item(
            "vendor/reference/WeFlow-gitcode/dist-electron",
            classification="build_output",
            reason="WeFlow build output can be regenerated from source",
            include_sizes=include_sizes,
        ),
        _native_cleanup_item(
            "vendor/reference/WeFlow-gitcode/.git",
            classification="nested_vcs_cache",
            reason="vendored reference history is not needed at runtime; keep package files and reinstall dependencies as needed",
            include_sizes=include_sizes,
        ),
        _native_cleanup_item(
            "vendor/reference/WeChat-Hook-aixed/.git",
            classification="nested_vcs_cache",
            reason="nested git history is not needed to build the current hook source tree",
            include_sizes=include_sizes,
        ),
        _native_cleanup_item(
            "vendor/reference/WeChat-Hook-aixed/x64",
            classification="build_output",
            reason="MSBuild output is regenerated by scripts/build_wechat_native_hook.ps1",
            include_sizes=include_sizes,
        ),
        _native_cleanup_item(
            "vendor/reference/WeChat-Hook-aixed/x64_Version",
            classification="build_output",
            reason="legacy native build output; current deploy artifact lives under vendor/artifacts/wechat-native-411053",
            include_sizes=include_sizes,
        ),
        _native_cleanup_item(
            "vendor/artifacts/wechat-hook-411053-image-experimental",
            classification="experimental_artifact",
            reason="not part of current wechat_native_http baseline",
            include_sizes=include_sizes,
        ),
        _native_cleanup_item(
            "vendor/reference/WeChatFerry-gitee",
            classification="deprecated_reference",
            reason="WCF/Wcferry path is intentionally removed from the current architecture",
            include_sizes=include_sizes,
        ),
        _native_cleanup_item(
            "vendor/reference/wxbot",
            classification="deprecated_reference",
            reason="legacy bot reference, no active import/runtime dependency",
            include_sizes=include_sizes,
        ),
        _native_cleanup_item(
            "vendor/downloads/LibreOffice_26.2.4_Win_x86-64.msi",
            classification="installer_cache",
            reason="large installer cache; runtime lives under vendor/libreoffice",
            include_sizes=include_sizes,
        ),
        _native_cleanup_item(
            "vendor/downloads/libreoffice_admin_extract.log",
            classification="install_log",
            reason="large extraction log, not needed at runtime",
            include_sizes=include_sizes,
        ),
        _native_cleanup_item(
            "vendor/reference/wechat-binaries",
            classification="reference_binary_archive",
            reason="reverse-engineering archive; move off SSD if no longer needed locally",
            include_sizes=include_sizes,
            conservative=True,
        ),
    ]
    total = sum(int(item.get("size_bytes", 0) or 0) for item in items if item.get("exists"))
    return {
        "schema": "native_hook_cleanup_manifest_v1",
        "dry_run": True,
        "delete_performed": False,
        "requires_explicit_cleanup_action": True,
        "total_candidate_bytes": total,
        "items": items,
    }


def _native_path_inventory_item(
    relative_path: str,
    *,
    role: str,
    keep: bool,
    reason: str,
    include_sizes: bool,
) -> dict[str, Any]:
    path = _repo_root() / relative_path
    size = _path_size_summary(path) if include_sizes else {"size_bytes": 0, "file_count": 0, "truncated": False}
    return {
        "path": str(path),
        "relative_path": relative_path,
        "exists": path.exists(),
        "role": role,
        "keep": keep,
        "reason": reason,
        **size,
    }


def _native_cleanup_item(
    relative_path: str,
    *,
    classification: str,
    reason: str,
    include_sizes: bool,
    conservative: bool = False,
) -> dict[str, Any]:
    path = _repo_root() / relative_path
    size = _path_size_summary(path) if include_sizes else {"size_bytes": 0, "file_count": 0, "truncated": False}
    return {
        "path": str(path),
        "relative_path": relative_path,
        "exists": path.exists(),
        "classification": classification,
        "reason": reason,
        "safe_to_delete_after_review": not conservative,
        "conservative_review_required": conservative,
        **size,
    }


def _path_size_summary(path: Path, *, max_files: int = 250_000) -> dict[str, Any]:
    if not path.exists():
        return {"size_bytes": 0, "file_count": 0, "truncated": False}
    if path.is_file():
        try:
            return {"size_bytes": path.stat().st_size, "file_count": 1, "truncated": False}
        except OSError:
            return {"size_bytes": 0, "file_count": 0, "truncated": True}
    total = 0
    count = 0
    truncated = False
    try:
        iterator = path.rglob("*")
        for child in iterator:
            if not child.is_file():
                continue
            count += 1
            if count > max_files:
                truncated = True
                break
            try:
                total += child.stat().st_size
            except OSError:
                truncated = True
    except OSError:
        truncated = True
    return {"size_bytes": total, "file_count": count, "truncated": truncated}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _safe_wechat_window_probe(
    *,
    max_children: int = 80,
    max_controls: int = 160,
) -> dict[str, Any]:
    try:
        probe = build_wechat_window_probe(max_children=max_children, max_controls=max_controls)
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
            "ui_automation": {"available": False, "reason": "probe_error"},
            "raw_probe": {},
        }
    return {
        "status": "empty",
        "reason": "probe returned no structured payload",
        "active": {"status": "empty_probe", "source": "none", "hwnd": 0, "title": ""},
        "windows": [],
        "ignored_windows": [],
        "ui_automation": {"available": False, "reason": "empty_probe"},
        "raw_probe": {},
    }


def _passive_wechat_window_probe() -> dict[str, Any]:
    """Return the polling-safe window schema without enumerating OS windows."""

    return {
        "status": "unchecked",
        "reason": "explicit_probe_required",
        "endpoint": "/api/wechat-probe",
        "active": {
            "status": "unchecked",
            "source": "passive_state",
            "hwnd": 0,
            "title": "",
        },
        "windows": [],
        "ignored_windows": [],
        "ui_automation": {"available": False, "reason": "explicit_probe_required"},
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
    root = Path(data_dir).resolve()
    mode = payload.get("mode")
    enabled = _payload_bool(payload, "send_enabled")
    driver = payload.get("send_driver")
    backend = payload.get("send_backend") or payload.get("sendBackend")
    weflow_base_url = payload.get("weflow_base_url") or payload.get("weflowBaseUrl")
    weflow_token_env = payload.get("weflow_token_env") or payload.get("weflowTokenEnv")
    weflow_text_path = payload.get("weflow_send_text_path") or payload.get("weflowSendTextPath")
    weflow_file_path = payload.get("weflow_send_file_path") or payload.get("weflowSendFilePath")
    weflow_timeout = payload.get("weflow_send_timeout_seconds") or payload.get("weflowSendTimeoutSeconds")
    wechat_native_base_url = payload.get("wechat_native_base_url") or payload.get("wechatHookBaseUrl")
    wechat_native_text_path = payload.get("wechat_native_send_text_path") or payload.get("wechatHookSendTextPath")
    wechat_native_image_path = payload.get("wechat_native_send_image_path") or payload.get("wechatHookSendImagePath")
    wechat_native_file_path = payload.get("wechat_native_send_file_path") or payload.get("wechatHookSendFilePath")
    wechat_native_status_path = payload.get("wechat_native_status_path") or payload.get("wechatHookStatusPath")
    wechat_native_timeout = payload.get("wechat_native_timeout_seconds") or payload.get("wechatHookTimeoutSeconds")
    wechat_native_verify_timeout = (
        payload.get("wechat_native_verify_timeout_seconds") or payload.get("wechatHookVerifyTimeoutSeconds")
    )
    wechat_native_file_verify_timeout = (
        payload.get("wechat_native_file_verify_timeout_seconds") or payload.get("wechatHookFileVerifyTimeoutSeconds")
    )
    confirm_required = _payload_bool(payload, "send_confirm_required")
    max_chars = payload.get("send_max_chars")
    min_interval_seconds = payload.get("send_min_interval_seconds")
    controls = set_send_controls(
        data_dir,
        mode=str(mode) if mode is not None else None,
        enabled=enabled,
        driver=str(driver) if driver is not None else None,
        backend=str(backend) if backend is not None else None,
        weflow_base_url=str(weflow_base_url) if weflow_base_url is not None else None,
        weflow_token_env=str(weflow_token_env) if weflow_token_env is not None else None,
        weflow_send_text_path=str(weflow_text_path) if weflow_text_path is not None else None,
        weflow_send_file_path=str(weflow_file_path) if weflow_file_path is not None else None,
        weflow_send_timeout_seconds=float(weflow_timeout) if weflow_timeout is not None else None,
        wechat_native_base_url=str(wechat_native_base_url) if wechat_native_base_url is not None else None,
        wechat_native_send_text_path=str(wechat_native_text_path) if wechat_native_text_path is not None else None,
        wechat_native_send_image_path=str(wechat_native_image_path) if wechat_native_image_path is not None else None,
        wechat_native_send_file_path=str(wechat_native_file_path) if wechat_native_file_path is not None else None,
        wechat_native_status_path=str(wechat_native_status_path) if wechat_native_status_path is not None else None,
        wechat_native_timeout_seconds=float(wechat_native_timeout) if wechat_native_timeout is not None else None,
        wechat_native_verify_timeout_seconds=(
            float(wechat_native_verify_timeout) if wechat_native_verify_timeout is not None else None
        ),
        wechat_native_file_verify_timeout_seconds=(
            float(wechat_native_file_verify_timeout) if wechat_native_file_verify_timeout is not None else None
        ),
        confirm_required=confirm_required,
        max_chars=int(max_chars) if max_chars is not None else None,
        min_interval_seconds=int(min_interval_seconds) if min_interval_seconds is not None else None,
    )
    runtime_config = _update_runtime_modes_from_payload(data_dir, payload)
    bridge_worker = _reconcile_bridge_worker_after_config_change(root, dict(payload))
    return {
        "status": "ok",
        "send_controls": controls,
        "runtime_modes": runtime_config,
        "runtime_config": runtime_config,
        "bridge_worker": bridge_worker,
    }


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
    def apply(config) -> None:
        if "ocr_mode" in payload:
            config.ocr_mode = _normalize_runtime_mode(payload.get("ocr_mode"))
        if "asr_mode" in payload:
            config.asr_mode = _normalize_runtime_mode(payload.get("asr_mode"))
        file_max_bytes = _file_max_bytes_from_payload(payload)
        if file_max_bytes is not None:
            config.file_max_bytes = file_max_bytes

    config = update_config(data_dir, apply)
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


def sidebar_channel_test_reply(
    data_dir: str | Path,
    conversation_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    root = Path(data_dir).resolve()
    config = ensure_config(root)
    review_required = bool(getattr(config, "send_confirm_required", True))
    channel = _require_sidebar_channel(root, conversation_id)
    scope_error = _sidebar_channel_send_scope_error(channel, payload, default_require=True)
    if scope_error:
        return {
            "status": "blocked",
            "action": "channel_test_reply",
            "conversation_id": channel.conversation_id,
            "reason": scope_error,
            "message": scope_error,
        }
    text = str(payload.get("text") or "").strip()
    if not text:
        text = f"【sidebar测试】这是一条发往 {channel.chat_title or channel.conversation_id} 的通道文本投递探针。"
    reply = _sidebar_test_reply_candidate(
        channel,
        text=text,
        origin="sidebar_channel_test_reply",
        attachments=[],
        review_required=review_required,
    )
    entry = ConversationLedgerStore(root).append_reply(
        reply,
        chat_title=channel.chat_title,
        conversation_type=channel.conversation_type or "private",
    )
    dispatch = _dispatch_sidebar_test_reply(root, reply, entry.entry_id)
    return {
        "status": "ok",
        "action": "channel_test_reply",
        "conversation_id": channel.conversation_id,
        **dispatch,
        "reply": asdict(reply),
        "ledger_entry_id": entry.entry_id,
        "message": dispatch.get("message", ""),
    }


def sidebar_channel_test_file(
    data_dir: str | Path,
    conversation_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    root = Path(data_dir).resolve()
    channel = _require_sidebar_channel(root, conversation_id)
    scope_error = _sidebar_channel_send_scope_error(channel, payload, default_require=True)
    if scope_error:
        return {
            "status": "blocked",
            "action": "channel_test_file",
            "conversation_id": channel.conversation_id,
            "reason": scope_error,
            "message": scope_error,
        }
    config = ensure_config(root)
    review_required = bool(getattr(config, "send_confirm_required", True))
    upload = payload.get("file") if isinstance(payload.get("file"), dict) else payload
    raw_name = str(upload.get("name") or payload.get("name") or "upload.bin").strip()
    safe_name = _safe_upload_filename(raw_name)
    content_base64 = str(
        upload.get("content_base64")
        or upload.get("contentBase64")
        or upload.get("base64")
        or payload.get("content_base64")
        or ""
    ).strip()
    if not content_base64:
        raise ValueError("file.content_base64 is required")
    content = _decode_upload_base64(content_base64)
    max_bytes = int(getattr(config, "file_max_bytes", 20 * 1024 * 1024) or 20 * 1024 * 1024)
    if len(content) > max_bytes:
        raise ValueError(f"uploaded file exceeds file_max_bytes: {len(content)} > {max_bytes}")
    segment = str(getattr(channel, "segment", "") or channel.conversation_id or "unknown")
    target_dir = root / "outgoing_uploads" / _safe_upload_filename(segment)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = (target_dir / f"{uuid4().hex[:12]}_{safe_name}").resolve()
    if root not in target.parents:
        raise ValueError("upload target escapes data_dir")
    target.write_bytes(content)
    caption = str(payload.get("caption") or payload.get("text") or "").strip()
    attachment = {
        "path": str(target),
        "name": safe_name,
        "kind": _upload_kind_for_name(safe_name),
        "mime_type": str(upload.get("mime_type") or upload.get("type") or payload.get("mime_type") or "").strip(),
        "size": len(content),
        "status": "outgoing",
        "source": "sidebar_channel_test_file",
    }
    reply = _sidebar_test_reply_candidate(
        channel,
        text=caption,
        origin="sidebar_channel_test_file",
        attachments=[attachment],
        review_required=review_required,
    )
    entry = ConversationLedgerStore(root).append_reply(
        reply,
        chat_title=channel.chat_title,
        conversation_type=channel.conversation_type or "private",
    )
    dispatch = _dispatch_sidebar_test_reply(root, reply, entry.entry_id)
    return {
        "status": "ok",
        "action": "channel_test_file",
        "conversation_id": channel.conversation_id,
        **dispatch,
        "reply": asdict(reply),
        "ledger_entry_id": entry.entry_id,
        "stored_path": str(target),
        "attachment": attachment,
        "message": dispatch.get("message", ""),
    }


def _dispatch_sidebar_test_reply(root: Path, reply: ReplyCandidate, ledger_entry_id: str) -> dict[str, Any]:
    config = ensure_config(root)
    if bool(getattr(config, "send_confirm_required", True)):
        queue = ConfirmQueue(root / "confirm_queue.jsonl")
        queue_id = queue.enqueue(reply)
        return {
            "dispatch_mode": "confirm",
            "queue_id": queue_id,
            "item": queue.get(queue_id),
            "send_result": {},
            "message": "测试发送已进入发送审核",
        }
    _start_bridge_worker(root, {"source": "sidebar_channel_test_auto"})
    executor = GuardedSendExecutor(config, build_send_driver(config))
    send_result = executor.execute_auto(reply)
    ledger_updated = ConversationLedgerStore(root).update_reply_send_result(
        reply.conversation_id,
        ledger_entry_id,
        send_result,
    )
    SendAuditLog(root / "send_audit.jsonl").append(
        "confirm_send_attempt",
        queue_id=reply.message_id,
        status=send_result.status,
        reason=send_result.reason,
        payload={
            "conversation_id": reply.conversation_id,
            "message_id": reply.message_id,
            "send_result": asdict(send_result),
            "dispatch_mode": "auto",
            "ledger_updated": ledger_updated,
        },
    )
    if ledger_updated:
        try:
            activation = executor.activate_staged(
                send_result,
                expected_projections=["ledger"],
            )
        except Exception as exc:
            activation = executor.fail_staged(
                send_result,
                reason=f"staged_activation_failed:{type(exc).__name__}:{exc}",
                expected_projections=["ledger"],
            )
    else:
        activation = executor.fail_staged(
            send_result,
            reason="staged_projection_failed:ledger_projection_not_updated",
            expected_projections=["ledger"],
        )
    return {
        "dispatch_mode": "auto",
        "queue_id": "",
        "item": {},
        "send_result": asdict(send_result),
        "activation": activation,
        "message": f"测试发送已自动投递：{send_result.status}",
    }


def _sidebar_channel_send_scope_error(channel: Any, payload: dict[str, Any], *, default_require: bool = False) -> str:
    raw_require = payload.get("require_scope", payload.get("requireScope", default_require))
    if not bool(raw_require):
        return ""
    talkers = set(_string_list(payload.get("talkers") or payload.get("talker") or []))
    if not talkers:
        return "send_scope_required: select at least one talker before sending"
    channel_ids = {
        str(getattr(channel, "conversation_id", "") or "").strip(),
        str(getattr(channel, "conversation_key", "") or "").strip(),
        str(getattr(channel, "chat_title", "") or "").strip(),
    }
    sender_ids = getattr(channel, "sender_wechat_ids", [])
    if isinstance(sender_ids, list):
        channel_ids.update(str(item or "").strip() for item in sender_ids)
    sender_names = getattr(channel, "sender_names", [])
    if isinstance(sender_names, list):
        channel_ids.update(str(item or "").strip() for item in sender_names)
    channel_ids.discard("")
    if not talkers.intersection(channel_ids):
        return "send_scope_mismatch: selected talkers do not include this channel"
    return ""


def sidebar_agent_tick(data_dir: str | Path = "data", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    # Peer requests queue before joining the process-wide fence. Nested
    # scheduler threads may still adopt the active fence without forcing the
    # first HTTP request to wait for every later peer request to finish.
    with _AGENT_TICK_ADMISSION_LOCK:
        with history_writer_fence(root, label="sidebar_agent_tick"):
            with _AGENT_TICK_LOCK:
                with blocking_process_lock(
                    root / "runtime_locks" / "sidebar_agent_tick.lock",
                    label="sidebar_agent_tick",
                    stale_after_seconds=3600,
                    wait_timeout_seconds=600,
                ):
                    return _sidebar_agent_tick_unlocked(root, payload)


def _sidebar_agent_tick_unlocked(data_dir: str | Path = "data", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one bounded dialog-agent tick over the backend event bus."""

    payload = payload if isinstance(payload, dict) else {}
    root = Path(data_dir).resolve()
    config = ensure_config(root)
    runtime = build_runtime(config)
    event_file = _backend_event_file_path(root, payload)
    event_file.parent.mkdir(parents=True, exist_ok=True)
    event_file.touch(exist_ok=True)
    loops = _bounded_int(payload.get("loops"), 1, 1, 20)
    requested_talkers = _agent_requested_talkers(payload)
    requested_conversations = _agent_requested_conversation_ids(root, payload)
    requested_scope = _agent_has_requested_scope(payload)
    cursor_scope_ids = _agent_cursor_scope_ids(
        requested_scope=requested_scope,
        requested_talkers=requested_talkers,
        requested_conversation_ids=requested_conversations,
    )
    requested_snapshot_scope = requested_conversations if requested_scope else None
    agent_state_before = _read_agent_state(root)
    restore_cursor = not bool(payload.get("replay") or payload.get("ignore_cursor") or payload.get("ignoreCursor"))
    job_id = f"agent-tick-{uuid4().hex[:12]}"
    task_id = job_id
    store = TaskStatusStore(root)
    task_payload = {
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
    _safe_agent_tick_task_create(store, task_payload)
    _safe_agent_tick_task_transition(
        store,
        task_id,
        "start",
        {"progress": 15, "phase": "正在读取当前 session 对话文件"},
        base_payload=task_payload,
    )
    snapshot_before = _agent_session_snapshot(root, runtime=runtime, conversation_ids=requested_snapshot_scope)
    result: dict[str, Any]
    processed_conversations: list[str] = []
    cursor_restored = False
    cursor_after: dict[str, Any] = {}
    try:
        driver = _build_agent_backend_driver(
            config,
            runtime,
            event_file,
            allowed_conversation_ids=requested_conversations if requested_scope else None,
            extra_roots=_string_list(payload.get("extra_roots") or payload.get("extraRoots") or []),
        )
        if restore_cursor:
            cursor_restored = driver.restore_cursor(
                _agent_event_cursor(agent_state_before, event_file, scope_ids=cursor_scope_ids)
            )
        runtime.active_driver = driver
        _safe_agent_tick_task_update(
            store,
            task_id,
            {
                "progress": 35,
                "phase": "正在运行消息聚合与接话管线",
                "detail": f"会话快照 {snapshot_before.get('conversation_count', 0)} 个；游标{'已恢复' if cursor_restored else '从头读取'}",
            },
            base_payload=task_payload,
        )
        result = PollingRunner(
            runtime,
            driver,
            poll_interval_seconds=0,
            workload="interactive",
        ).run_forever(max_loops=loops)
        cursor_after = driver.cursor()
        processed_conversations = _agent_processed_conversation_ids(result)
        after_scope = _dedupe_strings([*requested_conversations, *processed_conversations])
        snapshot_after = _agent_session_snapshot(
            root,
            runtime=runtime,
            conversation_ids=after_scope if requested_scope else None,
        )
        proactive_replies = _agent_generate_proactive_replies(
            root,
            runtime=runtime,
            snapshot=snapshot_after,
            limit=_bounded_int(payload.get("proactive_limit") or payload.get("proactiveLimit"), 10, 0, 10),
            enabled=bool(payload.get("proactive_pending", payload.get("proactivePending", True))),
        )
        if proactive_replies:
            processed_conversations = _dedupe_strings(
                [
                    *processed_conversations,
                    *[str(item.get("conversation_id") or "") for item in proactive_replies],
                ]
            )
            after_scope = _dedupe_strings([*requested_conversations, *processed_conversations])
            snapshot_after = _agent_session_snapshot(
                root,
                runtime=runtime,
                conversation_ids=after_scope if requested_scope else None,
            )
        processed_count = int(result.get("processed_count") or 0)
        result["proactive_replies"] = proactive_replies
        result["proactive_attempt_count"] = len(proactive_replies)
        result["proactive_reply_count"] = sum(
            1
            for item in proactive_replies
            if isinstance(item, dict) and str(item.get("status") or "") == "ok"
        )
        result["requested_talkers"] = requested_talkers
        result["requested_conversation_ids"] = requested_conversations
        _safe_agent_tick_task_transition(
            store,
            task_id,
            "complete",
            {
                "progress": 100,
                "phase": "一次接话管线已完成",
                "detail": f"处理 {processed_count} 条消息；聚合 {snapshot_after.get('conversation_count', 0)} 个通道",
                "actual_cost": max(1, processed_count + len(proactive_replies)),
            },
            base_payload=task_payload,
        )
        status = "ok"
        error = ""
    except Exception as exc:
        result = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "processed_count": 0, "processed": []}
        result["requested_talkers"] = requested_talkers
        result["requested_conversation_ids"] = requested_conversations
        snapshot_after = _agent_session_snapshot(root, runtime=runtime, conversation_ids=requested_snapshot_scope)
        _safe_agent_tick_task_transition(
            store,
            task_id,
            "fail",
            {
                "progress": 100,
                "phase": "对话 Agent 运行失败",
                "detail": str(exc),
                "last_error": str(exc),
            },
            base_payload=task_payload,
        )
        status = "error"
        error = str(exc)
    agent_state = _record_agent_tick_state(
        root,
        event_file=event_file,
        job_id=job_id,
        status=status,
        result=result,
        snapshot=snapshot_after,
        cursor=cursor_after,
        cursor_scope_ids=cursor_scope_ids,
        cursor_restored=cursor_restored,
        error=error,
    )
    channels = _channel_state(root)
    task_manager = build_sidebar_task_manager(root)
    queues = _sidebar_queue_state(root, channels_state=channels)
    response = {
        "status": status,
        "agent": {
            "schema": "dialog_agent_tick_v1",
            "job_id": job_id,
            "task_id": task_id,
            "event_file": str(event_file),
            "loops": loops,
            "processed_count": int(result.get("processed_count") or 0),
            "proactive_reply_count": int(result.get("proactive_reply_count") or 0),
            "proactive_attempt_count": int(result.get("proactive_attempt_count") or 0),
            "proactive_replies": result.get("proactive_replies", []),
            "processed": result.get("processed", []),
            "runner_status": result.get("status", ""),
            "processed_conversation_ids": processed_conversations,
            "requested_talkers": requested_talkers,
            "requested_conversation_ids": requested_conversations,
            "aggregation_mode": "per_channel",
            "cursor_restored": cursor_restored,
            "cursor": cursor_after,
            "policy": "read_session_snapshot_then_poll_backend_events_then_reply_gate",
        },
        "agent_state": agent_state,
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


def _safe_agent_tick_task_create(store: TaskStatusStore, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return store.create(payload)
    except Exception as exc:
        logger.warning("agent tick task create failed: %s", exc)
        return {}


def _safe_agent_tick_task_update(
    store: TaskStatusStore,
    task_id: str,
    patch: dict[str, Any],
    *,
    base_payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        return store.update(task_id, patch)
    except KeyError:
        return _safe_agent_tick_task_create(store, {**base_payload, **patch})
    except Exception as exc:
        logger.warning("agent tick task update failed for %s: %s", task_id, exc)
        return {}


def _safe_agent_tick_task_transition(
    store: TaskStatusStore,
    task_id: str,
    action: str,
    patch: dict[str, Any] | None = None,
    *,
    base_payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        return store.transition(task_id, action, patch)
    except KeyError:
        status = {
            "start": "running",
            "pause": "paused",
            "resume": "queued",
            "wait": "waiting",
            "block": "blocked",
            "complete": "completed",
            "fail": "failed",
            "cancel": "cancelled",
        }.get(str(action or "").strip().lower(), str(base_payload.get("status") or "queued"))
        return _safe_agent_tick_task_create(store, {**base_payload, **(patch or {}), "status": status})
    except Exception as exc:
        logger.warning("agent tick task transition failed for %s: %s", task_id, exc)
        return {}


def sidebar_agent_start(data_dir: str | Path = "data", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start a conservative continuous dialog-agent worker."""

    payload = payload if isinstance(payload, dict) else {}
    root = Path(data_dir).resolve()
    with history_writer_fence_if_owned(root, label="sidebar_agent_start"):
        return _sidebar_agent_start_fenced(root, payload)


def _sidebar_agent_start_fenced(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    event_file = _backend_event_file_path(root, payload)
    key = str(root)
    with _AGENT_LOCK:
        requested_talkers = _agent_requested_talkers(payload)
        requested_conversations = _agent_requested_conversation_ids(root, payload)
        existing = _AGENT_WORKERS.get(key)
        thread = existing.get("thread") if isinstance(existing, dict) else None
        if isinstance(thread, threading.Thread) and thread.is_alive():
            scope_check = _agent_running_worker_scope_check(
                existing,
                event_file=event_file,
                requested_talkers=requested_talkers,
                requested_conversation_ids=requested_conversations,
            )
            if not scope_check["match"]:
                _upsert_agent_worker_task(
                    root,
                    progress=25,
                    phase="连续接话已在其他作用域运行",
                    detail=str(scope_check["reason"]),
                )
                return {
                    "status": "blocked",
                    "reason": "agent_worker_scope_mismatch",
                    "message": "Dialog Agent worker is already running with a different scope; stop it before starting another scope.",
                    "worker": _agent_worker_state(root),
                    "running_scope": scope_check["running_scope"],
                    "requested_scope": scope_check["requested_scope"],
                    "agent_state": _agent_public_state(root),
                    "task_manager": build_sidebar_task_manager(root),
                }
            _upsert_agent_worker_task(
                root,
                progress=25,
                phase="连续接话已在运行",
                detail=str(event_file),
            )
            return {
                "status": "ok",
                "worker": _agent_worker_state(root),
                "agent_state": _agent_public_state(root),
                "task_manager": build_sidebar_task_manager(root),
                "message": "Dialog Agent worker is already running",
            }
        stop_event = threading.Event()
        worker_payload = dict(payload)
        worker_payload["loops"] = _bounded_int(worker_payload.get("loops"), 1, 1, 5)
        interval = _bounded_float(worker_payload.get("interval_seconds") or worker_payload.get("intervalSeconds"), 2.0, 0.05, 60.0)
        history_lease = register_history_writer_lease_if_owned(
            root,
            label="sidebar_agent_loop",
        )
        thread = threading.Thread(
            target=_run_history_leased_thread,
            args=(
                _agent_background_loop,
                (root, worker_payload, stop_event),
                history_lease,
            ),
            name="sidebar-agent-worker",
            daemon=True,
        )
        _AGENT_WORKERS[key] = {
            "thread": thread,
            "stop": stop_event,
            "started_at": time.time(),
            "loops": 0,
            "interval_seconds": interval,
            "event_file": str(event_file),
            "requested_talkers": requested_talkers,
            "requested_conversation_ids": requested_conversations,
            "last_status": "starting",
            "last_error": "",
            "last_tick_at": 0,
            "last_heartbeat_at": 0,
            "last_idle_at": 0,
            "stop_requested": False,
        }
        _upsert_agent_worker_task(
            root,
            progress=15,
            phase="连续接话后台 worker 已启动",
            detail=str(event_file),
        )
        try:
            thread.start()
        except BaseException:
            _AGENT_WORKERS.pop(key, None)
            if history_lease is not None:
                history_lease.release()
            raise
    return {
        "status": "ok",
        "worker": _agent_worker_state(root),
        "agent_state": _agent_public_state(root),
        "task_manager": build_sidebar_task_manager(root),
        "message": "Dialog Agent worker started",
    }


def _agent_running_worker_scope_check(
    worker: dict[str, Any],
    *,
    event_file: Path,
    requested_talkers: list[str],
    requested_conversation_ids: list[str],
) -> dict[str, Any]:
    running_talkers = _dedupe_strings(
        [str(item or "") for item in worker.get("requested_talkers", [])]
        if isinstance(worker.get("requested_talkers"), list)
        else []
    )
    running_conversations = _dedupe_strings(
        [str(item or "") for item in worker.get("requested_conversation_ids", [])]
        if isinstance(worker.get("requested_conversation_ids"), list)
        else []
    )
    requested_talkers = _dedupe_strings(requested_talkers)
    requested_conversation_ids = _dedupe_strings(requested_conversation_ids)
    running_event_file = _normalize_path_for_compare(str(worker.get("event_file") or ""))
    requested_event_file = _normalize_path_for_compare(str(event_file))
    same_event_file = bool(running_event_file and requested_event_file and running_event_file == requested_event_file)
    if running_conversations and requested_conversation_ids:
        same_scope = set(running_conversations) == set(requested_conversation_ids)
    else:
        same_scope = set(running_talkers) == set(requested_talkers) and set(running_conversations) == set(requested_conversation_ids)
    reason = ""
    if not same_event_file:
        reason = "event_file_mismatch"
    elif not same_scope:
        reason = "scope_mismatch"
    return {
        "match": bool(same_event_file and same_scope),
        "reason": reason,
        "running_scope": {
            "event_file": str(worker.get("event_file") or ""),
            "talkers": running_talkers,
            "conversation_ids": running_conversations,
        },
        "requested_scope": {
            "event_file": str(event_file),
            "talkers": requested_talkers,
            "conversation_ids": requested_conversation_ids,
        },
    }


def _normalize_path_for_compare(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).resolve()).lower()
    except Exception:
        return text.lower()


def sidebar_agent_stop(data_dir: str | Path = "data", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Stop the continuous dialog-agent worker and terminalize its task."""

    payload = payload if isinstance(payload, dict) else {}
    root = Path(data_dir).resolve()
    key = str(root)
    thread: threading.Thread | None = None
    with _AGENT_LOCK:
        worker = _AGENT_WORKERS.get(key)
        if worker and worker.get("stop"):
            worker["stop"].set()
            worker["stop_requested"] = True
            worker["last_status"] = "stopping"
            worker["last_heartbeat_at"] = time.time()
            thread = worker.get("thread") if isinstance(worker.get("thread"), threading.Thread) else None
    if thread is not None:
        thread.join(timeout=1.0)
    worker_state = _agent_worker_state(root)
    finished_tasks = _finish_agent_worker_tasks(root, running=bool(worker_state.get("running")))
    return {
        "status": "ok",
        "worker": worker_state,
        "agent_state": _agent_public_state(root),
        "task_manager": build_sidebar_task_manager(root),
        "finished_tasks": finished_tasks,
        "message": "Dialog Agent worker stop requested" if worker_state.get("running") else "Dialog Agent worker stopped",
    }


def _agent_background_loop(
    root: Path,
    payload: dict[str, Any],
    stop_event: threading.Event,
) -> None:
    with history_writer_lease_if_owned(
        root,
        label="sidebar_agent_loop",
    ):
        _agent_background_loop_leased(root, payload, stop_event)


def _agent_background_loop_leased(root: Path, payload: dict[str, Any], stop_event: threading.Event) -> None:
    interval = _bounded_float(payload.get("interval_seconds") or payload.get("intervalSeconds"), 2.0, 0.05, 60.0)
    tick_payload = _agent_worker_tick_payload(payload)
    try:
        while not stop_event.is_set():
            try:
                pending = _agent_pending_event_snapshot(root, tick_payload)
                _set_agent_worker_fields(
                    root,
                    interval_seconds=interval,
                    event_file=pending.get("event_file", ""),
                    event_file_size=pending.get("size", 0),
                    cursor_offset=pending.get("cursor_offset", 0),
                    last_heartbeat_at=time.time(),
                )
                if pending.get("has_new_events") or pending.get("has_pending_ledger"):
                    _set_agent_worker_fields(root, last_status="running", last_error="", last_tick_at=time.time())
                    _upsert_agent_worker_task(
                        root,
                        progress=35,
                        phase="发现新消息，正在运行一次接话",
                        detail=str(pending.get("event_file") or ""),
                    )
                    result = sidebar_agent_tick(root, tick_payload)
                    agent = result.get("agent") if isinstance(result.get("agent"), dict) else {}
                    processed_count = int(agent.get("processed_count") or result.get("processed_count") or 0)
                    proactive_reply_count = int(agent.get("proactive_reply_count") or 0)
                    proactive_attempt_count = int(agent.get("proactive_attempt_count") or 0)
                    status = str(result.get("status") or "ok")
                    pending_after = _agent_pending_event_snapshot(root, tick_payload)
                    _set_agent_worker_fields(
                        root,
                        loops=_agent_worker_loop_count(root) + 1,
                        event_file_size=pending_after.get("size", 0),
                        cursor_offset=pending_after.get("cursor_offset", 0),
                        last_status=status,
                        last_error=str(result.get("error") or "") if status == "error" else "",
                        last_tick_at=time.time(),
                        last_processed_count=processed_count,
                        last_proactive_reply_count=proactive_reply_count,
                        last_result={
                            "status": status,
                            "processed_count": processed_count,
                            "proactive_reply_count": proactive_reply_count,
                            "proactive_attempt_count": proactive_attempt_count,
                            "job_id": str(agent.get("job_id") or ""),
                        },
                    )
                    _upsert_agent_worker_task(
                        root,
                        progress=45,
                        phase="连续接话运行中，等待新消息",
                        detail=f"last_processed={processed_count}; proactive={proactive_reply_count}",
                    )
                else:
                    _set_agent_worker_fields(root, last_status="idle", last_error="", last_idle_at=time.time())
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                _set_agent_worker_fields(root, last_status="error", last_error=error, last_tick_at=time.time())
                _upsert_agent_worker_task(
                    root,
                    progress=20,
                    phase="连续接话遇到错误，等待重试",
                    detail=error,
                )
            stop_event.wait(interval)
    finally:
        with _AGENT_LOCK:
            worker = _AGENT_WORKERS.get(str(root.resolve()), {})
            worker["stop_requested"] = False
            if str(worker.get("last_status") or "") not in {"error"}:
                worker["last_status"] = "stopped"
            worker["last_heartbeat_at"] = time.time()
            _AGENT_WORKERS[str(root.resolve())] = worker


def _agent_worker_tick_payload(payload: dict[str, Any]) -> dict[str, Any]:
    tick_payload = dict(payload)
    tick_payload["loops"] = _bounded_int(tick_payload.get("loops"), 1, 1, 5)
    for key in ("replay", "ignore_cursor", "ignoreCursor"):
        tick_payload.pop(key, None)
    return tick_payload


def _agent_requested_talkers(payload: dict[str, Any]) -> list[str]:
    return _string_list(payload.get("talkers") or payload.get("talker") or [])


def _agent_has_requested_scope(payload: dict[str, Any]) -> bool:
    if _agent_requested_talkers(payload):
        return True
    return bool(_string_list(payload.get("conversation_ids") or payload.get("conversationIds") or []))


def _agent_requested_conversation_ids(root: Path, payload: dict[str, Any]) -> list[str]:
    explicit = _string_list(payload.get("conversation_ids") or payload.get("conversationIds") or [])
    talkers = set(_agent_requested_talkers(payload))
    if not talkers:
        return _dedupe_strings(explicit)
    matched: list[str] = []
    try:
        channels = _channel_store(root).list_channels()
    except Exception:
        channels = []
    for channel in channels:
        aliases = {
            str(getattr(channel, "conversation_id", "") or "").strip(),
            str(getattr(channel, "conversation_key", "") or "").strip(),
            str(getattr(channel, "chat_title", "") or "").strip(),
        }
        aliases.update(str(item).strip() for item in getattr(channel, "sender_wechat_ids", []) if str(item).strip())
        aliases.update(str(item).strip() for item in getattr(channel, "sender_names", []) if str(item).strip())
        if aliases.intersection(talkers):
            matched.append(str(channel.conversation_id))
    return _dedupe_strings([*explicit, *matched])


def _agent_cursor_scope_ids(
    *,
    requested_scope: bool,
    requested_talkers: list[str],
    requested_conversation_ids: list[str],
) -> list[str] | None:
    if not requested_scope:
        return None
    conversation_ids = _dedupe_strings(requested_conversation_ids)
    if conversation_ids:
        return [f"conversation:{item}" for item in conversation_ids]
    return [f"talker:{item}" for item in _dedupe_strings(requested_talkers)]


def _agent_pending_event_snapshot(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    event_file = _backend_event_file_path(root, payload)
    exists = event_file.exists()
    try:
        size = event_file.stat().st_size if exists else 0
    except OSError:
        size = 0
    cursor = _agent_event_cursor(_read_agent_state(root), event_file)
    try:
        cursor_offset = max(0, int(cursor.get("read_offset") or 0))
    except (TypeError, ValueError):
        cursor_offset = 0
    has_cursor = bool(cursor)
    has_new_events = bool(exists and size > 0 and (not has_cursor or size > cursor_offset or size < cursor_offset))
    requested_talkers = _agent_requested_talkers(payload)
    requested_conversations = _agent_requested_conversation_ids(root, payload)
    ledger_pending = _agent_pending_ledger_snapshot(
        root,
        conversation_ids=requested_conversations if _agent_has_requested_scope(payload) else None,
    )
    return {
        "event_file": str(event_file),
        "exists": exists,
        "size": size,
        "cursor_offset": cursor_offset,
        "has_cursor": has_cursor,
        "has_new_events": has_new_events,
        "requested_talkers": requested_talkers,
        "requested_conversation_ids": requested_conversations,
        **ledger_pending,
    }


def _agent_pending_ledger_snapshot(root: Path, *, conversation_ids: list[str] | None = None) -> dict[str, Any]:
    pending_user_count = 0
    blocked_pending_user_count = 0
    opening_greeting_count = 0
    pending_conversation_ids: list[str] = []
    store = ConversationLedgerStore(root)
    candidates = _dedupe_strings(conversation_ids) if conversation_ids is not None else _agent_conversation_ids_from_ledgers(root)
    for conversation_id in candidates[:50]:
        try:
            entries = [_dataclass_payload(entry) for entry in store.read_entries(conversation_id)]
        except Exception:
            continue
        if not entries:
            continue
        last = entries[-1]
        if str(last.get("conversation_type") or "private") != "private":
            continue
        raw_pending = _agent_pending_user_entries(entries)
        pending = _agent_actionable_pending_user_entries(entries, raw_pending)
        if raw_pending and not pending:
            blocked_pending_user_count += len(raw_pending)
        if pending:
            pending_user_count += len(pending)
            pending_conversation_ids.append(conversation_id)
            continue
        has_assistant = any(str(item.get("role") or "") == "assistant" for item in entries)
        if not has_assistant and str(last.get("role") or "") == "self":
            opening_greeting_count += 1
            pending_conversation_ids.append(conversation_id)
    return {
        "has_pending_ledger": pending_user_count > 0 or opening_greeting_count > 0,
        "pending_user_count": pending_user_count,
        "blocked_pending_user_count": blocked_pending_user_count,
        "opening_greeting_count": opening_greeting_count,
        "pending_conversation_ids": _dedupe_strings(pending_conversation_ids),
    }


def _agent_worker_loop_count(root: Path) -> int:
    with _AGENT_LOCK:
        worker = _AGENT_WORKERS.get(str(root.resolve()), {})
        return int(worker.get("loops", 0) or 0)


def _set_agent_worker_fields(root: Path, **patch: Any) -> None:
    with _AGENT_LOCK:
        key = str(root.resolve())
        worker = dict(_AGENT_WORKERS.get(key, {}))
        if not worker:
            return
        worker.update(patch)
        _AGENT_WORKERS[key] = worker


def _agent_worker_state(root: Path) -> dict[str, Any]:
    key = str(root.resolve())
    with _AGENT_LOCK:
        worker = _AGENT_WORKERS.get(key, {})
        thread = worker.get("thread")
        running = bool(isinstance(thread, threading.Thread) and thread.is_alive())
        return {
            "running": running,
            "started_at": worker.get("started_at", 0),
            "loops": int(worker.get("loops", 0) or 0),
            "interval_seconds": float(worker.get("interval_seconds", 0) or 0),
            "event_file": str(worker.get("event_file") or ""),
            "requested_talkers": list(worker.get("requested_talkers") or [])
            if isinstance(worker.get("requested_talkers"), list)
            else [],
            "requested_conversation_ids": list(worker.get("requested_conversation_ids") or [])
            if isinstance(worker.get("requested_conversation_ids"), list)
            else [],
            "event_file_size": int(worker.get("event_file_size", 0) or 0),
            "cursor_offset": int(worker.get("cursor_offset", 0) or 0),
            "last_status": str(worker.get("last_status", "")),
            "last_error": str(worker.get("last_error", "")),
            "last_tick_at": worker.get("last_tick_at", 0),
            "last_heartbeat_at": worker.get("last_heartbeat_at", 0),
            "last_idle_at": worker.get("last_idle_at", 0),
            "last_processed_count": int(worker.get("last_processed_count", 0) or 0),
            "last_result": worker.get("last_result") if isinstance(worker.get("last_result"), dict) else {},
            "stop_requested": bool(worker.get("stop_requested")),
        }


def _upsert_agent_worker_task(root: Path, *, progress: int, phase: str, detail: str = "") -> dict[str, Any]:
    return TaskStatusStore(root).create(
        {
            "task_id": "agent-worker",
            "title": "连续对话 Agent",
            "kind": "Agent",
            "status": "running",
            "priority": 85,
            "progress": progress,
            "phase": phase,
            "detail": detail,
            "finished_at": "",
            "last_error": "",
            "concurrency_key": "agent:worker",
            "resource_class": "llm_interactive",
            "estimated_cost": 1,
            "external_id": "agent-worker",
            "metadata": _worker_task_metadata(
                scope_label="对话 Agent",
                worker_kind="agent",
                last_status="running",
            ),
        }
    )


def _finish_agent_worker_tasks(root: Path, *, running: bool) -> list[dict[str, Any]]:
    return TaskStatusStore(root).finish_external(
        "agent-worker",
        {
            "status": "cancelled" if running else "completed",
            "progress": 100,
            "phase": "停止信号已发送" if running else "连续接话已停止",
            "detail": "user_requested_stop",
            "actual_cost": 1,
        },
    )


def build_sidebar_bridge_state(data_dir: str | Path = "data") -> dict[str, Any]:
    return _sidebar_bridge_state(data_dir, limit=50)


def clear_sidebar_send_audit(data_dir: str | Path) -> dict[str, Any]:
    return clear_send_audit(data_dir)


def clear_sidebar_history_data(data_dir: str | Path, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Clear disposable conversation/runtime history while preserving config."""

    payload = payload if isinstance(payload, dict) else {}
    shutdown_requested = bool(
        payload.get("shutdown_processes")
        or payload.get("shutdownProcesses")
    )
    try:
        root = _resolve_history_data_root(data_dir)
        _require_owned_history_config_file(root)
    except Exception as exc:
        if shutdown_requested:
            raise HistoryResetNotScheduledError(str(exc)) from exc
        raise
    if shutdown_requested:
        try:
            _validate_history_reset_target(root, "runtime_locks", expected_kind="dir")
            _validate_private_history_file(
                root,
                "runtime_locks/history_reset_fence.lock",
                purpose="admission fence",
            )
            _validate_private_history_file(
                root,
                "runtime_locks/history_reset_fence.lock.guard",
                purpose="admission fence guard",
            )
        except Exception as exc:
            raise HistoryResetNotScheduledError(str(exc)) from exc
        try:
            with blocking_process_lock(
                root / "runtime_locks" / "history_reset_fence.lock",
                label="history_reset_schedule_admission",
                stale_after_seconds=3600.0,
                wait_timeout_seconds=30.0,
            ):
                return _clear_sidebar_history_data_admitted(root, payload)
        except HistoryResetNotScheduledError:
            raise
        except ProcessLockError as exc:
            raise HistoryResetNotScheduledError(str(exc)) from exc
    return _clear_sidebar_history_data_admitted(root, payload)


def _clear_sidebar_history_data_admitted(
    data_dir: str | Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    shutdown_requested = bool(
        payload.get("shutdown_processes")
        or payload.get("shutdownProcesses")
    )
    try:
        root = _resolve_history_data_root(data_dir)
        # A destructive reset must never initialize and thereby "claim" an
        # arbitrary directory supplied as data_dir. Existing config (including
        # its persistent sidecar) is the minimum ownership proof for this root.
        _require_owned_history_config_file(root)
        with config_update_lock(root):
            _load_owned_history_config(root)
            _validate_history_clear_preflight(root)
    except Exception as exc:
        if shutdown_requested:
            raise HistoryResetNotScheduledError(str(exc)) from exc
        raise
    if shutdown_requested:
        return _schedule_sidebar_history_reset_shutdown(root, payload)
    try:
        with blocking_process_lock(
            root / "runtime_locks" / "history_reset_fence.lock",
            label="history_clear",
            stale_after_seconds=3600.0,
            wait_timeout_seconds=0.0,
        ):
            if not _AGENT_TICK_LOCK.acquire(blocking=False):
                return _history_clear_blocked_result(
                    root,
                    [{"worker": "dialog_agent_tick", "source": "in_process"}],
                )
            try:
                with blocking_process_lock(
                    root / "runtime_locks" / "sidebar_agent_tick.lock",
                    label="history_clear",
                    stale_after_seconds=3600,
                    wait_timeout_seconds=0.0,
                ):
                    with blocking_process_lock(
                        root / "weflow_global_operation.lock",
                        label="history_clear",
                        stale_after_seconds=1800.0,
                        wait_timeout_seconds=0.0,
                    ):
                        with _legacy_history_writer_authority(root) as legacy_blockers:
                            if legacy_blockers:
                                return _history_clear_blocked_result(root, legacy_blockers)
                            blockers = _history_clear_active_runtime_blockers(root)
                            if blockers:
                                return _history_clear_blocked_result(root, blockers)
                            return _clear_sidebar_history_data_locked(root)
            finally:
                _AGENT_TICK_LOCK.release()
    except ProcessLockError as exc:
        holder = exc.holder if isinstance(exc.holder, dict) else {}
        label = str(holder.get("label") or "")
        if label.startswith("weflow_"):
            worker_name = "weflow_operation"
        elif label.endswith("agent_tick"):
            worker_name = "dialog_agent_tick"
        else:
            worker_name = "history_writer"
        return _history_clear_blocked_result(
            root,
            [
                {
                    "worker": worker_name,
                    "source": "process_lock",
                    "pid": int(holder.get("pid", 0) or 0),
                    "label": label,
                }
            ],
        )


def _clear_sidebar_history_data_locked(root: Path) -> dict[str, Any]:
    with config_update_lock(root):
        # Re-read the owned config while updates are excluded. The retained-file
        # set and every destructive action below must describe one config state.
        _load_owned_history_config(root)
        _validate_history_clear_preflight(root)
        removed: list[dict[str, Any]] = []
        retained_locked: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        retained = _retained_config_paths(root)
        orphan_tmp_paths = _history_reset_uuid_tmp_paths(root)
        retained_history_paths = _validate_retained_history_paths(
            root,
            retained,
            extra_file_relatives=orphan_tmp_paths,
        )
        reset_at_epoch = int(time.time())
        reset_id = uuid4().hex
        weflow_reset_barrier: dict[str, Any] = {}
        send_bridge_archive: dict[str, Any] = {}

        def pre_delete_failure(phase: str, exc: BaseException) -> dict[str, Any]:
            phase_error = {
                "relative_path": "weflow_bridge_state.json"
                if phase == "write_weflow_history_reset_barrier"
                else "send_bridge/acks.jsonl",
                "path": str(
                    root / (
                        "weflow_bridge_state.json"
                        if phase == "write_weflow_history_reset_barrier"
                        else Path("send_bridge") / "acks.jsonl"
                    )
                ),
                "kind": "reset_barrier"
                if phase == "write_weflow_history_reset_barrier"
                else "bridge_archive",
                "phase": phase,
                "error": f"{type(exc).__name__}: {exc}",
            }
            return {
                "status": "partial_error",
                "policy": "history_only_preserve_sidebar_config",
                "removed_count": 0,
                "removed": [],
                "retained_locked_count": 0,
                "retained_locked": [],
                "error_count": 1,
                "errors": [phase_error],
                "retained_config": [str(item) for item in sorted(retained)],
                "preserved_runtime": _existing_relative_paths(root, _HISTORY_PRESERVED_RUNTIME_PATHS),
                "reinitialized": [],
                "history_reset_id": reset_id,
                "history_reset_epoch": reset_at_epoch,
                "weflow_reset_barrier": weflow_reset_barrier,
                "send_bridge_archive": send_bridge_archive,
            }

        try:
            weflow_reset_barrier = _write_weflow_history_reset_barrier(
                root,
                reset_id=reset_id,
                reset_at_epoch=reset_at_epoch,
            )
        except Exception as exc:
            return pre_delete_failure("write_weflow_history_reset_barrier", exc)
        try:
            send_bridge_archive = _archive_send_bridge_for_history_reset(
                root,
                reset_id=reset_id,
                reset_at_epoch=reset_at_epoch,
            )
        except Exception as exc:
            send_bridge_archive = _send_bridge_history_reset_progress(root, reset_id=reset_id)
            return pre_delete_failure("archive_send_bridge_for_history_reset", exc)
        for relative in _HISTORY_RESET_DIRS:
            _remove_history_path(
                root,
                relative,
                removed,
                retained_locked,
                errors,
                expected_kind="dir",
                retained_history_paths=retained_history_paths,
            )
        for relative in _HISTORY_RESET_NESTED_DIRS:
            _remove_history_path(
                root,
                relative,
                removed,
                retained_locked,
                errors,
                expected_kind="nested_dir",
                retained_history_paths=retained_history_paths,
            )
        for relative in _HISTORY_RESET_FILES:
            _remove_history_path(
                root,
                relative,
                removed,
                retained_locked,
                errors,
                expected_kind="file",
                retained_history_paths=retained_history_paths,
            )
        for relative in orphan_tmp_paths:
            _remove_history_path(
                root,
                relative,
                removed,
                retained_locked,
                errors,
                expected_kind="file",
                retained_history_paths=retained_history_paths,
            )
        reinitialized, reinitialize_errors = _reinitialize_history_runtime_files(root)
        errors.extend(reinitialize_errors)
        try:
            _reset_weflow_sidebar_history(root)
        except Exception as exc:
            errors.append(
                {
                    "relative_path": "sidebar_state.sqlite",
                    "path": str(root / "sidebar_state.sqlite"),
                    "kind": "writable_control",
                    "phase": "reset_weflow_sidebar_history",
                    "error": f"{type(exc).__name__}: {exc}",
                }
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
            "preserved_runtime": _existing_relative_paths(root, _HISTORY_PRESERVED_RUNTIME_PATHS),
            "reinitialized": reinitialized,
            "history_reset_id": reset_id,
            "history_reset_epoch": reset_at_epoch,
            "weflow_reset_barrier": weflow_reset_barrier,
            "send_bridge_archive": send_bridge_archive,
        }


def _history_clear_blocked_result(root: Path, blockers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "blocked",
        "reason": "history_clear_runtime_active",
        "message": "Stop running Agent, WeFlow, and send-bridge history writers first, or use shutdown_processes so cleanup runs after shutdown.",
        "active_workers": blockers,
        "policy": "history_clear_requires_idle_history_writers",
        "preserved_runtime": _existing_relative_paths(root, _HISTORY_PRESERVED_RUNTIME_PATHS),
    }


def _history_clear_active_runtime_blockers(root: Path) -> list[dict[str, Any]]:
    root = root.resolve()
    key = str(root)
    blockers: list[dict[str, Any]] = []
    with _AGENT_LOCK:
        worker = _AGENT_WORKERS.get(key, {})
        if _thread_alive(worker.get("thread") if isinstance(worker, dict) else None):
            blockers.append(
                {
                    "worker": "dialog_agent",
                    "last_status": str(worker.get("last_status") or ""),
                    "event_file": str(worker.get("event_file") or ""),
                    "requested_conversation_ids": list(worker.get("requested_conversation_ids") or [])
                    if isinstance(worker.get("requested_conversation_ids"), list)
                    else [],
                }
            )
    with _WEFLOW_LOCK:
        worker = _WEFLOW_WORKERS.get(key, {})
        if _thread_alive(worker.get("thread") if isinstance(worker, dict) else None):
            blockers.append(
                {
                    "worker": "weflow_background_pull",
                    "last_status": str(worker.get("last_status") or ""),
                    "loops": int(worker.get("loops", 0) or 0),
                }
            )
        pull_job = _WEFLOW_PULL_JOBS.get(key, {})
        if _thread_alive(pull_job.get("thread") if isinstance(pull_job, dict) else None):
            blockers.append(
                {
                    "worker": "weflow_pull_once",
                    "job_id": str(pull_job.get("job_id") or ""),
                    "status": str(pull_job.get("status") or ""),
                }
            )
        backfill_job = _WEFLOW_BACKFILL_JOBS.get(key, {})
        if _thread_alive(backfill_job.get("thread") if isinstance(backfill_job, dict) else None):
            blockers.append(
                {
                    "worker": "weflow_backfill",
                    "job_id": str(backfill_job.get("job_id") or ""),
                    "status": str(backfill_job.get("status") or ""),
                }
            )
    managed_bridge_running = False
    with _BRIDGE_LOCK:
        worker = _BRIDGE_WORKERS.get(key, {})
        managed_bridge_running = _thread_alive(worker.get("thread") if isinstance(worker, dict) else None)
        if managed_bridge_running:
            blockers.append(
                {
                    "worker": "send_bridge",
                    "source": "sidebar_managed",
                    "last_status": str(worker.get("last_status") or ""),
                    "last_error": str(worker.get("last_error") or ""),
                }
            )
    if not managed_bridge_running and bridge_worker_lock_alive(root):
        lock = _bridge_worker_lock_snapshot(root)
        try:
            pid = int(lock.get("pid", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            pid = 0
        lock_data_dir = str(lock.get("data_dir") or "").strip()
        same_data_dir = False
        if lock_data_dir:
            try:
                same_data_dir = Path(lock_data_dir).resolve() == root
            except OSError:
                same_data_dir = False
        # A preserved legacy evidence lock can contain this process's PID and a
        # fresh timestamp without representing a live worker. Modern workers
        # identify their data dir; a different live PID remains fail-closed.
        if same_data_dir or (pid > 0 and pid != os.getpid()):
            blockers.append(
                {
                    "worker": "send_bridge",
                    "source": "external_process",
                    "pid": pid,
                    "label": str(lock.get("label") or ""),
                    "backend_name": str(lock.get("backend_name") or ""),
                }
            )
    blockers.extend(sidebar_browser_runtime_blockers(root))
    blockers.extend(active_history_writer_leases(root))
    return blockers


@contextmanager
def _legacy_history_writer_authority(root: Path):
    """Hold legacy hook authorities until destructive reset has finished."""

    process_paths = (
        (root / "hook_events_state.json.consumer.lock", "hook_pull_runner"),
        (root / "hook_events_state.json.consume.lock", "hook_consume_tick"),
    )
    state_path = root / "hook_events_state.json.lock"
    authority_paths = [path for path, _ in process_paths]
    authority_paths.append(state_path)
    authority_paths.extend(Path(f"{path}.guard") for path in tuple(authority_paths))
    for path in authority_paths:
        path_stat = _history_path_lstat(path)
        if path_stat is None:
            continue
        if (
            _history_path_is_reparse_point(path_stat)
            or not stat.S_ISREG(path_stat.st_mode)
            or int(getattr(path_stat, "st_nlink", 1) or 1) != 1
        ):
            yield [
                {
                    "worker": "hook_pull_runner",
                    "source": "legacy_process_lock",
                    "pid": 0,
                    "label": "",
                    "path": str(path),
                    "reason": "unsafe_legacy_lock_path",
                }
            ]
            return

    with ExitStack() as stack:
        for path, label in process_paths:
            lock = ProcessLock(
                path,
                label=f"history_clear_authority:{label}",
                stale_after_seconds=60.0,
            )
            try:
                lock.acquire(mutation_deadline=time.monotonic() + 2.0)
            except ProcessLockError as exc:
                holder = exc.holder if isinstance(exc.holder, dict) else {}
                yield [_legacy_history_lock_blocker(path, label, holder)]
                return
            except (OSError, TimeoutError) as exc:
                yield [_legacy_history_lock_blocker(path, label, {}, error=exc)]
                return
            stack.callback(lock.release)

        try:
            stack.enter_context(
                short_process_lock(
                    state_path,
                    timeout_seconds=0.1,
                    stale_after_seconds=120.0,
                    timeout_label="legacy hook state authority",
                )
            )
        except (OSError, TimeoutError) as exc:
            holder = _read_json(state_path, None)
            yield [
                _legacy_history_lock_blocker(
                    state_path,
                    "hook_state_writer",
                    holder if isinstance(holder, dict) else {},
                    error=exc,
                )
            ]
            return
        yield []


def _legacy_history_lock_blocker(
    path: Path,
    label: str,
    holder: dict[str, Any],
    *,
    error: BaseException | None = None,
) -> dict[str, Any]:
    try:
        pid = int(holder.get("pid") or 0)
    except (TypeError, ValueError, OverflowError):
        pid = 0
    result = {
        "worker": "hook_pull_runner",
        "source": "legacy_process_lock",
        "pid": pid,
        "label": str(holder.get("label") or label),
        "path": str(path),
        "process_start": str(holder.get("process_start") or ""),
    }
    if error is not None:
        result["reason"] = f"legacy_lock_probe_failed:{type(error).__name__}"
    elif pid <= 0:
        result["reason"] = "invalid_live_lock_identity"
    return result


def build_sidebar_weflow_state(data_dir: str | Path = "data") -> dict[str, Any]:
    root = Path(data_dir)
    persisted = _read_weflow_sidebar_state(root)
    worker = _weflow_worker_state(root)
    token_env = str(persisted.get("token_env") or "WEFLOW_API_TOKEN") if isinstance(persisted, dict) else "WEFLOW_API_TOKEN"
    env_token_present = bool(_env_value(token_env) or _env_value("WEFLOW_API_TOKEN"))
    # A direct token is deliberately never persisted. The historical boolean
    # only says that an earlier request supplied credentials; it is not proof
    # that this process can authenticate now.
    token_present = env_token_present
    token_source = "environment" if env_token_present else "missing"
    cached_sessions = _weflow_cached_sessions(root, limit=200)
    readiness = _weflow_readiness_snapshot(persisted if isinstance(persisted, dict) else {}, worker, token_present, token_source)
    pull_job = _weflow_pull_job_state(root, persisted)
    backfill_job = _weflow_backfill_job_state(root, persisted)
    last_pull = persisted.get("last_pull", {}) if isinstance(persisted, dict) else {}
    if isinstance(pull_job.get("result"), dict) and pull_job.get("result"):
        last_pull = pull_job["result"]
    last_backfill = persisted.get("last_backfill", {}) if isinstance(persisted, dict) else {}
    if isinstance(backfill_job.get("result"), dict) and backfill_job.get("result"):
        last_backfill = backfill_job["result"]
    last_health = persisted.get("last_health", {}) if isinstance(persisted, dict) else {}
    if isinstance(last_health, dict) and last_health:
        health_fresh, health_age = _weflow_health_freshness(last_health)
        last_health = {
            **last_health,
            "fresh": health_fresh,
            "age_seconds": health_age,
            "ttl_seconds": _WEFLOW_HEALTH_TTL_SECONDS,
        }
    return {
        "status": "ok",
        "base_url": str(persisted.get("base_url") or "http://127.0.0.1:5031"),
        "token_env": token_env,
        "token_present": token_present,
        "token_source": token_source,
        "requested_talkers": persisted.get("talkers", []) if isinstance(persisted.get("talkers"), list) else [],
        "requested_talker_count": len(persisted.get("talkers", [])) if isinstance(persisted.get("talkers"), list) else 0,
        "hook_event_file": str(root / "hook_events.jsonl"),
        "backend_event_file": str(root / "backend_events.jsonl"),
        "weflow_state_file": str(root / "weflow_bridge_state.json"),
        "security": {
            "primary_source": "weflow_local_fork",
            "requires_token_for_pull": True,
            "requires_local_fork_marker": True,
            "allows_non_local_by_default": False,
            "native_send_bridge_primary": True,
        },
        "worker": worker,
        "readiness": readiness,
        "pull_job": pull_job,
        "backfill_job": backfill_job,
        "bridge_state": summarize_weflow_bridge_state(root / "weflow_bridge_state.json"),
        "last_health": last_health,
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
    result = {
        **result,
        "token_source": params["token_source"],
        "checked_at": utc_now_iso(),
        "ttl_seconds": _WEFLOW_HEALTH_TTL_SECONDS,
    }
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
        sessions = _annotate_weflow_sessions_with_registration(sessions, registration)
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
    with history_writer_lease_if_owned(
        Path(data_dir).resolve(),
        label="sidebar_weflow_pull_once",
    ):
        result = _run_sidebar_weflow_once(data_dir, payload)
        params = _weflow_params(data_dir, payload)
        result["session_store"] = _register_weflow_result_sessions(data_dir, payload, result)
        _record_weflow_state_safely(data_dir, {"last_pull": result, **_weflow_public_params(params)}, action="pull-once", result=result)
        return result


def _start_weflow_pull_job(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    with history_writer_fence_if_owned(root, label="sidebar_weflow_pull_job_start"):
        return _start_weflow_pull_job_fenced(root, payload)


def _start_weflow_pull_job_fenced(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
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
        history_lease = register_history_writer_lease_if_owned(
            root,
            label="sidebar_weflow_pull_job",
            metadata={"job_id": job_id},
        )
        thread = threading.Thread(
            target=_run_history_leased_thread,
            args=(
                _weflow_pull_job_loop,
                (root, dict(payload), job_id),
                history_lease,
            ),
            name="sidebar-weflow-pull-once",
            daemon=True,
        )
        job["thread"] = thread
        _WEFLOW_PULL_JOBS[key] = job
        try:
            thread.start()
        except BaseException:
            _WEFLOW_PULL_JOBS.pop(key, None)
            if history_lease is not None:
                history_lease.release()
            raise
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
    with history_writer_lease_if_owned(
        Path(data_dir).resolve(),
        label="sidebar_weflow_backfill_sync",
    ):
        return _run_weflow_backfill_sync_leased(data_dir, payload)


def _run_weflow_backfill_sync_leased(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Run a history backfill synchronously and return the final result.

    The sidebar UI uses the async :func:`sidebar_weflow_backfill` (returns
    immediately with a job it polls). A short-lived CLI process cannot poll a
    daemon thread; it would exit before the thread ran, so the CLI calls this
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
    root = Path(data_dir).resolve()
    with history_writer_fence_if_owned(root, label="sidebar_weflow_backfill_start"):
        return _sidebar_weflow_backfill_fenced(root, payload)


def _sidebar_weflow_backfill_fenced(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
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
        history_lease = register_history_writer_lease_if_owned(
            root,
            label="sidebar_weflow_backfill_job",
            metadata={"job_id": job_id},
        )
        thread = threading.Thread(
            target=_run_history_leased_thread,
            args=(
                _weflow_backfill_job_loop,
                (root, backfill_payload, job_id, stop_event),
                history_lease,
            ),
            name="sidebar-weflow-backfill",
            daemon=True,
        )
        job["thread"] = thread
        _WEFLOW_BACKFILL_JOBS[key] = job
        try:
            thread.start()
        except BaseException:
            _WEFLOW_BACKFILL_JOBS.pop(key, None)
            if history_lease is not None:
                history_lease.release()
            raise
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
    with history_writer_fence_if_owned(root, label="sidebar_weflow_start"):
        return _sidebar_weflow_start_fenced(root, payload)


def _sidebar_weflow_start_fenced(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    key = str(root)
    worker_payload = _weflow_background_payload(payload)
    with _WEFLOW_LOCK:
        existing = _WEFLOW_WORKERS.get(key)
        if existing and existing.get("thread") and existing["thread"].is_alive():
            _start_bridge_worker(root, worker_payload)
            result = {"status": "ok", "worker": _weflow_worker_state(root), "message": "WeFlow 后台拉取已经在运行"}
            _append_weflow_operation_history(data_dir, "start", result)
            return result
        stop_event = threading.Event()
        history_lease = register_history_writer_lease_if_owned(
            root,
            label="sidebar_weflow_loop",
        )
        thread = threading.Thread(
            target=_run_history_leased_thread,
            args=(
                _weflow_background_loop,
                (root, worker_payload, stop_event),
                history_lease,
            ),
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
        try:
            thread.start()
        except BaseException:
            _WEFLOW_WORKERS.pop(key, None)
            if history_lease is not None:
                history_lease.release()
            raise
    # Start the send-bridge delivery worker alongside the pull worker: the
    # pull->reply->deliver chain needs both halves running. No-op unless the
    # active send driver is bridge_outbox.
    _start_bridge_worker(root, worker_payload)
    result = {"status": "ok", "worker": _weflow_worker_state(root), "message": "WeFlow 后台拉取已启动"}
    _append_weflow_operation_history(data_dir, "start", result)
    return result


def _weflow_background_payload(payload: dict[str, Any]) -> dict[str, Any]:
    worker_payload = dict(payload)
    explicit_process = (
        "process_backend_events" in worker_payload
        or "processBackendEvents" in worker_payload
        or "capture_only" in worker_payload
        or "captureOnly" in worker_payload
    )
    if not explicit_process:
        worker_payload["capture_only"] = True
        worker_payload["process_backend_events"] = False
    return worker_payload


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
                "purpose": str(spec.get("purpose") or ""),
                "python": str(python_executable),
                "runtime_available": runtime_available,
                "available": bool(group_items) and all(item["available"] for item in group_items),
                "requirements": str(spec["requirements"].resolve()),
                "requirements_exists": Path(spec["requirements"]).exists(),
                "install_command": _dependency_install_command(spec, python_executable),
                "portable_from_github": True,
                "reinstallable": bool(spec.get("venv") or spec.get("target") or spec["runtime"] == "main_python"),
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
        "migration_notes": [
            "A fresh GitHub checkout can start with the main Python requirements, then use /api/weflow/install-deps for optional document/OCR/ASR/UI runtimes.",
            "Large vendor runtimes are reinstallable caches; keep requirements files in git and avoid committing generated virtualenv/node_modules output.",
            "GPU OCR/ASR dependencies are intentionally not installed by the default light dependency action.",
        ],
    }


def _dependency_specs() -> list[dict[str, Any]]:
    return [
        {
            "group": "document_runtime",
            "runtime": "main_python",
            "purpose": "PDF, Office, spreadsheet, and document attachment parsing in the main Python runtime",
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
            "purpose": "lightweight CPU OCR runtime used by file ingestion and runtime probe",
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
            "purpose": "lightweight local ASR runtime used by voice attachment ingestion",
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
            "purpose": "Windows UI Automation diagnostics for the control console; not used for foreground sending",
            "python": Path(sys.executable),
            "target": Path("vendor/windows-ui"),
            "requirements": Path("requirements-windows-ui.txt"),
            "modules": {"comtypes": "comtypes"},
        },
    ]


def _dependency_install_command(spec: dict[str, Any], python_executable: Path) -> list[str]:
    command = [str(python_executable), "-m", "pip", "install"]
    if spec.get("target"):
        command.extend(["--target", str(Path(spec["target"]))])
    command.extend(["-r", str(Path(spec["requirements"]).resolve())])
    if spec.get("venv") and not python_executable.exists():
        return [str(Path(sys.executable)), "-m", "venv", str(Path(spec["venv"])), "&&", *command]
    return command


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
        raise ValueError("status must be sent, accepted, failed, or blocked")
    reason = str(payload.get("reason", "")).strip()
    external_message_id = str(payload.get("external_message_id") or payload.get("externalMessageId") or "").strip()
    extra = payload.get("payload")
    result = bridge_ack_if_queued(
        data_dir,
        bridge_id,
        status=status,
        reason=reason,
        external_message_id=external_message_id,
        payload=extra if isinstance(extra, dict) else {},
    )
    if not bool(result.get("applied")):
        return result
    effective_ack = result.get("ack") if isinstance(result.get("ack"), dict) else {}
    result["send_sync"] = sync_bridge_ack_to_send_state(
        data_dir,
        bridge_id,
        status=str(result.get("effective_status") or status),
        reason=str(effective_ack.get("reason", reason)),
        external_message_id=str(effective_ack.get("external_message_id", external_message_id)),
    )
    return result


def retry_sidebar_bridge_item(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    bridge_id = str(payload.get("bridge_id") or payload.get("bridgeId") or "").strip()
    if not bridge_id:
        raise ValueError("bridge_id is required")
    reviewer = str(payload.get("reviewer") or "sidebar").strip() or "sidebar"
    note = str(payload.get("note") or "").strip()
    result = retry_bridge_item(data_dir, bridge_id, reviewer=reviewer, note=note)
    # Publish queue/ledger/task projections before a newly-started worker can
    # drain the successor and race its terminal ack against those projections.
    _start_bridge_worker(Path(data_dir).resolve(), dict(payload))
    return result


def sidebar_queue_action(data_dir: str | Path, action: str, queue_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    reviewer = str(payload.get("reviewer", "sidebar"))
    note = str(payload.get("note", ""))
    try:
        if action == "approve":
            return approve_confirm_item(data_dir, queue_id, reviewer=reviewer, note=note)
        if action == "reject":
            return reject_confirm_item(data_dir, queue_id, reviewer=reviewer, note=note)
        if action in {"remove", "delete"}:
            return remove_confirm_item(data_dir, queue_id, reviewer=reviewer, note=note)
    except ConfirmQueueClaimConflict as exc:
        item = ConfirmQueue(Path(data_dir) / "confirm_queue.jsonl").get(queue_id) or {}
        return {
            "status": "blocked",
            "reason": str(exc) or "send_claim_conflict",
            "queue_id": queue_id,
            "queue_status": str(item.get("status") or ""),
            "claim_conflict": True,
            "item": item,
        }
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
                pull = runner.run_once(process_imported=bool(params["process_backend_events"]))
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
            "process_backend_events": bool(params["process_backend_events"]),
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
            return {"running": False, "last_status": "stopped", "worker_id": "", "config_signature": {}}
        thread = worker.get("thread")
        return {
            "running": bool(isinstance(thread, threading.Thread) and thread.is_alive()),
            "last_status": str(worker.get("last_status") or ""),
            "last_error": str(worker.get("last_error") or ""),
            "started_at": worker.get("started_at"),
            "restart_count": int(worker.get("restart_count", 0) or 0),
            "last_tick_at": worker.get("last_tick_at"),
            "stop_requested": bool(worker.get("stop_requested")),
            "worker_id": str(worker.get("worker_id") or ""),
            "config_signature": worker.get("config_signature") if isinstance(worker.get("config_signature"), dict) else {},
        }


def _bridge_worker_public_state(root: Path) -> dict[str, Any]:
    """Snapshot covering both sidebar-managed and separately launched workers."""

    root = root.resolve()
    managed = _bridge_worker_state(root)
    lock = _bridge_worker_lock_snapshot(root)
    managed_running = bool(managed.get("running"))
    lock_alive = bool(lock.get("alive"))
    if managed_running:
        source = "sidebar_managed"
    elif lock_alive:
        source = "external_process"
    else:
        source = "stopped"
    current_signature: dict[str, Any] = {}
    try:
        current_signature = _bridge_worker_config_signature(load_config(root))
    except Exception:
        current_signature = {}
    lock_signature = lock.get("config_signature") if isinstance(lock.get("config_signature"), dict) else {}
    if lock_alive and lock_signature and current_signature:
        config_match: bool | None = lock_signature == current_signature
        config_status = "matched" if config_match else "stale"
    elif lock_alive:
        config_match = None
        config_status = "unknown_legacy_lock"
    else:
        config_match = False
        config_status = "not_running"
    return {
        "running": managed_running or lock_alive,
        "source": source,
        "pid": lock.get("pid", 0),
        "pid_alive": lock.get("pid_alive", False),
        "lock_alive": lock_alive,
        "lock_path": lock.get("path", ""),
        "heartbeat_age_seconds": lock.get("heartbeat_age_seconds"),
        "label": lock.get("label", ""),
        "backend_name": lock.get("backend_name", ""),
        "data_dir": lock.get("data_dir", ""),
        "config_match": config_match,
        "config_status": config_status,
        "config_signature": lock_signature or managed.get("config_signature", {}),
        "expected_config_signature": current_signature,
        "managed": managed,
    }


def _bridge_worker_lock_snapshot(root: Path) -> dict[str, Any]:
    path = bridge_worker_lock_path(root)
    payload: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payload = raw
        except (OSError, json.JSONDecodeError):
            payload = {}
    heartbeat = payload.get("heartbeat_at")
    heartbeat_age: float | None = None
    if isinstance(heartbeat, (int, float)):
        heartbeat_age = max(0.0, time.time() - float(heartbeat))
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    config_signature = payload.get("config_signature") if isinstance(payload.get("config_signature"), dict) else {}
    return {
        "path": str(path),
        "exists": path.exists(),
        "alive": bridge_worker_lock_alive(root),
        "stale_after_seconds": BRIDGE_WORKER_LOCK_STALE_SECONDS,
        "heartbeat_age_seconds": heartbeat_age,
        "pid": pid,
        "pid_alive": _pid_exists(pid) if pid else False,
        "process_start": str(payload.get("process_start") or ""),
        "process_instance": str(payload.get("process_instance") or ""),
        "owner_token": str(payload.get("owner_token") or ""),
        "label": str(payload.get("label") or ""),
        "acquired_at": payload.get("acquired_at"),
        "heartbeat_at": heartbeat,
        "backend_name": str(payload.get("backend_name") or ""),
        "data_dir": str(payload.get("data_dir") or ""),
        "config_signature": config_signature,
    }


def _bridge_worker_config_signature(config: Any) -> dict[str, Any]:
    """Fields that are captured when the bridge worker constructs its backend."""

    return bridge_worker_config_signature(config)


def _bridge_worker_should_run(config: Any) -> bool:
    return (
        str(getattr(config, "send_driver", "") or "") == "bridge_outbox"
        and bool(getattr(config, "send_enabled", False))
    )


def ensure_sidebar_bridge_worker(data_dir: str | Path, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start the sidebar-managed send bridge when current config requires it."""

    root = Path(data_dir).resolve()
    _start_bridge_worker(root, dict(payload or {}))
    return _bridge_worker_public_state(root)


def _reconcile_bridge_worker_after_config_change(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Restart or stop an already-running bridge worker after send config edits.

    The worker reads ``config.json`` once at startup to build its backend. If the
    user switches send backends while the worker is alive, leaving it in place
    would keep delivering with the old backend. Do not start a brand-new worker
    from a passive settings save; the next WeFlow start or send-approved action
    will do that. Existing workers are kept aligned.
    """

    try:
        config = load_config(root)
    except Exception:
        return _bridge_worker_state(root)
    worker_state = _bridge_worker_state(root)
    if not worker_state.get("running"):
        return worker_state
    if not _bridge_worker_should_run(config):
        _stop_bridge_worker(root)
        return _bridge_worker_state(root)
    _start_bridge_worker(root, payload)
    return _bridge_worker_state(root)


def _bridge_worker_supervisor(
    root: Path,
    payload: dict[str, Any],
    stop_event: threading.Event,
) -> None:
    with history_writer_lease_if_owned(
        root,
        label="sidebar_send_bridge_loop",
    ):
        _bridge_worker_supervisor_leased(root, payload, stop_event)


def _bridge_worker_supervisor_leased(root: Path, payload: dict[str, Any], stop_event: threading.Event) -> None:
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
    worker_id = str(payload.get("_bridge_worker_id") or "")
    restart_count = 0
    final_status = ""

    def _mark(status: str, error: str = "") -> None:
        with _BRIDGE_LOCK:
            worker = _BRIDGE_WORKERS.get(key, {})
            if worker_id and str(worker.get("worker_id") or "") != worker_id:
                return
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
    with history_writer_fence_if_owned(root, label="sidebar_send_bridge_start"):
        _start_bridge_worker_fenced(root, payload)


def _start_bridge_worker_fenced(root: Path, payload: dict[str, Any]) -> None:
    """Start the supervised send-bridge worker for this data dir if not running.

    Only starts when the active send driver is bridge_outbox; that is the only
    driver whose replies need a bridge to deliver them. Idempotent: a live worker
    with the same backend config is left as-is; a live worker with stale backend
    config is stopped and replaced so backend switches take effect.
    """
    key = str(root)
    try:
        config = load_config(root)
    except Exception:
        return
    if not _bridge_worker_should_run(config):
        return
    config_signature = _bridge_worker_config_signature(config)
    worker_id = f"bridge-worker-{uuid4().hex[:12]}"
    thread_to_join: threading.Thread | None = None
    with _BRIDGE_LOCK:
        existing = _BRIDGE_WORKERS.get(key)
        thread = existing.get("thread") if existing else None
        if isinstance(thread, threading.Thread) and thread.is_alive():
            existing_signature = existing.get("config_signature") if isinstance(existing.get("config_signature"), dict) else {}
            if existing_signature == config_signature:
                return
            stop = existing.get("stop")
            if isinstance(stop, threading.Event):
                stop.set()
            existing["stop_requested"] = True
            existing["last_status"] = "restarting_for_config"
            _BRIDGE_WORKERS[key] = existing
            thread_to_join = thread
    if thread_to_join is not None:
        thread_to_join.join(timeout=1.5)
        if thread_to_join.is_alive():
            return
    if bridge_worker_lock_alive(root):
        if not _repair_stale_external_bridge_worker(root, config_signature):
            return
    with _BRIDGE_LOCK:
        existing = _BRIDGE_WORKERS.get(key)
        thread = existing.get("thread") if existing else None
        if isinstance(thread, threading.Thread) and thread.is_alive():
            return
        stop_event = threading.Event()
        worker_payload = dict(payload)
        worker_payload["_bridge_worker_id"] = worker_id
        history_lease = register_history_writer_lease_if_owned(
            root,
            label="sidebar_send_bridge_loop",
        )
        worker_thread = threading.Thread(
            target=_run_history_leased_thread,
            args=(
                _bridge_worker_supervisor,
                (root, worker_payload, stop_event),
                history_lease,
            ),
            name="sidebar-send-bridge",
            daemon=True,
        )
        _BRIDGE_WORKERS[key] = {
            "thread": worker_thread,
            "stop": stop_event,
            "started_at": time.time(),
            "last_status": "starting",
            "restart_count": 0,
            "last_error": "",
            "stop_requested": False,
            "worker_id": worker_id,
            "config_signature": config_signature,
        }
        try:
            worker_thread.start()
        except BaseException:
            _BRIDGE_WORKERS.pop(key, None)
            if history_lease is not None:
                history_lease.release()
            raise


def _repair_stale_external_bridge_worker(root: Path, expected_signature: dict[str, Any]) -> bool:
    """Stop a stale external bridge worker so a current-config worker can start.

    The send bridge is single-instance by design. A separately launched
    ``scripts/send_bridge_worker.py`` keeps the lock fresh, so the sidebar cannot
    replace it via the in-process stop event. We only repair locks that clearly
    belong to a send-bridge worker for this exact data dir; unknown legacy locks
    remain a blocker for operator review.
    """

    lock = _bridge_worker_lock_snapshot(root)
    if not lock.get("alive"):
        return True
    lock_signature = lock.get("config_signature") if isinstance(lock.get("config_signature"), dict) else {}
    if lock_signature == expected_signature:
        return False
    if str(lock.get("label") or "") != "send_bridge_worker":
        return False
    lock_data_dir = str(lock.get("data_dir") or "").strip()
    if not lock_data_dir:
        return False
    try:
        if Path(lock_data_dir).resolve() != root.resolve():
            return False
    except OSError:
        return False
    try:
        pid = int(lock.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    recorded_start = str(lock.get("process_start") or "")
    if not bool(lock.get("pid_alive")):
        return not bridge_worker_lock_alive(root)
    current_start = process_start_marker(pid)
    if not recorded_start or not current_start or current_start != recorded_start:
        return False
    if not _terminate_verified_process(pid, expected_process_start=recorded_start):
        return False
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline and _pid_exists(pid):
        time.sleep(0.1)
    if _pid_exists(pid):
        if not _terminate_verified_process(pid, force=True, expected_process_start=recorded_start):
            return False
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and _pid_exists(pid):
            time.sleep(0.1)
    if _pid_exists(pid):
        return False
    return not bridge_worker_lock_alive(root)


def _terminate_verified_process(
    pid: int,
    *,
    force: bool = False,
    expected_process_start: str = "",
) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    if expected_process_start and process_start_marker(pid) != expected_process_start:
        return False
    if os.name == "nt":
        return _terminate_windows_verified_process_handle(
            pid,
            expected_process_start=expected_process_start,
        )
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError as exc:
        logger.warning("bridge stale worker terminate failed for pid %s: %s", pid, exc)
        return False
    return True


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
    with _BRIDGE_LOCK:
        worker = _BRIDGE_WORKERS.get(key)
        thread = worker.get("thread") if worker else None
        if worker and not (isinstance(thread, threading.Thread) and thread.is_alive()):
            worker["last_status"] = "stopped"
            worker["stop_requested"] = False
            _BRIDGE_WORKERS[key] = worker


def _weflow_background_loop(
    root: Path,
    payload: dict[str, Any],
    stop_event: threading.Event,
) -> None:
    with history_writer_lease_if_owned(
        root,
        label="sidebar_weflow_loop",
    ):
        _weflow_background_loop_leased(root, payload, stop_event)


def _weflow_background_loop_leased(root: Path, payload: dict[str, Any], stop_event: threading.Event) -> None:
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
        # A clean stop request means we're done; do not resurrect.
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
        # This is a deliberate single-instance refusal, NOT a crash; signal the
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
    capture_only = bool(payload.get("capture_only") or payload.get("captureOnly"))
    requested_process_backend_events = payload.get("process_backend_events")
    if requested_process_backend_events is None:
        requested_process_backend_events = payload.get("processBackendEvents")
    process_backend_events = not capture_only if requested_process_backend_events is None else bool(requested_process_backend_events)
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
        "process_backend_events": process_backend_events,
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
    with history_writer_fence_if_owned(root, label=label):
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
        config = load_config(data_dir)
    except Exception as exc:
        return {
            "registered_count": 0,
            "registered_channels": [],
            "skipped_count": 0,
            "skipped_channels": [],
            "registration_errors": [{"type": type(exc).__name__, "message": str(exc)}],
        }
    registered: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for session in sessions:
        session_id = str(session.get("id") or "").strip()
        if not session_id:
            continue
        try:
            message = _weflow_session_channel_message(session)
            admission = channel_admission_for_session(
                session,
                config,
                existing_channel=store.get_channel(message.conversation_id) or False,
            )
            if not admission.allowed:
                skipped.append(
                    {
                        "id": session_id,
                        "name": str(session.get("name") or session_id),
                        "type": str(session.get("type") or ""),
                        "reason": admission.reason,
                    }
                )
                continue
            channel = store.ensure_channel(message)
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
        "skipped_count": len(skipped),
        "skipped_channels": skipped,
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


def _annotate_weflow_sessions_with_registration(
    sessions: list[dict[str, Any]],
    registration: dict[str, Any],
) -> list[dict[str, Any]]:
    registered = {
        str(item.get("id") or ""): item
        for item in registration.get("registered_channels", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    skipped = {
        str(item.get("id") or ""): item
        for item in registration.get("skipped_channels", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    annotated: list[dict[str, Any]] = []
    for session in sessions:
        session_id = str(session.get("id") or "").strip()
        channel = registered.get(session_id, {})
        blocked = skipped.get(session_id, {})
        annotated.append(
            {
                **session,
                "conversation_id": channel.get("conversation_id") or "",
                "conversation_type": channel.get("conversation_type") or "",
                "chat_title": channel.get("chat_title") or "",
                "channel_registration_status": "registered" if channel else ("blocked" if blocked else "unknown"),
                "channel_blocked_reason": str(blocked.get("reason") or "") if blocked else "",
            }
        )
    return annotated


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
    skipped = {
        str(item.get("id") or ""): item
        for item in (registration or {}).get("skipped_channels", [])
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
        skipped_channel = skipped.get(session_id, {})
        display_name = _preferred_weflow_display_name(
            normalized.get("name"),
            existing.get("name") if isinstance(existing, dict) else "",
            channel.get("chat_title") if isinstance(channel, dict) else "",
            session_id,
        )
        channel_status = "registered" if channel else ("blocked" if skipped_channel else str(existing.get("channel_registration_status") or "unknown"))
        merged = {
            **existing,
            **normalized,
            "id": session_id,
            "name": display_name,
            "type": str(normalized.get("type") or existing.get("type") or ("group" if session_id.endswith("@chatroom") else "private")),
            "conversation_id": channel.get("conversation_id") or ("" if skipped_channel else existing.get("conversation_id") or ""),
            "conversation_type": channel.get("conversation_type") or ("" if skipped_channel else existing.get("conversation_type") or ""),
            "chat_title": _preferred_weflow_display_name(
                channel.get("chat_title") if isinstance(channel, dict) else "",
                existing.get("chat_title") if isinstance(existing, dict) else "",
                display_name,
                session_id,
            ),
            "channel_registration_status": channel_status,
            "channel_blocked_reason": str(skipped_channel.get("reason") or "") if skipped_channel else "",
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
    talkers = params.get("talkers") if isinstance(params.get("talkers"), list) else []
    return {
        "base_url": params.get("base_url", ""),
        "token_env": params.get("token_env", "WEFLOW_API_TOKEN"),
        "token_present": bool(params.get("token")),
        "token_source": params.get("token_source", "missing"),
        "allow_non_local": bool(params.get("allow_non_local")),
        "talkers": list(talkers),
        "talker_count": len(talkers),
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


def _weflow_pull_job_loop(
    root: Path,
    payload: dict[str, Any],
    job_id: str,
) -> None:
    with history_writer_lease_if_owned(
        root,
        label="sidebar_weflow_pull_job",
        metadata={"job_id": job_id},
    ):
        _weflow_pull_job_loop_leased(root, payload, job_id)


def _weflow_pull_job_loop_leased(root: Path, payload: dict[str, Any], job_id: str) -> None:
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
        "metadata": _worker_task_metadata(
            scope_label="WeFlow后台拉取",
            worker_kind="weflow" if job_id == "worker" else "weflow_job",
            last_status=status,
            stale_after_seconds=180.0,
            extra={
                "job_id": job_id,
                "session_id": session_id,
                "event": event,
                "worker": job_id == "worker",
            },
        ),
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


def _weflow_backfill_job_loop(
    root: Path,
    payload: dict[str, Any],
    job_id: str,
    stop_event: threading.Event,
) -> None:
    with history_writer_lease_if_owned(
        root,
        label="sidebar_weflow_backfill_job",
        metadata={"job_id": job_id},
    ):
        _weflow_backfill_job_loop_leased(root, payload, job_id, stop_event)


def _weflow_backfill_job_loop_leased(
    root: Path,
    payload: dict[str, Any],
    job_id: str,
    stop_event: threading.Event,
) -> None:
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
        final_progress = {
            "scanned_count": result.get("source", {}).get("scanned_count", 0),
            "appended_count": result.get("source", {}).get("appended_count", 0),
            "processed_count": result.get("pull", {}).get("processed_count", 0),
        }
        _update_backfill_job(
            root,
            job_id,
            result=result,
            progress={**final_progress, "event": "finalizing"},
            force=True,
        )
        _record_weflow_state_safely(
            root,
            {"last_backfill": result, "last_error": "", "backfill_job": _weflow_backfill_job_state(root), **_weflow_public_params(_weflow_params(root, payload))},
            action="backfill",
            result=result,
        )
        _update_backfill_job(root, job_id, status=final_status, result=result, progress={**final_progress, "event": final_status}, force=True)
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
    health_fresh, health_age = _weflow_health_freshness(last_health)
    health_ok = health_fresh and str(last_health.get("status") or "") == "ok"
    fork_ok = health_fresh and bool(last_health.get("fork_ok"))
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
    elif last_health and not health_fresh:
        status = "health_stale"
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
        "last_health_checked_at": str(last_health.get("checked_at") or ""),
        "health_fresh": health_fresh,
        "health_age_seconds": health_age,
        "health_ttl_seconds": _WEFLOW_HEALTH_TTL_SECONDS,
        "message": str(last_health.get("message") or last_health.get("error") or ""),
        "updated_at": persisted.get("updated_at", ""),
    }


def _weflow_health_freshness(last_health: dict[str, Any], *, now: datetime | None = None) -> tuple[bool, float | None]:
    checked_at = str(last_health.get("checked_at") or "").strip()
    if not checked_at:
        return False, None
    try:
        checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return False, None
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    raw_age = (current.astimezone(timezone.utc) - checked.astimezone(timezone.utc)).total_seconds()
    age = max(0.0, raw_age)
    # Small negative values can occur around clock synchronization, but a state
    # file dated materially in the future must not remain fresh indefinitely.
    fresh = raw_age >= -5.0 and age <= _WEFLOW_HEALTH_TTL_SECONDS
    return fresh, round(age, 3)


def _read_weflow_sidebar_state(data_dir: str | Path) -> dict[str, Any]:
    with _WEFLOW_STATE_FILE_LOCK:
        return SidebarStateStore(data_dir).read_weflow_state(history_limit=50)


def _write_weflow_sidebar_state(data_dir: str | Path, update: dict[str, Any]) -> None:
    path = Path(data_dir) / "weflow_sidebar_state.json"
    with _WEFLOW_STATE_FILE_LOCK:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        store = SidebarStateStore(data_dir)
        current = store.read_weflow_state(history_limit=50)
        payload = current if isinstance(current, dict) else {}
        payload.update(update)
        payload["updated_at"] = now
        payload = store.update_weflow_state(payload)
        _write_json(path, payload)


def _reset_weflow_sidebar_history(data_dir: str | Path) -> None:
    path = Path(data_dir) / "weflow_sidebar_state.json"
    with _WEFLOW_STATE_FILE_LOCK:
        payload = SidebarStateStore(data_dir).reset_weflow_history()
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
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "action": str(action),
            "status": str(result.get("status", "")) if isinstance(result, dict) else "",
            "summary": _weflow_operation_summary(result),
            "result": _compact_weflow_history_payload(result),
        }
        store = SidebarStateStore(data_dir)
        payload = store.append_weflow_operation_entry(entry, limit=50)
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


def _tail_jsonl(path: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for line in lines[-max(1, int(limit)) :]:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = {"malformed": True, "preview": line[:500]}
        if isinstance(payload, dict):
            items.append(payload)
    return items


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


def _require_sidebar_channel(data_dir: str | Path, conversation_id: str) -> Any:
    channel_id = str(conversation_id or "").strip()
    if not channel_id:
        raise ValueError("conversation_id is required")
    channel = _channel_store(data_dir).get_channel(channel_id)
    if channel is None:
        raise ValueError(f"conversation channel not found: {channel_id}")
    return channel


def _sidebar_test_reply_candidate(
    channel: Any,
    *,
    text: str,
    origin: str,
    attachments: list[dict[str, Any]],
    review_required: bool = True,
) -> ReplyCandidate:
    return ReplyCandidate(
        message_id=f"{origin}:{uuid4().hex[:12]}",
        conversation_id=str(channel.conversation_id),
        text=text,
        send_mode="confirm" if review_required else "auto",
        model="sidebar-manual-test",
        policy_hits=["manual_sidebar_channel_test"],
        attachments=attachments,
        send_metadata={
            "origin": origin,
            "channel_test": True,
            "review_required": review_required,
            "conversation_type": str(getattr(channel, "conversation_type", "") or ""),
        },
    )


def _safe_upload_filename(name: str) -> str:
    filename = Path(str(name or "upload.bin")).name.strip() or "upload.bin"
    invalid = set('<>:"/\\|?*')
    cleaned = "".join("_" if char in invalid or ord(char) < 32 else char for char in filename)
    cleaned = cleaned.strip(" .")
    if not cleaned:
        cleaned = "upload.bin"
    if len(cleaned) > 140:
        suffix = Path(cleaned).suffix[:24]
        stem = Path(cleaned).stem[: max(1, 140 - len(suffix))]
        cleaned = f"{stem}{suffix}" if suffix else stem
    return cleaned


def _decode_upload_base64(content_base64: str) -> bytes:
    raw = str(content_base64 or "").strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:
        return base64.b64decode(raw)


def _upload_kind_for_name(name: str) -> str:
    suffix = Path(str(name or "")).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}:
        return "image"
    if suffix in {".ppt", ".pptx", ".key"}:
        return "presentation"
    if suffix in {".doc", ".docx", ".pdf", ".txt", ".md", ".xlsx", ".xls", ".csv"}:
        return "document"
    return "file"


def _bounded_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return min(max(parsed, lower), upper)


def _bounded_float(value: Any, default: float, lower: float, upper: float) -> float:
    try:
        parsed = float(value)
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
        "topic_rules.json",
        "search_blocklist.json",
        "api_keys.local.md",
        "api_key_models.local.json",
    }
    retained = {Path(os.path.abspath(root / name)) for name in names}
    config = load_config(root)
    for provider in config.providers.values():
        configured = str(provider.api_key_file or "").strip()
        if not configured:
            continue
        key_path = Path(configured)
        if not key_path.is_absolute():
            key_path = root / key_path
        retained.add(Path(os.path.abspath(key_path)))
    return retained


def _existing_relative_paths(root: Path, relatives: tuple[str, ...]) -> list[str]:
    existing: list[str] = []
    for relative in relatives:
        path = (root / relative).resolve()
        if path.exists():
            try:
                existing.append(str(path.relative_to(root)))
            except ValueError:
                continue
    return sorted(existing)


def _remove_history_path(
    root: Path,
    relative: str,
    removed: list[dict[str, Any]],
    retained_locked: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    *,
    expected_kind: str,
    retained_history_paths: set[Path],
) -> None:
    target = _validate_history_reset_target(root, relative, expected_kind=expected_kind)
    if _history_path_lstat(target) is None:
        return
    try:
        if expected_kind in {"dir", "nested_dir"}:
            target_removed = _remove_history_tree(
                target,
                retained_paths=retained_history_paths,
            )
            kind = "dir" if target_removed else "dir_contents"
        else:
            if target in retained_history_paths:
                raise ValueError(f"history reset file conflicts with retained config: {relative}")
            _remove_history_file(target)
            kind = "file"
    except (OSError, ValueError) as exc:
        if (
            isinstance(exc, OSError)
            and expected_kind == "file"
            and relative in _HISTORY_RESET_LOCK_TOLERANT_FILES
            and _is_windows_locked_file_error(exc)
        ):
            retained_locked.append(_locked_history_record(relative, target, exc))
            retained_record = retained_locked[-1]
            _truncate_locked_history_file(target, relative, retained_record)
            if retained_record.get("fallback") != "truncated":
                errors.append(
                    {
                        "relative_path": relative,
                        "path": str(target),
                        "kind": expected_kind,
                        "phase": "truncate_locked_history_file",
                        "error": str(
                            retained_record.get("fallback_error")
                            or "locked history file could not be removed or truncated"
                        ),
                        "winerror": retained_record.get("fallback_winerror"),
                    }
                )
            return
        errors.append(
            {
                "relative_path": relative,
                "path": str(target),
                "kind": expected_kind,
                "error": f"{type(exc).__name__}: {exc}",
                "winerror": getattr(exc, "winerror", None),
            }
        )
        return
    removed.append({"relative_path": relative, "path": str(target), "kind": kind})


def _validate_history_reset_manifest(root: Path) -> None:
    """Validate every fixed reset target before the first destructive action."""

    seen: set[Path] = set()
    for expected_kind, relatives in (
        ("dir", _HISTORY_RESET_DIRS),
        ("nested_dir", _HISTORY_RESET_NESTED_DIRS),
        ("file", _HISTORY_RESET_FILES),
    ):
        for relative in relatives:
            target = _validate_history_reset_target(root, relative, expected_kind=expected_kind)
            if target in seen:
                raise ValueError(f"duplicate history reset target: {relative}")
            seen.add(target)


def _history_reset_uuid_tmp_paths(root: Path) -> tuple[str, ...]:
    """Enumerate only atomic-write orphans for known history state files."""

    found: set[str] = set()
    for base_relative in _HISTORY_RESET_UUID_TMP_BASES:
        base = _validate_history_reset_target(root, base_relative, expected_kind="file")
        parent_stat = _history_path_lstat(base.parent)
        if parent_stat is None:
            continue
        if _history_path_is_reparse_point(parent_stat) or not stat.S_ISDIR(parent_stat.st_mode):
            raise ValueError(f"history reset temp parent is unsafe: {base_relative}")
        name_pattern = re.compile(rf"^{re.escape(base.name)}\.[0-9a-f]{{32}}\.tmp$")
        with os.scandir(base.parent) as entries:
            names = sorted(entry.name for entry in entries if name_pattern.fullmatch(entry.name))
        for name in names:
            relative = (Path(base_relative).parent / name).as_posix()
            target = _validate_history_reset_target(root, relative, expected_kind="file")
            target_stat = _history_path_lstat(target)
            if target_stat is None:
                continue
            if not _history_path_is_private_regular_file(target_stat):
                raise ValueError(f"history reset orphan temp must be private and regular: {relative}")
            found.add(relative)
    return tuple(sorted(found))


def _load_owned_history_config(root: Path) -> None:
    _require_owned_history_config_file(root)
    load_config(root)
    _require_owned_history_config_file(root)


def _require_owned_history_config_file(root: Path) -> None:
    config_path = root / "config.json"
    config_stat = _history_path_lstat(config_path)
    if config_stat is None:
        raise ConfigError(f"missing config: {config_path}; run init first")
    if (
        _history_path_is_reparse_point(config_stat)
        or not stat.S_ISREG(config_stat.st_mode)
        or int(getattr(config_stat, "st_nlink", 1) or 1) != 1
    ):
        raise ValueError("history reset config.json must be a private regular file")


def _validate_history_clear_preflight(root: Path) -> None:
    _validate_history_reset_manifest(root)
    orphan_tmp_paths = _history_reset_uuid_tmp_paths(root)
    _validate_retained_history_paths(
        root,
        _retained_config_paths(root),
        extra_file_relatives=orphan_tmp_paths,
    )
    for relative in _HISTORY_RESET_LOCK_TOLERANT_FILES:
        _validate_private_history_file(root, relative, purpose="lock-tolerant file")
    for relative in ("runtime", "runtime_locks", "send_bridge"):
        _validate_history_reset_target(root, relative, expected_kind="dir")
    _validate_private_history_file(root, "runtime/sidebar_launch.json", purpose="sidebar launch state")
    for relative in (
        "runtime/history_reset_shutdown.lock",
        "runtime/history_reset_shutdown.json",
        "runtime/history_reset_shutdown.out.log",
        "runtime/history_reset_shutdown.err.log",
        "runtime_locks/sidebar_agent_tick.lock",
        "runtime_locks/sidebar_agent_tick.lock.guard",
        "runtime_locks/history_reset_fence.lock",
        "runtime_locks/history_reset_fence.lock.guard",
        "runtime_locks/history_reset_shutdown_schedule.lock",
        "runtime_locks/history_reset_shutdown_schedule.lock.guard",
        "runtime_locks/sidebar_frontend_lifecycle.lock",
        "runtime_locks/sidebar_frontend_lifecycle.lock.guard",
        "runtime_locks/weflow_lifecycle.lock",
        "runtime_locks/weflow_lifecycle.lock.guard",
        "weflow_global_operation.lock",
        "weflow_global_operation.lock.guard",
        "send_bridge/.bridge_worker.lock",
        "send_bridge/.bridge_worker.lock.guard",
    ):
        target = _validate_history_reset_target(root, relative, expected_kind="file")
        target_stat = _history_path_lstat(target)
        if target_stat is not None and int(getattr(target_stat, "st_nlink", 1) or 1) != 1:
            raise ValueError(f"history reset control file must not be hardlinked: {relative}")
    for relative in _HISTORY_CLEAR_WRITABLE_CONTROL_FILES:
        _validate_private_history_file(root, relative, purpose="writable control file")
    for relative in _HISTORY_PRESERVED_RUNTIME_PATHS:
        _validate_private_history_file(root, relative, purpose="preserved runtime file")


def _validate_private_history_file(root: Path, relative: str, *, purpose: str) -> None:
    target = _validate_history_reset_target(root, relative, expected_kind="file")
    target_stat = _history_path_lstat(target)
    if target_stat is not None and not _history_path_is_private_regular_file(target_stat):
        raise ValueError(f"history reset {purpose} must be private and regular: {relative}")


def _resolve_history_data_root(data_dir: str | Path) -> Path:
    """Resolve a destructive-operation root without accepting path aliases."""

    lexical_root = Path(os.path.abspath(os.fspath(data_dir)))
    root_stat = _history_path_lstat(lexical_root)
    if root_stat is not None and _history_path_is_reparse_point(root_stat):
        raise ValueError("history reset data_dir must not be a symlink or reparse point")
    try:
        canonical_root = lexical_root.resolve(strict=False)
    except OSError as exc:
        raise ValueError("history reset data_dir cannot be resolved safely") from exc
    if canonical_root != lexical_root:
        raise ValueError("history reset data_dir must use its canonical path")
    return canonical_root


def _validate_history_reset_target(root: Path, relative: str, *, expected_kind: str) -> Path:
    if expected_kind not in {"dir", "nested_dir", "file"}:
        raise ValueError(f"unsupported history reset target kind: {expected_kind}")

    root = root.resolve()
    relative_path = Path(relative)
    parts = relative_path.parts
    if (
        not parts
        or relative_path.is_absolute()
        or relative_path.anchor
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"invalid history reset target: {relative}")
    if expected_kind == "dir" and len(parts) != 1:
        raise ValueError(f"history reset directory must be a direct child of data_dir: {relative}")

    target = root.joinpath(*parts)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"history reset target escapes data_dir: {relative}") from exc

    for index in range(1, len(parts) + 1):
        component = root.joinpath(*parts[:index])
        path_stat = _history_path_lstat(component)
        if path_stat is None:
            continue
        if _history_path_is_reparse_point(path_stat):
            raise ValueError(f"history reset target uses a symlink or reparse point: {relative}")
        if index < len(parts) and not stat.S_ISDIR(path_stat.st_mode):
            raise ValueError(f"history reset target parent is not a directory: {relative}")

    target_stat = _history_path_lstat(target)
    if target_stat is not None:
        if expected_kind in {"dir", "nested_dir"} and not stat.S_ISDIR(target_stat.st_mode):
            raise ValueError(f"history reset directory has unexpected type: {relative}")
        if expected_kind == "file" and not stat.S_ISREG(target_stat.st_mode):
            raise ValueError(f"history reset file has unexpected type: {relative}")

    try:
        canonical_target = target.resolve(strict=False)
    except OSError as exc:
        raise ValueError(f"history reset target cannot be resolved safely: {relative}") from exc
    if canonical_target != target:
        raise ValueError(f"history reset target is not its expected canonical path: {relative}")
    return target


def _history_path_lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _history_path_is_reparse_point(path_stat: os.stat_result) -> bool:
    if stat.S_ISLNK(path_stat.st_mode):
        return True
    attributes = int(getattr(path_stat, "st_file_attributes", 0) or 0)
    reparse_attribute = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_attribute)


def _history_path_is_private_regular_file(path_stat: os.stat_result) -> bool:
    return (
        not _history_path_is_reparse_point(path_stat)
        and stat.S_ISREG(path_stat.st_mode)
        and int(getattr(path_stat, "st_nlink", 1) or 1) == 1
    )


def _validate_retained_history_paths(
    root: Path,
    retained: set[Path],
    *,
    extra_file_relatives: tuple[str, ...] = (),
) -> set[Path]:
    """Return explicit config files nested under reset trees, or fail closed."""

    directory_targets = [
        root / relative
        for relative in (*_HISTORY_RESET_DIRS, *_HISTORY_RESET_NESTED_DIRS)
    ]
    file_targets = [
        root / relative
        for relative in (
            *_HISTORY_RESET_FILES,
            *_HISTORY_CLEAR_WRITABLE_CONTROL_FILES,
            *_HISTORY_CLEAR_MUTATED_PRESERVED_FILES,
            *extra_file_relatives,
        )
    ]
    protected: set[Path] = set()
    for path in retained:
        if any(path == target or path in target.parents for target in (*directory_targets, *file_targets)):
            raise ValueError(f"retained config path contains a history reset target: {path}")
        if any(path == target or target in path.parents for target in file_targets):
            raise ValueError(f"retained config path conflicts with a history reset file: {path}")
        containing_tree = next((target for target in directory_targets if target in path.parents), None)
        if containing_tree is None:
            continue
        path_stat = _history_path_lstat(path)
        if path_stat is None:
            continue
        current = containing_tree
        relative = path.relative_to(containing_tree)
        for part in relative.parts[:-1]:
            current = current / part
            current_stat = _history_path_lstat(current)
            if (
                current_stat is None
                or _history_path_is_reparse_point(current_stat)
                or not stat.S_ISDIR(current_stat.st_mode)
            ):
                raise ValueError(f"retained config path has an unsafe parent: {path}")
        if _history_path_is_reparse_point(path_stat) or not stat.S_ISREG(path_stat.st_mode):
            raise ValueError(f"retained config path must be a regular file: {path}")
        protected.add(path)
    return protected


def _remove_history_tree(
    target: Path,
    *,
    hardlink_counts: dict[tuple[int, int], int] | None = None,
    retained_paths: set[Path] | None = None,
) -> bool:
    """Remove resettable trees that may contain immutable workspace artifacts."""

    retained_paths = retained_paths or set()
    target_stat = _history_path_lstat(target)
    if target_stat is None:
        return True
    if _history_path_is_reparse_point(target_stat) or not stat.S_ISDIR(target_stat.st_mode):
        raise ValueError(f"refusing to traverse unsafe history reset tree: {target}")
    if hardlink_counts is None:
        hardlink_counts = _history_tree_hardlink_counts(target, retained_paths=retained_paths)

    with os.scandir(target) as iterator:
        entries = list(iterator)
    for entry in entries:
        path = Path(entry.path)
        path_stat = _history_path_lstat(path)
        if path_stat is None:
            continue
        if path in retained_paths:
            if _history_path_is_reparse_point(path_stat) or not stat.S_ISREG(path_stat.st_mode):
                raise ValueError(f"retained config path changed type during history reset: {path}")
            continue
        contains_retained = any(path in retained.parents for retained in retained_paths)
        if contains_retained and (
            _history_path_is_reparse_point(path_stat)
            or not stat.S_ISDIR(path_stat.st_mode)
        ):
            raise ValueError(f"retained config path parent changed type during history reset: {path}")
        if _history_path_is_reparse_point(path_stat):
            _remove_history_reparse_point(path, path_stat)
        elif stat.S_ISDIR(path_stat.st_mode):
            _remove_history_tree(
                path,
                hardlink_counts=hardlink_counts,
                retained_paths=retained_paths,
            )
        else:
            _remove_history_file(path, hardlink_counts=hardlink_counts)
    if any(target in retained.parents for retained in retained_paths):
        return False
    _make_history_path_writable(target)
    target.rmdir()
    return True


def _terminate_windows_verified_process_handle(pid: int, *, expected_process_start: str) -> bool:
    """Terminate one Windows process after same-handle identity verification."""

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (ImportError, OSError) as exc:
        logger.warning("bridge stale worker Win32 API unavailable for pid %s: %s", pid, exc)
        return False

    process_terminate = 0x0001
    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    wait_object_0 = 0
    wait_timeout = 258
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    access = process_terminate | process_query_limited_information | synchronize
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        error_code = int(ctypes.get_last_error())
        return error_code in {87, 1168}
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return False
        current_start = f"win:{(int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)}"
        if current_start != str(expected_process_start or ""):
            return False
        if int(kernel32.WaitForSingleObject(handle, 0)) == wait_object_0:
            return True
        if not kernel32.TerminateProcess(handle, 1):
            return False
        wait_result = int(kernel32.WaitForSingleObject(handle, 8000))
        if wait_result == wait_object_0:
            return True
        if wait_result != wait_timeout:
            logger.warning("bridge stale worker wait returned %s for pid %s", wait_result, pid)
        return False
    finally:
        kernel32.CloseHandle(handle)


def _remove_history_reparse_point(path: Path, path_stat: os.stat_result) -> None:
    """Remove an alias itself without following or changing its destination."""

    if stat.S_ISDIR(path_stat.st_mode):
        path.rmdir()
    else:
        path.unlink()


def _remove_history_file(
    target: Path,
    *,
    hardlink_counts: dict[tuple[int, int], int] | None = None,
) -> None:
    try:
        target.unlink()
    except PermissionError:
        target_stat = _history_path_lstat(target)
        if (
            target_stat is not None
            and int(getattr(target_stat, "st_nlink", 1) or 1) > 1
            and not _history_tree_contains_all_hardlinks(hardlink_counts, target_stat)
        ):
            raise PermissionError(f"refusing to change permissions on multiply-linked history file: {target}")
        _make_history_path_writable(target)
        target.unlink()


def _history_tree_hardlink_counts(
    root: Path,
    *,
    retained_paths: set[Path] | None = None,
) -> dict[tuple[int, int], int]:
    counts: dict[tuple[int, int], int] = {}
    retained_paths = retained_paths or set()
    pending = [root]
    while pending:
        current = pending.pop()
        current_stat = _history_path_lstat(current)
        if current_stat is None or _history_path_is_reparse_point(current_stat):
            continue
        if stat.S_ISDIR(current_stat.st_mode):
            with os.scandir(current) as iterator:
                pending.extend(Path(entry.path) for entry in iterator)
            continue
        if current in retained_paths:
            continue
        if int(getattr(current_stat, "st_nlink", 1) or 1) <= 1:
            continue
        identity = _history_file_identity(current_stat)
        if identity is not None:
            counts[identity] = counts.get(identity, 0) + 1
    return counts


def _history_tree_contains_all_hardlinks(
    counts: dict[tuple[int, int], int] | None,
    target_stat: os.stat_result,
) -> bool:
    if counts is None:
        return False
    expected = int(getattr(target_stat, "st_nlink", 1) or 1)
    if expected <= 1:
        return True
    identity = _history_file_identity(target_stat)
    return identity is not None and counts.get(identity, 0) >= expected


def _history_file_identity(path_stat: os.stat_result) -> tuple[int, int] | None:
    identity = (
        int(getattr(path_stat, "st_dev", 0) or 0),
        int(getattr(path_stat, "st_ino", 0) or 0),
    )
    return identity if identity[1] else None


def _make_history_path_writable(path: Path) -> None:
    path_stat = _history_path_lstat(path)
    if path_stat is None or _history_path_is_reparse_point(path_stat):
        return
    try:
        path.chmod(path_stat.st_mode | stat.S_IWUSR)
    except OSError:
        return


def _write_weflow_history_reset_barrier(
    root: Path,
    *,
    reset_id: str,
    reset_at_epoch: int,
) -> dict[str, Any]:
    payload = {
        "version": 1,
        "history_reset_id": str(reset_id),
        "history_reset_epoch": int(reset_at_epoch),
        "sessions": {},
        "seen_raw_ids": [],
    }
    _write_json(root / "weflow_bridge_state.json", payload)
    return {
        "path": "weflow_bridge_state.json",
        "history_reset_id": str(reset_id),
        "history_reset_epoch": int(reset_at_epoch),
    }


def _archive_send_bridge_for_history_reset(
    root: Path,
    *,
    reset_id: str,
    reset_at_epoch: int,
) -> dict[str, Any]:
    archive = BridgeOutboxStore(root).archive_for_history_reset(
        reset_id=reset_id,
        reset_at_epoch=reset_at_epoch,
    )
    fingerprints = archive.pop("terminal_sync_fingerprints", {})
    accepted_ids = archive.pop("accepted_ids", [])
    reset_metadata = {
        "history_reset_id": str(reset_id),
        "history_reset_epoch": int(reset_at_epoch),
        "archived_count": int(archive.get("archived_count", 0) or 0),
    }

    synced_path = root / "send_bridge" / "synced_acks.json"
    synced_payload = _read_json(synced_path, {})
    if not isinstance(synced_payload, dict):
        synced_payload = {}
    synced = synced_payload.get("synced")
    if not isinstance(synced, dict):
        synced = {}
    synced.update(
        {
            str(bridge_id): str(fingerprint)
            for bridge_id, fingerprint in fingerprints.items()
            if str(bridge_id) and str(fingerprint)
        }
    )
    synced_payload.update({"version": 3, "synced": synced, "history_reset": reset_metadata})
    _write_json(synced_path, synced_payload)

    reverify_path = root / "send_bridge" / "accepted_reverify.json"
    reverify_payload = _read_json(reverify_path, {})
    if not isinstance(reverify_payload, dict):
        reverify_payload = {}
    reverify_items = reverify_payload.get("items")
    if not isinstance(reverify_items, dict):
        reverify_items = {}
    for bridge_id in accepted_ids:
        bridge_id = str(bridge_id or "").strip()
        if not bridge_id:
            continue
        previous = reverify_items.get(bridge_id)
        previous = previous if isinstance(previous, dict) else {}
        reverify_items[bridge_id] = {
            **previous,
            "history_reset_frozen": True,
            "history_reset_id": str(reset_id),
            "history_reset_epoch": int(reset_at_epoch),
        }
    reverify_payload.update({"items": reverify_items, "history_reset": reset_metadata})
    _write_json(reverify_path, reverify_payload)
    return {**archive, **reset_metadata, "accepted_frozen_count": len(accepted_ids)}


def _send_bridge_history_reset_progress(root: Path, *, reset_id: str) -> dict[str, Any]:
    """Best-effort snapshot after an interrupted bridge archive phase."""

    try:
        store = BridgeOutboxStore(root)
        states = effective_bridge_ack_states(store._read_all(store.ack_path))
        frozen_ids = sorted(
            bridge_id
            for bridge_id, state in states.items()
            if isinstance(state.ack.get("payload"), dict)
            and state.ack["payload"].get("history_reset_frozen") is True
            and str(state.ack["payload"].get("history_reset_id") or "") == str(reset_id)
        )
        return {
            "status": "partial_error",
            "history_reset_id": str(reset_id),
            "frozen_terminal_count": len(frozen_ids),
            "frozen_bridge_ids": frozen_ids,
            "progress_unavailable": False,
        }
    except Exception:
        return {
            "status": "partial_error",
            "history_reset_id": str(reset_id),
            "frozen_terminal_count": 0,
            "frozen_bridge_ids": [],
            "progress_unavailable": True,
        }


def _reinitialize_history_runtime_files(root: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """Recreate empty review files and report preserved bridge files.

    The reset may clear conversation history, but the verified native bridge
    state is kept intact so a working non-foreground route is not erased.
    """

    created: list[str] = []
    errors: list[dict[str, Any]] = []
    for relative in ("confirm_queue.jsonl", "send_audit.jsonl"):
        path = root / relative
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("", encoding="utf-8")
                created.append(relative)
        except OSError as exc:
            errors.append(
                {
                    "relative_path": relative,
                    "path": str(path),
                    "kind": "runtime_reinitialize",
                    "phase": "reinitialize_runtime_file",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    try:
        ConfirmQueue(root / "confirm_queue.jsonl").list_pending()
        if (root / "confirm_queue.sqlite").exists():
            created.append("confirm_queue.sqlite")
    except Exception as exc:
        errors.append(
            {
                "relative_path": "confirm_queue.sqlite",
                "path": str(root / "confirm_queue.sqlite"),
                "kind": "runtime_reinitialize",
                "phase": "reinitialize_confirm_queue",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    try:
        SendAuditLog(root / "send_audit.jsonl").list_recent(limit=1)
        if (root / "send_audit.sqlite").exists():
            created.append("send_audit.sqlite")
    except Exception as exc:
        errors.append(
            {
                "relative_path": "send_audit.sqlite",
                "path": str(root / "send_audit.sqlite"),
                "kind": "runtime_reinitialize",
                "phase": "reinitialize_send_audit",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    try:
        bridge_snapshot = bridge_state(root, limit=1)
        for key in ("outbox_path", "ack_path"):
            value = str(bridge_snapshot.get(key) or "")
            if value:
                created.append(str(Path(value).relative_to(root)))
    except Exception as exc:
        errors.append(
            {
                "relative_path": "send_bridge",
                "path": str(root / "send_bridge"),
                "kind": "runtime_reinitialize",
                "phase": "verify_preserved_send_bridge",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    try:
        TaskStatusStore(root).state()
    except Exception as exc:
        errors.append(
            {
                "relative_path": "task_manager",
                "path": str(root / "task_manager"),
                "kind": "runtime_reinitialize",
                "phase": "reinitialize_task_status",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    return sorted(dict.fromkeys(created)), errors


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
    descriptor: int | None = None
    try:
        before = _history_path_lstat(target)
        if before is None or not _history_path_is_private_regular_file(before):
            raise ValueError("locked history file is not a private regular file")
        before_identity = _history_file_identity(before)
        if before_identity is None:
            raise ValueError("locked history file identity is unavailable")
        descriptor = os.open(str(target), os.O_WRONLY | int(getattr(os, "O_BINARY", 0) or 0))
        opened = os.fstat(descriptor)
        current = _history_path_lstat(target)
        if (
            current is None
            or not _history_path_is_private_regular_file(opened)
            or not _history_path_is_private_regular_file(current)
            or _history_file_identity(opened) != before_identity
            or _history_file_identity(current) != before_identity
        ):
            raise ValueError("locked history file changed identity before truncation")
        os.ftruncate(descriptor, 0)
        record["fallback"] = "truncated"
    except (OSError, ValueError) as exc:
        record["fallback"] = "retained"
        record["fallback_error"] = f"{type(exc).__name__}: {exc}"
        record["fallback_winerror"] = getattr(exc, "winerror", None)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _schedule_sidebar_history_reset_shutdown(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    spawn_state = {"started": False}
    try:
        with blocking_process_lock(
            root / "runtime_locks" / "history_reset_shutdown_schedule.lock",
            label="history_reset_shutdown_schedule",
            stale_after_seconds=3600.0,
            wait_timeout_seconds=30.0,
        ):
            return _schedule_sidebar_history_reset_shutdown_locked(
                root,
                payload,
                spawn_state=spawn_state,
            )
    except HistoryResetNotScheduledError:
        raise
    except Exception as exc:
        if spawn_state["started"]:
            raise
        raise HistoryResetNotScheduledError(str(exc)) from exc


def _schedule_sidebar_history_reset_shutdown_locked(
    root: Path,
    payload: dict[str, Any],
    *,
    spawn_state: dict[str, bool] | None = None,
) -> dict[str, Any]:
    runtime_dir = root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    status_path = runtime_dir / "history_reset_shutdown.json"
    active = _active_sidebar_history_reset_shutdown(runtime_dir)
    if active:
        return active
    if status_path.exists():
        persisted_status = _read_json(status_path, None)
        if not isinstance(persisted_status, dict):
            return _deduped_sidebar_history_reset_shutdown(
                runtime_dir / "history_reset_shutdown.lock",
                {},
                outcome_unknown=True,
            )
        if _history_reset_payload_is_nonterminal(persisted_status):
            helper_state = _nonterminal_history_reset_helper_state(persisted_status)
            if helper_state != "inactive":
                return _deduped_sidebar_history_reset_shutdown(
                    runtime_dir / "history_reset_shutdown.lock",
                    persisted_status,
                    helper_pid=_int_value(persisted_status.get("helper_pid"), 0),
                    outcome_unknown=True,
                )
        elif not _history_reset_payload_is_terminal(persisted_status):
            return _deduped_sidebar_history_reset_shutdown(
                runtime_dir / "history_reset_shutdown.lock",
                persisted_status,
                outcome_unknown=True,
            )
    lock_path = runtime_dir / "history_reset_shutdown.lock"
    shutdown_owner_token = uuid4().hex
    if not _try_acquire_sidebar_history_reset_shutdown_lock(
        lock_path,
        owner_token=shutdown_owner_token,
    ):
        active = _active_sidebar_history_reset_shutdown(runtime_dir)
        if active:
            return active
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return _deduped_sidebar_history_reset_shutdown(
                lock_path,
                _read_json(status_path, {}),
                outcome_unknown=True,
            )
        if not _try_acquire_sidebar_history_reset_shutdown_lock(
            lock_path,
            owner_token=shutdown_owner_token,
        ):
            return _deduped_sidebar_history_reset_shutdown(
                lock_path,
                _read_json(status_path, {}),
                outcome_unknown=True,
            )

    launch_state_path = root / "runtime" / "sidebar_launch.json"
    launch_state = _read_json(launch_state_path, None)
    if not launch_state_path.exists():
        _remove_sidebar_history_reset_shutdown_lock(lock_path)
        raise RuntimeError("current sidebar launch state is required for verified shutdown")
    if not isinstance(launch_state, dict) or not launch_state:
        _remove_sidebar_history_reset_shutdown_lock(lock_path)
        raise RuntimeError("current sidebar launch state is unreadable or incomplete")
    launch_state = launch_state if isinstance(launch_state, dict) else {}
    launch_state_data_dir = str(launch_state.get("data_dir") or "").strip()
    launch_state_process_start = str(launch_state.get("process_start") or "").strip()
    launch_state_trusted = False
    launch_state_pid = _int_value(launch_state.get("pid"), 0)
    launch_state_data_matches = False
    if launch_state_data_dir:
        try:
            launch_state_data_matches = Path(launch_state_data_dir).resolve() == root
        except OSError:
            launch_state_data_matches = False
    if launch_state_pid == os.getpid() and launch_state_data_matches:
        current_process_start = process_start_marker(os.getpid())
        if not launch_state_process_start or not current_process_start:
            _remove_sidebar_history_reset_shutdown_lock(lock_path)
            raise RuntimeError("current sidebar launch process identity is unavailable")
        launch_state_trusted = current_process_start == launch_state_process_start
    if launch_state_path.exists() and not launch_state_trusted:
        _remove_sidebar_history_reset_shutdown_lock(lock_path)
        raise RuntimeError("current sidebar launch process identity could not be verified")
    weflow_result = launch_state.get("weflow_result") if isinstance(launch_state.get("weflow_result"), dict) else {}
    parent_pid = os.getpid()
    parent_process_start = launch_state_process_start
    weflow_pid = _int_value(
        launch_state.get("weflow_pid") or weflow_result.get("pid"),
        0,
    )
    weflow_process_start = str(
        launch_state.get("weflow_process_start") or weflow_result.get("process_start") or ""
    )
    weflow_mode = str(launch_state.get("weflow") or "auto")
    if weflow_mode not in {"auto", "on", "off"}:
        weflow_mode = "auto"
    weflow_port = _int_value(launch_state.get("weflow_port"), 5031)
    helper = Path(__file__).resolve().parents[3] / "scripts" / "sidebar_history_reset_shutdown.py"
    command = [
        sys.executable,
        str(helper),
        "--data-dir",
        str(root),
        "--parent-pid",
        str(parent_pid),
        "--parent-process-start",
        parent_process_start,
        "--shutdown-owner-token",
        shutdown_owner_token,
        "--weflow",
        weflow_mode,
        "--weflow-port",
        str(weflow_port),
        "--weflow-pid",
        str(weflow_pid),
        "--weflow-process-start",
        weflow_process_start,
    ]
    status = {
        "status": "shutdown_scheduled",
        "phase": "scheduled",
        "parent_pid": parent_pid,
        "parent_process_start": parent_process_start,
        "weflow_pid": weflow_pid,
        "weflow_process_start": weflow_process_start,
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
            if spawn_state is not None:
                spawn_state["started"] = True
    except Exception as exc:
        status.update(
            {
                "status": "error",
                "phase": "helper_spawn_failed",
                "manual_reopen_required": False,
                "error": f"{type(exc).__name__}: {exc}",
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        terminal_status_persisted = False
        status_removed = False
        try:
            _write_json(status_path, status)
            terminal_status_persisted = True
        except Exception:
            try:
                status_path.unlink()
                status_removed = True
            except FileNotFoundError:
                status_removed = True
            except OSError:
                pass
        if terminal_status_persisted or status_removed:
            _remove_sidebar_history_reset_shutdown_lock(lock_path)
        raise
    status["helper_pid"] = process.pid
    helper_process_start = process_start_marker(process.pid)
    if not helper_process_start:
        status["status"] = "error"
        status["phase"] = "helper_identity_unavailable"
        _write_json(status_path, status)
        _write_json(
            lock_path,
            {
                "helper_pid": process.pid,
                "owner_pid": os.getpid(),
                "owner_process_start": parent_process_start,
                "owner_token": shutdown_owner_token,
                "data_dir": str(root),
                "updated_at_epoch": time.time(),
                "status_file": str(status_path),
            },
        )
        raise RuntimeError("history reset helper process identity is unavailable")
    status["helper_process_start"] = helper_process_start
    _write_json(status_path, status)
    _write_json(
        lock_path,
        {
            "helper_pid": process.pid,
            "helper_process_start": helper_process_start,
            "owner_pid": os.getpid(),
            "owner_process_start": parent_process_start,
            "owner_token": shutdown_owner_token,
            "data_dir": str(root),
            "updated_at_epoch": time.time(),
            "status_file": str(status_path),
        },
    )
    return {
        "status": "shutdown_scheduled",
        "message": "Sidebar and WeFlow will stop and clear disposable history while preserving send_bridge evidence. Reopen the sidebar manually after it closes.",
        "helper_pid": process.pid,
        "parent_pid": parent_pid,
        "weflow_pid": weflow_pid,
        "manual_reopen_required": True,
        "shutdown_status_file": str(status_path),
        "preserved_runtime_policy": list(_HISTORY_PRESERVED_RUNTIME_PATHS),
    }


def _try_acquire_sidebar_history_reset_shutdown_lock(lock_path: Path, *, owner_token: str = "") -> bool:
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
                "owner_process_start": process_start_marker(os.getpid()),
                "owner_token": str(owner_token or uuid4().hex),
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
    helper_process_start = str((lock.get("helper_process_start") if isinstance(lock, dict) else "") or "")
    if helper_pid > 0 and _pid_exists(helper_pid):
        if not helper_process_start:
            return _deduped_sidebar_history_reset_shutdown(
                lock_path,
                status if isinstance(status, dict) else {},
                helper_pid=helper_pid,
                outcome_unknown=True,
            )
        current_process_start = process_start_marker(helper_pid)
        if not current_process_start:
            return _deduped_sidebar_history_reset_shutdown(
                lock_path,
                status if isinstance(status, dict) else {},
                helper_pid=helper_pid,
                outcome_unknown=True,
            )
        if current_process_start == helper_process_start:
            return _deduped_sidebar_history_reset_shutdown(
                lock_path,
                status if isinstance(status, dict) else {},
                helper_pid=helper_pid,
            )
    if helper_pid <= 0 and lock_age <= 20.0:
        return _deduped_sidebar_history_reset_shutdown(lock_path, status if isinstance(status, dict) else {}, helper_pid=0)
    _remove_sidebar_history_reset_shutdown_lock(lock_path)
    return None


def _deduped_sidebar_history_reset_shutdown(
    lock_path: Path,
    status: dict[str, Any],
    *,
    helper_pid: int = 0,
    outcome_unknown: bool = False,
) -> dict[str, Any]:
    return {
        "status": "shutdown_scheduled",
        "message": (
            "History reset state cannot be verified; do not retry or continue writing until it is reconciled."
            if outcome_unknown
            else "Sidebar and WeFlow shutdown/cleanup is already in progress."
        ),
        "deduplicated": True,
        "outcome_unknown": bool(outcome_unknown),
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
    return process_pid_alive(pid)


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
    return bridge_worker_lock_alive(data_dir)


def _background_send_status(
    config: Any,
    bridge: dict[str, Any],
    data_dir: str | Path | None = None,
    *,
    active_backend_probe: bool = True,
) -> str:
    if str(getattr(config, "send_driver", "")) == "bridge_outbox":
        if not bool(getattr(config, "send_enabled", False)):
            return "bridge_outbox_configured_disabled"
        send_backend = _normalize_send_backend(str(getattr(config, "send_backend", "dry_run") or "dry_run"))
        if send_backend in {"", "dry_run", "dryrun", "mock"}:
            return "bridge_outbox_dry_run_backend"
        worker = bridge.get("worker") if isinstance(bridge, dict) else {}
        if not isinstance(worker, dict) or not worker:
            worker = _bridge_worker_public_state(Path(data_dir).resolve()) if data_dir is not None else {}
        if isinstance(worker, dict) and str(worker.get("config_status") or "") == "stale":
            return "bridge_outbox_worker_stale_config"
        if isinstance(worker, dict) and str(worker.get("config_status") or "") == "unknown_legacy_lock":
            return "bridge_outbox_worker_config_unknown"
        if active_backend_probe and send_backend == "weflow_http":
            weflow_status = weflow_http_status(
                str(getattr(config, "weflow_base_url", "http://127.0.0.1:5031") or "http://127.0.0.1:5031"),
                token_env=str(getattr(config, "weflow_token_env", "WEFLOW_API_TOKEN") or "WEFLOW_API_TOKEN"),
                timeout_seconds=min(float(getattr(config, "weflow_send_timeout_seconds", 35.0) or 35.0), 3.0),
            )
            if not weflow_status.get("token_present"):
                return "bridge_outbox_weflow_token_missing"
            if not weflow_status.get("available"):
                return "bridge_outbox_weflow_http_unavailable"
            capabilities = (
                weflow_status.get("send_capabilities")
                if isinstance(weflow_status.get("send_capabilities"), dict)
                else {}
            )
            text_capability = capabilities.get("text") if isinstance(capabilities.get("text"), dict) else {}
            if text_capability.get("supports") is False:
                return "bridge_outbox_weflow_send_not_supported"
        if active_backend_probe and send_backend == "wechat_native_http":
            hook_status = wechat_native_http_status(
                str(getattr(config, "wechat_native_base_url", "http://127.0.0.1:30001") or "http://127.0.0.1:30001"),
                text_path=str(getattr(config, "wechat_native_send_text_path", "/SendTextMsg") or "/SendTextMsg"),
                image_path=str(getattr(config, "wechat_native_send_image_path", "/SendImgMsg") or "/SendImgMsg"),
                file_path=str(getattr(config, "wechat_native_send_file_path", "/send_file_msg") or "/send_file_msg"),
                status_path=str(getattr(config, "wechat_native_status_path", "/QueryDB/status") or "/QueryDB/status"),
                timeout_seconds=min(float(getattr(config, "wechat_native_timeout_seconds", 15.0) or 15.0), 3.0),
            )
            if not hook_status.get("available"):
                return "bridge_outbox_wechat_native_http_unavailable"
        # send_enabled + bridge_outbox: replies are queued, but nothing is
        # delivered unless a worker is actually consuming the outbox. Report the
        # real worker liveness instead of a config-only "ready", and flag a
        # backlog that is piling up with no live worker to drain it.
        if data_dir is not None and not _bridge_worker_alive(data_dir):
            pending = int(bridge.get("pending_count", 0) or 0) if isinstance(bridge, dict) else 0
            if pending > 0:
                return "bridge_outbox_worker_down_backlog"
            return "bridge_outbox_worker_down"
        if send_backend == "wechat_native_http":
            active_unverified = int(
                bridge.get("active_unverified_count", bridge.get("accepted_count", 0) or 0) or 0
            ) if isinstance(bridge, dict) else 0
            if active_unverified > 0:
                return "bridge_outbox_wechat_native_accepted_unverified"
        if not active_backend_probe:
            return "bridge_outbox_backend_probe_deferred"
        if send_backend == "wechat_native_http":
            return "bridge_outbox_ready"
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


def _agent_state_path(root: Path) -> Path:
    return root / "runtime" / "agent_state.json"


def _read_agent_state(root: Path) -> dict[str, Any]:
    payload = _read_json(_agent_state_path(root), {})
    return payload if isinstance(payload, dict) else {}


def _agent_public_state(root: Path) -> dict[str, Any]:
    state = _read_agent_state(root)
    last_tick = state.get("last_tick") if isinstance(state.get("last_tick"), dict) else {}
    event_files = state.get("event_files") if isinstance(state.get("event_files"), dict) else {}
    return {
        "schema": "dialog_agent_state_v1",
        "status": str(state.get("status") or "idle"),
        "storage": str(_agent_state_path(root)),
        "updated_at": str(state.get("updated_at") or ""),
        "last_tick": last_tick,
        "event_file_count": len(event_files),
        "event_files": event_files,
        "worker": _agent_worker_state(root),
    }


def _agent_event_cursor(state: dict[str, Any], event_file: Path, *, scope_ids: list[str] | None = None) -> dict[str, Any]:
    event_files = state.get("event_files") if isinstance(state.get("event_files"), dict) else {}
    item = event_files.get(_agent_event_file_key(event_file, scope_ids=scope_ids)) if isinstance(event_files, dict) else {}
    cursor = item.get("cursor") if isinstance(item, dict) and isinstance(item.get("cursor"), dict) else {}
    return cursor if isinstance(cursor, dict) else {}


def _record_agent_tick_state(
    root: Path,
    *,
    event_file: Path,
    job_id: str,
    status: str,
    result: dict[str, Any],
    snapshot: dict[str, Any],
    cursor: dict[str, Any],
    cursor_scope_ids: list[str] | None,
    cursor_restored: bool,
    error: str = "",
) -> dict[str, Any]:
    state = _read_agent_state(root)
    event_files = state.get("event_files") if isinstance(state.get("event_files"), dict) else {}
    now = utc_now_iso()
    processed_count = int(result.get("processed_count") or 0)
    proactive_reply_count = int(result.get("proactive_reply_count") or 0)
    proactive_attempt_count = int(result.get("proactive_attempt_count") or 0)
    processed_conversation_ids = _agent_processed_conversation_ids(result)
    requested_talkers = result.get("requested_talkers") if isinstance(result.get("requested_talkers"), list) else []
    requested_conversation_ids = (
        result.get("requested_conversation_ids") if isinstance(result.get("requested_conversation_ids"), list) else []
    )
    key = _agent_event_file_key(event_file, scope_ids=cursor_scope_ids)
    summary = {
        "conversation_count": int(snapshot.get("conversation_count") or 0),
        "entry_count": int(snapshot.get("entry_count") or 0),
        "pending_user_count": int(snapshot.get("pending_user_count") or 0),
        "blocked_pending_user_count": int(snapshot.get("blocked_pending_user_count") or 0),
        "opening_greeting_count": int(snapshot.get("opening_greeting_count") or 0),
        "pending_conversation_ids": snapshot.get("pending_conversation_ids")
        if isinstance(snapshot.get("pending_conversation_ids"), list)
        else [],
        "blocked_conversation_ids": snapshot.get("blocked_conversation_ids")
        if isinstance(snapshot.get("blocked_conversation_ids"), list)
        else [],
        "opening_greeting_conversation_ids": snapshot.get("opening_greeting_conversation_ids")
        if isinstance(snapshot.get("opening_greeting_conversation_ids"), list)
        else [],
        "topic_candidates": snapshot.get("topic_candidates") if isinstance(snapshot.get("topic_candidates"), list) else [],
        "topic_lifecycle_counts": snapshot.get("topic_lifecycle_counts")
        if isinstance(snapshot.get("topic_lifecycle_counts"), dict)
        else {},
        "requested_talkers": requested_talkers,
        "requested_conversation_ids": requested_conversation_ids,
        "aggregation_mode": "per_channel",
    }
    event_files[key] = {
        "event_file": str(event_file),
        "scope_ids": list(cursor_scope_ids or []),
        "requested_talkers": list(requested_talkers),
        "requested_conversation_ids": list(requested_conversation_ids),
        "cursor": cursor if isinstance(cursor, dict) else {},
        "updated_at": now,
        "processed_count": processed_count,
    }
    state = {
        "schema": "dialog_agent_state_v1",
        "status": status,
        "updated_at": now,
        "last_tick": {
            "job_id": job_id,
            "status": status,
            "event_file": str(event_file),
            "processed_count": processed_count,
            "proactive_reply_count": proactive_reply_count,
            "proactive_attempt_count": proactive_attempt_count,
            "proactive_replies": result.get("proactive_replies", []) if isinstance(result.get("proactive_replies"), list) else [],
            "processed_conversation_ids": processed_conversation_ids,
            "requested_talkers": requested_talkers,
            "requested_conversation_ids": requested_conversation_ids,
            "aggregation_mode": "per_channel",
            "cursor_restored": bool(cursor_restored),
            "cursor": cursor if isinstance(cursor, dict) else {},
            "session_summary": summary,
            "error": error,
            "finished_at": now,
        },
        "event_files": event_files,
    }
    _write_json(_agent_state_path(root), state)
    return _agent_public_state(root)


def _agent_event_file_key(event_file: Path, *, scope_ids: list[str] | None = None) -> str:
    resolved = str(event_file.resolve())
    if scope_ids is None:
        return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    seed = json.dumps(
        {"event_file": resolved, "scope_ids": _dedupe_strings(scope_ids)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _build_agent_backend_driver(
    config: Any,
    runtime: Any,
    event_file: Path,
    *,
    extra_roots: list[str] | None = None,
    allowed_conversation_ids: list[str] | None = None,
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
        allowed_conversation_ids=allowed_conversation_ids,
        voice_cache_resolver=_voice_cache_resolver(config, extra_roots=extra_roots),
    )


def _agent_session_snapshot(
    root: Path,
    *,
    runtime: Any,
    conversation_ids: list[str] | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    ids = _agent_conversation_ids(root, runtime=runtime, requested=conversation_ids)
    conversations = [_agent_conversation_snapshot(runtime, conversation_id, limit=limit) for conversation_id in ids[:50]]
    conversations = [item for item in conversations if item]
    pending_conversation_ids = [
        str(item.get("conversation_id") or "")
        for item in conversations
        if int(item.get("pending_user_count_since_last_assistant", 0) or 0) > 0
    ]
    blocked_conversation_ids = [
        str(item.get("conversation_id") or "")
        for item in conversations
        if int(item.get("blocked_pending_user_count", 0) or 0) > 0
    ]
    opening_greeting_conversation_ids = [
        str(item.get("conversation_id") or "")
        for item in conversations
        if _agent_can_open_greeting(
            item,
            recent_turns=item.get("recent_turns") if isinstance(item.get("recent_turns"), list) else [],
            last_assistant=str(item.get("last_assistant_reply") or ""),
        )
    ]
    topic_lifecycle_counts = _agent_topic_lifecycle_counts(conversations)
    return {
        "schema": "dialog_agent_session_snapshot_v1",
        "conversation_count": len(conversations),
        "entry_count": sum(int(item.get("entry_count", 0) or 0) for item in conversations),
        "pending_user_count": sum(int(item.get("pending_user_count_since_last_assistant", 0) or 0) for item in conversations),
        "blocked_pending_user_count": sum(int(item.get("blocked_pending_user_count", 0) or 0) for item in conversations),
        "opening_greeting_count": len(opening_greeting_conversation_ids),
        "pending_conversation_ids": _dedupe_strings(pending_conversation_ids),
        "blocked_conversation_ids": _dedupe_strings(blocked_conversation_ids),
        "opening_greeting_conversation_ids": _dedupe_strings(opening_greeting_conversation_ids),
        "topic_candidates": _agent_merged_topics(conversations),
        "topic_lifecycle_counts": topic_lifecycle_counts,
        "conversations": conversations,
    }


def _agent_conversation_ids(root: Path, *, runtime: Any, requested: list[str] | None) -> list[str]:
    if requested is not None:
        requested_ids = _dedupe_strings(requested)
        return requested_ids
    ids: list[str] = []
    try:
        ids.extend(channel.conversation_id for channel in runtime.channel_store.list_channels())
    except Exception:
        pass
    ids.extend(_agent_conversation_ids_from_ledgers(root))
    return _dedupe_strings(ids)


def _agent_conversation_ids_from_ledgers(root: Path) -> list[str]:
    try:
        return ConversationLedgerStore(root).list_conversation_ids()
    except Exception:
        return []


def _agent_conversation_snapshot(runtime: Any, conversation_id: str, *, limit: int) -> dict[str, Any]:
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        return {}
    try:
        entries = [_dataclass_payload(entry) for entry in runtime.ledger_store.read_entries(conversation_id)]
    except Exception:
        entries = []
    try:
        session_state = runtime.session_store.state_for_conversation(conversation_id)
    except Exception:
        session_state = {}
    session_id = str(session_state.get("current_session_id") or "").strip()
    if not session_id:
        try:
            session_id = runtime.session_store.current_session_id(conversation_id)
        except Exception:
            session_id = "session_default"
        session_state = {"current_session_id": session_id}
    session_entries = [
        entry
        for entry in entries
        if str(entry.get("session_id") or "session_default") == session_id
    ]
    markdown_path = runtime.ledger_store.conversation_markdown_path(conversation_id)
    recent = session_entries[-limit:]
    user_texts = [_agent_entry_text(entry) for entry in _agent_user_text_entries(session_entries)]
    assistant_texts = [
        _agent_entry_text(entry)
        for entry in session_entries
        if str(entry.get("role") or "") == "assistant" and _agent_entry_text(entry)
    ]
    raw_pending_entries = _agent_pending_user_entries(session_entries)
    pending_entries = _agent_actionable_pending_user_entries(session_entries, raw_pending_entries)
    blocked_pending_count = len(raw_pending_entries) if raw_pending_entries and not pending_entries else 0
    participants = _agent_participant_summary(session_entries)
    topic_candidates = _agent_topic_candidates(user_texts[-10:])
    last_entry = session_entries[-1] if session_entries else {}
    topic_lifecycle = _agent_topic_lifecycle(
        conversation_id,
        session_entries,
        pending_entries=pending_entries,
        blocked_pending_count=blocked_pending_count,
        topic_candidates=topic_candidates,
    )
    return {
        "conversation_id": conversation_id,
        "conversation_type": str(last_entry.get("conversation_type") or ""),
        "chat_title": str(last_entry.get("chat_title") or ""),
        "session_id": session_id,
        "previous_session_id": str(session_state.get("previous_session_id") or ""),
        "session_started_at": str(session_state.get("session_started_at") or ""),
        "session_reset_count": _bounded_int(session_state.get("reset_count"), 0, 0, 1_000_000),
        "session_reset_reason": str(session_state.get("previous_reset_reason") or ""),
        "session_reset_message_id": str(session_state.get("previous_reset_message_id") or ""),
        "ledger_markdown": str(markdown_path),
        "ledger_messages": str(markdown_path.with_name("messages.jsonl")),
        "entry_count": len(session_entries),
        "total_entry_count": len(entries),
        "last_message_at": str(last_entry.get("received_at") or last_entry.get("updated_at") or ""),
        "last_user_message": _agent_compact_text(user_texts[-1] if user_texts else "", 240),
        "last_assistant_reply": _agent_compact_text(assistant_texts[-1] if assistant_texts else "", 240),
        "pending_user_count_since_last_assistant": len(pending_entries),
        "blocked_pending_user_count": blocked_pending_count,
        "participant_count": len(participants),
        "participants": participants,
        "pending_user_messages": [_agent_pending_message_payload(entry) for entry in pending_entries],
        "message_aggregation": _agent_message_aggregation(conversation_id, session_entries, pending_entries, participants, topic_candidates),
        "topic_candidates": topic_candidates,
        "topic_lifecycle": topic_lifecycle,
        "dispatch_preview": _agent_dispatch_preview(conversation_id, session_entries, pending_entries, topic_candidates),
        "recent_turns": [
            {
                "role": str(entry.get("role") or "user"),
                "message_id": str(entry.get("message_id") or ""),
                "sequence": _agent_entry_sequence(entry),
                "sender_name": str(entry.get("sender_name") or ""),
                "received_at": str(entry.get("received_at") or ""),
                "text": _agent_compact_text(_agent_entry_text(entry), 240),
                "attachment_count": len(entry.get("attachments") if isinstance(entry.get("attachments"), list) else []),
                "send_status": _agent_entry_send_status(entry),
                "send_reason": _agent_entry_send_reason(entry),
            }
            for entry in recent
        ],
    }


def _agent_generate_proactive_replies(
    root: Path,
    *,
    runtime: Any,
    snapshot: dict[str, Any],
    limit: int,
    enabled: bool = True,
) -> list[dict[str, Any]]:
    if not enabled or limit <= 0:
        return []
    conversations = snapshot.get("conversations") if isinstance(snapshot.get("conversations"), list) else []
    candidates = _agent_proactive_candidates(conversations, limit=limit)
    if not candidates:
        return []
    max_workers = _agent_proactive_parallelism(runtime, len(candidates))
    if max_workers <= 1 or len(candidates) == 1:
        return [_agent_generate_one_proactive_reply(root, runtime=runtime, candidate=item) for item in candidates]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agent-proactive") as pool:
        future_to_candidate = {
            pool.submit(_agent_generate_one_proactive_reply, root, runtime=runtime, candidate=item): item
            for item in candidates
        }
        for future in as_completed(future_to_candidate):
            try:
                results.append(future.result())
            except Exception as exc:
                candidate = future_to_candidate[future]
                results.append(
                    {
                        "status": "error",
                        "conversation_id": str(candidate.get("conversation_id") or ""),
                        "kind": str(candidate.get("kind") or "proactive"),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
    return sorted(results, key=lambda item: str(item.get("conversation_id") or ""))


def _agent_proactive_candidates(conversations: list[Any], *, limit: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for conversation in conversations:
        if not isinstance(conversation, dict):
            continue
        if str(conversation.get("conversation_type") or "private") != "private":
            continue
        conversation_id = str(conversation.get("conversation_id") or "").strip()
        if not conversation_id:
            continue
        pending = conversation.get("pending_user_messages") if isinstance(conversation.get("pending_user_messages"), list) else []
        pending_count = int(conversation.get("pending_user_count_since_last_assistant") or len(pending) or 0)
        last_assistant = str(conversation.get("last_assistant_reply") or "").strip()
        recent_turns = conversation.get("recent_turns") if isinstance(conversation.get("recent_turns"), list) else []
        kind = ""
        if pending_count > 0 and pending:
            kind = "pending_private_reply"
        elif _agent_can_open_greeting(conversation, recent_turns=recent_turns, last_assistant=last_assistant):
            kind = "opening_greeting"
        if not kind:
            continue
        candidates.append(
            {
                "kind": kind,
                "conversation": conversation,
                "conversation_id": conversation_id,
                "pending_count": pending_count,
                "source_watermark": _agent_proactive_source_watermark(conversation, kind=kind),
                "last_message_at": str(conversation.get("last_message_at") or ""),
            }
        )
    candidates.sort(
        key=lambda item: (
            0 if item.get("kind") == "pending_private_reply" else 1,
            -int(item.get("pending_count") or 0),
            str(item.get("last_message_at") or ""),
        )
    )
    return candidates[: max(0, limit)]


def _agent_can_open_greeting(conversation: dict[str, Any], *, recent_turns: list[Any], last_assistant: str) -> bool:
    if last_assistant:
        return False
    if int(conversation.get("entry_count") or 0) <= 0:
        return False
    if not recent_turns:
        return False
    last_turn = recent_turns[-1] if isinstance(recent_turns[-1], dict) else {}
    last_role = str(last_turn.get("role") or "")
    return last_role == "self"


def _agent_generate_one_proactive_reply(root: Path, *, runtime: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    conversation = candidate.get("conversation") if isinstance(candidate.get("conversation"), dict) else {}
    kind = str(candidate.get("kind") or "pending_private_reply")
    conversation_id = str(conversation.get("conversation_id") or "").strip()
    if not conversation_id:
        return {"status": "skipped", "reason": "conversation_id_empty", "kind": kind}
    message = _agent_proactive_message(conversation, kind=kind)
    speak = SpeakDecision(
        conversation_id=conversation_id,
        decision="speak",
        reason=kind,
        topic=_agent_proactive_topic(conversation),
        confidence=1.0,
        style_context="自然、简短、像真实朋友一样接话",
    )
    reply = runtime.conversation.generate_reply(message, speak)
    if reply is None or not str(reply.text or "").strip():
        return {"status": "skipped", "reason": "empty_reply", "conversation_id": conversation_id, "kind": kind}
    if _agent_reply_repeats_recent_assistant(str(reply.text or ""), conversation):
        return {
            "status": "skipped",
            "reason": "repeat_guard_recent_assistant",
            "conversation_id": conversation_id,
            "kind": kind,
        }
    stale_reason = _agent_stale_proactive_reply_reason(
        runtime,
        conversation_id,
        session_id=str(conversation.get("session_id") or "session_default"),
        source_watermark=_bounded_int(candidate.get("source_watermark"), 0, 0, 1_000_000_000),
    )
    if stale_reason:
        return {
            "status": "skipped",
            "reason": stale_reason,
            "conversation_id": conversation_id,
            "kind": kind,
            "source_watermark": int(candidate.get("source_watermark") or 0),
        }
    entry = runtime.ledger_store.append_reply_if_latest(
        reply,
        expected_latest_sequence=_bounded_int(candidate.get("source_watermark"), 0, 0, 1_000_000_000),
        chat_title=str(conversation.get("chat_title") or ""),
        conversation_type=str(conversation.get("conversation_type") or "private"),
        session_id=str(conversation.get("session_id") or "session_default"),
    )
    if entry is None:
        return {
            "status": "skipped",
            "reason": "stale_linear_context:ledger_changed_before_reply_commit",
            "conversation_id": conversation_id,
            "kind": kind,
            "source_watermark": int(candidate.get("source_watermark") or 0),
        }
    send = runtime.reply_gate.handle(reply)
    ledger_updated = runtime.ledger_store.update_reply_send_result(conversation_id, entry.entry_id, send)
    try:
        runtime.event_logger.log(
            "agent.proactive_reply",
            {
                "kind": kind,
                "reply": asdict(reply),
                "send": asdict(send),
                "pending_count": int(candidate.get("pending_count") or 0),
            },
            message_id=reply.message_id,
        )
    except Exception:
        pass
    task_projection_error = _record_agent_proactive_tasks(
        root,
        conversation=conversation,
        reply=reply,
        send=send,
        kind=kind,
    )
    if not ledger_updated or task_projection_error:
        projection_error = task_projection_error or "ledger_projection_not_updated"
        runtime.reply_gate.fail_staged(
            send,
            reason=f"staged_projection_failed:{projection_error}",
            expected_projections=["ledger", "task"],
        )
    else:
        try:
            runtime.reply_gate.activate_staged(
                send,
                expected_projections=["ledger", "task"],
            )
        except Exception as exc:
            runtime.reply_gate.fail_staged(
                send,
                reason=f"staged_activation_failed:{type(exc).__name__}:{exc}",
                expected_projections=["ledger", "task"],
            )
    return {
        "status": "ok",
        "kind": kind,
        "conversation_id": conversation_id,
        "message_id": reply.message_id,
        "send_status": str(getattr(send, "status", "")),
        "send_reason": str(getattr(send, "reason", "")),
        "pending_count": int(candidate.get("pending_count") or 0),
    }


def _agent_proactive_message(conversation: dict[str, Any], *, kind: str) -> NormalizedMessage:
    conversation_id = str(conversation.get("conversation_id") or "")
    pending = conversation.get("pending_user_messages") if isinstance(conversation.get("pending_user_messages"), list) else []
    recent_turns = conversation.get("recent_turns") if isinstance(conversation.get("recent_turns"), list) else []
    source = pending[-5:] if pending else recent_turns[-5:]
    prompt = _agent_proactive_prompt(conversation, source=source, kind=kind)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "conversation_id": conversation_id,
                "kind": kind,
                "source_watermark": _agent_proactive_source_watermark(conversation, kind=kind),
                "source": [
                    {
                        "message_id": str(item.get("message_id") or ""),
                        "sequence": int(item.get("sequence") or 0),
                        "received_at": str(item.get("received_at") or ""),
                        "text": str(item.get("text") or ""),
                    }
                    for item in source
                    if isinstance(item, dict)
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    sender = _agent_proactive_sender(source, conversation, kind=kind)
    return NormalizedMessage(
        message_id=f"agent-proactive-{fingerprint}",
        conversation_id=conversation_id,
        conversation_type="private",
        chat_title=str(conversation.get("chat_title") or sender or conversation_id),
        sender_name=sender,
        sender_wechat_id="",
        text=prompt,
        is_self=False,
        received_at=utc_now_iso(),
        metadata={
            "source": "agent_proactive_pending_ledger",
            "proactive_kind": kind,
            "pending_count": int(conversation.get("pending_user_count_since_last_assistant") or len(pending) or 0),
            "source_watermark": _agent_proactive_source_watermark(conversation, kind=kind),
            "original_text": prompt,
        },
    )


def _agent_proactive_source_watermark(conversation: dict[str, Any], *, kind: str) -> int:
    pending = conversation.get("pending_user_messages") if isinstance(conversation.get("pending_user_messages"), list) else []
    recent_turns = conversation.get("recent_turns") if isinstance(conversation.get("recent_turns"), list) else []
    source = pending[-5:] if pending and kind != "opening_greeting" else recent_turns[-5:]
    watermark = 0
    for item in source:
        if not isinstance(item, dict):
            continue
        try:
            watermark = max(watermark, int(item.get("sequence") or 0))
        except (TypeError, ValueError):
            continue
    return watermark


def _agent_stale_proactive_reply_reason(
    runtime: Any,
    conversation_id: str,
    *,
    session_id: str,
    source_watermark: int,
) -> str:
    if source_watermark <= 0:
        return ""
    try:
        entries = [_dataclass_payload(entry) for entry in runtime.ledger_store.read_entries(conversation_id)]
    except Exception:
        return ""
    session_id = str(session_id or "session_default")
    latest_sequence = 0
    for entry in entries:
        if str(entry.get("session_id") or "session_default") != session_id:
            continue
        latest_sequence = max(latest_sequence, _agent_entry_sequence(entry))
    if latest_sequence > source_watermark:
        return f"stale_linear_context:newer_entry_sequence={latest_sequence}:source_watermark={source_watermark}"
    return ""


def _agent_proactive_prompt(conversation: dict[str, Any], *, source: list[Any], kind: str) -> str:
    chat_title = str(conversation.get("chat_title") or "对方").strip() or "对方"
    lines: list[str] = []
    lines.append("角色说明：user=对方或群友；self=当前微信账号主人手动发言；assistant=你自己此前的回复。")
    lines.append("请只输出将要发到微信里的单条自然消息，不要写分析、标题或计划。")
    if kind == "opening_greeting":
        lines.append(f"请主动给 {chat_title} 发一条自然的微信开场白。")
        lines.append("语气轻松、克制，不要解释系统，也不要说自己是机器人；可以简单打招呼并自然打开话题。")
    else:
        lines.append(f"请接上与 {chat_title} 的当前私聊。")
        lines.append("下面是当前最新待接状态，请合并理解后只发一条自然回复；不要逐条补答旧历史，self 只作为主人上下文。")
    source_line_count = 0
    for item in source:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip() or "user"
        sender = str(item.get("sender_name") or "").strip() or chat_title
        text = str(item.get("text") or "").strip()
        attachment_count = int(item.get("attachment_count") or 0)
        if not text and attachment_count:
            text = f"[{attachment_count} 个附件]"
        if text:
            lines.append(f"{role} {sender}: {text}")
            source_line_count += 1
    if source_line_count <= 0:
        aggregation = conversation.get("message_aggregation") if isinstance(conversation.get("message_aggregation"), dict) else {}
        summary = str(aggregation.get("summary") or "")
        if summary:
            lines.append(summary)
    return "\n".join(lines).strip()


def _agent_proactive_sender(source: list[Any], conversation: dict[str, Any], *, kind: str) -> str:
    if kind == "opening_greeting":
        return str(conversation.get("chat_title") or "对方")
    for item in reversed(source):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "") == "self":
            continue
        sender = str(item.get("sender_name") or "").strip()
        if sender:
            return sender
    return str(conversation.get("chat_title") or "对方")


def _agent_proactive_topic(conversation: dict[str, Any]) -> str:
    topics = conversation.get("topic_candidates") if isinstance(conversation.get("topic_candidates"), list) else []
    if topics and isinstance(topics[0], dict):
        return str(topics[0].get("title") or "private")
    return "private"


def _agent_reply_repeats_recent_assistant(text: str, conversation: dict[str, Any]) -> bool:
    current = _agent_repeat_fingerprint(text)
    if len(current) < 8:
        return False
    candidates: list[str] = []
    last_assistant = str(conversation.get("last_assistant_reply") or "").strip()
    if last_assistant:
        candidates.append(last_assistant)
    recent_turns = conversation.get("recent_turns") if isinstance(conversation.get("recent_turns"), list) else []
    for item in recent_turns[-8:]:
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "") != "assistant":
            continue
        candidate = str(item.get("text") or "").strip()
        if candidate:
            candidates.append(candidate)
    for candidate in _dedupe_strings(candidates):
        previous = _agent_repeat_fingerprint(candidate)
        if len(previous) < 8:
            continue
        if current == previous:
            return True
        if _agent_text_similarity(current, previous) >= 0.92:
            return True
    return False


def _agent_repeat_fingerprint(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    return re.sub(r"\s+", "", normalized)


def _agent_text_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_grams = _agent_char_grams(left)
    right_grams = _agent_char_grams(right)
    if not left_grams or not right_grams:
        return 0.0
    intersection = len(left_grams.intersection(right_grams))
    union = len(left_grams.union(right_grams))
    return intersection / union if union else 0.0


def _agent_char_grams(text: str) -> set[str]:
    if len(text) <= 2:
        return {text}
    return {text[index : index + 2] for index in range(len(text) - 1)}


def _agent_proactive_parallelism(runtime: Any, candidate_count: int) -> int:
    if candidate_count <= 0:
        return 0
    default = max(1, min(3, candidate_count))
    scheduler = getattr(runtime, "resource_scheduler", None)
    schedule = getattr(scheduler, "conversation_parallelism", None)
    if callable(schedule):
        try:
            planned = schedule("interactive")
            return max(1, min(candidate_count, int(getattr(planned, "max_parallel_conversations", default) or default)))
        except Exception:
            return default
    return default


def _record_agent_proactive_tasks(
    root: Path,
    *,
    conversation: dict[str, Any],
    reply: ReplyCandidate,
    send: Any,
    kind: str,
) -> str:
    try:
        store = TaskStatusStore(root)
        fragment = _agent_safe_id(reply.message_id)
        conversation_id = str(conversation.get("conversation_id") or reply.conversation_id)
        session_id = str(conversation.get("session_id") or "session_default")
        store.create(
            {
                "task_id": f"agent-proactive-reply-{fragment}",
                "title": "主动接话回复",
                "kind": "reply",
                "conversation_id": conversation_id,
                "session_id": session_id,
                "scope": f"conversation:{conversation_id}",
                "topic_id": f"agent-proactive-{fragment}",
                "topic_title": "主动接话",
                "resource_class": "llm_interactive",
                "priority": 80,
                "estimated_cost": 2,
                "metadata": {"message_id": reply.message_id, "proactive_kind": kind},
            }
        )
        store.transition(
            f"agent-proactive-reply-{fragment}",
            "complete",
            {"progress": 100, "phase": "主动回复已生成", "detail": reply.summary or reply.text[:160], "actual_cost": 1},
        )
        status = str(getattr(send, "status", "") or "")
        reason = str(getattr(send, "reason", "") or "")
        bridge_ids = send_result_bridge_ids(send)
        bridge_id = bridge_ids[0] if bridge_ids else _agent_bridge_id_from_reason(reason)
        if bridge_id and bridge_id not in bridge_ids:
            bridge_ids.append(bridge_id)
        send_task_id = f"agent-proactive-send-{fragment}"
        store.create(
            {
                "task_id": send_task_id,
                "title": "主动接话发送",
                "kind": "send",
                "conversation_id": conversation_id,
                "session_id": session_id,
                "scope": f"conversation:{conversation_id}",
                "topic_id": f"agent-proactive-{fragment}",
                "topic_title": "主动接话",
                "resource_class": "send_bridge",
                "priority": 85,
                "estimated_cost": 1,
                "external_id": bridge_id,
                "metadata": {
                    "message_id": reply.message_id,
                    "send_status": status,
                    "send_reason": reason,
                    "bridge_id": bridge_id,
                    "bridge_ids": bridge_ids,
                    "non_bridge_part_statuses": send_result_non_bridge_part_statuses(send),
                },
            }
        )
        if status == "queued_to_bridge":
            store.update(send_task_id, {"status": "queued", "progress": 70, "phase": "等待非前台桥发送", "detail": reason})
        elif status in {"sent", "accepted", "skipped"}:
            store.transition(send_task_id, "complete", {"progress": 100, "phase": "发送链路已完成", "detail": reason})
        elif status == "queued_for_confirm":
            store.transition(send_task_id, "wait", {"progress": 45, "phase": "等待人工审核", "detail": reason})
        else:
            store.transition(send_task_id, "fail", {"progress": 100, "phase": "发送失败", "detail": reason, "last_error": reason})
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""


def _agent_safe_id(value: str) -> str:
    cleaned = "".join(ch for ch in str(value or "") if ch.isalnum() or ch in {"-", "_", "."})
    return cleaned[:64] or "unknown"


def _agent_bridge_id_from_reason(reason: str) -> str:
    marker = "bridge:"
    text = str(reason or "")
    index = text.rfind(marker)
    if index < 0:
        return ""
    candidate = text[index:].split()[0].strip("，,.;；")
    return candidate if candidate.startswith(marker) else ""


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
        if not _agent_visible_text_block(block):
            continue
        text = str(block.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _agent_visible_text_block(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    text = str(block.get("text") or "").strip()
    if not text:
        return False
    kind = str(block.get("kind") or "")
    if kind.startswith("control:") or kind.startswith("attachment:"):
        return False
    metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
    return metadata.get("visible_in_context") is not False


def _agent_entry_is_control_event(entry: dict[str, Any]) -> bool:
    blocks = entry.get("text_blocks") if isinstance(entry.get("text_blocks"), list) else []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("kind") or "")
        metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
        if kind.startswith("control:") or str(metadata.get("control_event") or ""):
            return True
    return False


def _agent_entry_has_actionable_user_content(entry: dict[str, Any]) -> bool:
    if _agent_entry_is_control_event(entry) or _agent_entry_is_historical_context(entry):
        return False
    if _agent_entry_text(entry):
        return True
    attachments = entry.get("attachments") if isinstance(entry.get("attachments"), list) else []
    return any(isinstance(item, dict) for item in attachments)


def _agent_entry_is_historical_context(entry: dict[str, Any]) -> bool:
    blocks = entry.get("text_blocks") if isinstance(entry.get("text_blocks"), list) else []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
        if not metadata.get("context_only"):
            continue
        capture_phase = str(metadata.get("capture_phase") or "").strip().lower()
        source = str(metadata.get("source") or "").strip()
        if capture_phase in {"history", "history_backfill", "backfill"}:
            return True
        if metadata.get("history_index") is not None:
            return True
        if source in {"windows_snapshot", "ocr_file_card", "poll_ocr_window", "ocr_snapshot"}:
            return True
    return False


def _agent_pending_user_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for entry in reversed(entries):
        role = str(entry.get("role") or "user")
        if role == "self":
            break
        if role == "assistant":
            if _agent_assistant_entry_settles_pending(entry):
                break
            continue
        if role == "user" and _agent_entry_has_actionable_user_content(entry):
            pending.append(entry)
    return list(reversed(pending))


def _agent_actionable_pending_user_entries(
    entries: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    pending = list(pending_entries if pending_entries is not None else _agent_pending_user_entries(entries))
    if not pending:
        return []
    if _agent_pending_blocked_by_unseen_assistant(entries, pending):
        return []
    return pending


def _agent_pending_blocked_by_unseen_assistant(
    entries: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
) -> bool:
    latest_pending_sequence = max((_agent_entry_sequence(item) for item in pending_entries), default=0)
    if latest_pending_sequence <= 0:
        return False
    for entry in entries:
        if _agent_entry_sequence(entry) <= latest_pending_sequence:
            continue
        if str(entry.get("role") or "") != "assistant":
            continue
        if not _agent_assistant_entry_settles_pending(entry):
            return True
    return False


def _agent_assistant_entry_settles_pending(entry: dict[str, Any]) -> bool:
    status = _agent_entry_send_status(entry)
    # A failed/candidate reply was generated but never became visible to the
    # other party. It must not close the pending user turn, but it also blocks
    # duplicate generation until a newer user message changes the conversation.
    if status in {"failed", "candidate"}:
        return False
    return True


def _agent_entry_sequence(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("sequence") or 0)
    except (TypeError, ValueError):
        return 0


def _agent_entry_send_status(entry: dict[str, Any]) -> str:
    send = entry.get("send") if isinstance(entry.get("send"), dict) else {}
    return str(send.get("status") or "")


def _agent_entry_send_reason(entry: dict[str, Any]) -> str:
    send = entry.get("send") if isinstance(entry.get("send"), dict) else {}
    return str(send.get("reason") or "")


def _agent_topic_lifecycle(
    conversation_id: str,
    entries: list[dict[str, Any]],
    *,
    pending_entries: list[dict[str, Any]],
    blocked_pending_count: int,
    topic_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    lead_topic = topic_candidates[0] if topic_candidates else {}
    topic_title = str(lead_topic.get("title") or "对话接续")
    topic_id = str(lead_topic.get("topic_id") or _agent_topic_id(f"{conversation_id}:{topic_title}"))
    last_user = _agent_last_entry_by_role(entries, "user")
    last_assistant = _agent_last_entry_by_role(entries, "assistant")
    last_user_sequence = _agent_entry_sequence(last_user)
    last_assistant_sequence = _agent_entry_sequence(last_assistant)
    latest_pending_sequence = max((_agent_entry_sequence(item) for item in pending_entries), default=0)
    assistant_status = _agent_entry_send_status(last_assistant)
    assistant_reason = _agent_entry_send_reason(last_assistant)

    if blocked_pending_count > 0:
        status = "responded"
        reason = "assistant_reply_generated_but_not_visible"
    elif pending_entries:
        reopened = last_assistant_sequence > 0 and latest_pending_sequence > last_assistant_sequence
        status = "reopened" if reopened else "open"
        reason = "new_user_message_after_assistant" if reopened else "pending_user_messages"
    elif last_assistant:
        if assistant_status in {"sent", "accepted"}:
            status = "sent"
            reason = f"assistant_{assistant_status}"
        elif assistant_status in {"queued_for_confirm", "queued_to_bridge", "approved", "pending", "candidate"}:
            status = "responded"
            reason = f"assistant_waiting_delivery:{assistant_status}"
        else:
            status = "closed"
            reason = "assistant_settled_no_pending"
    else:
        status = "closed"
        reason = "no_pending_user_messages"

    return {
        "schema": "dialog_agent_topic_lifecycle_v1",
        "topic_id": topic_id,
        "topic_title": topic_title,
        "status": status,
        "reason": reason,
        "pending_user_count": len(pending_entries),
        "blocked_pending_user_count": int(blocked_pending_count or 0),
        "last_user_message_id": str(last_user.get("message_id") or ""),
        "last_user_sequence": last_user_sequence,
        "last_assistant_message_id": str(last_assistant.get("message_id") or ""),
        "last_assistant_sequence": last_assistant_sequence,
        "last_assistant_send_status": assistant_status,
        "last_assistant_send_reason": assistant_reason,
    }


def _agent_last_entry_by_role(entries: list[dict[str, Any]], role: str) -> dict[str, Any]:
    for entry in reversed(entries):
        if str(entry.get("role") or "user") != role:
            continue
        if role == "user" and not _agent_entry_has_actionable_user_content(entry):
            continue
        return entry
    return {}


def _agent_user_entries_with_actionable_content(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in entries
        if str(entry.get("role") or "user") == "user" and _agent_entry_has_actionable_user_content(entry)
    ]


def _agent_user_text_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for entry in _agent_user_entries_with_actionable_content(entries):
        if _agent_entry_text(entry):
            result.append(entry)
    return result


def _agent_topic_lifecycle_counts(conversations: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {"open": 0, "responded": 0, "sent": 0, "closed": 0, "reopened": 0}
    for conversation in conversations:
        lifecycle = conversation.get("topic_lifecycle") if isinstance(conversation.get("topic_lifecycle"), dict) else {}
        status = str(lifecycle.get("status") or "closed")
        counts[status] = int(counts.get(status, 0)) + 1
    return counts


def _agent_participant_summary(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    participants: dict[str, dict[str, Any]] = {}
    for entry in _agent_user_entries_with_actionable_content(entries):
        sender_name = str(entry.get("sender_name") or "").strip() or "unknown"
        sender_wechat_id = str(entry.get("sender_wechat_id") or "").strip()
        key = sender_wechat_id or sender_name
        item = participants.setdefault(
            key,
            {
                "sender_name": sender_name,
                "sender_wechat_id": sender_wechat_id,
                "message_count": 0,
                "last_message_at": "",
                "recent_texts": [],
            },
        )
        item["message_count"] = int(item["message_count"]) + 1
        item["last_message_at"] = max(str(item.get("last_message_at") or ""), str(entry.get("received_at") or entry.get("updated_at") or ""))
        text = _agent_compact_text(_agent_entry_text(entry), 160)
        if text:
            item["recent_texts"] = [*item["recent_texts"], text][-3:]
    return sorted(
        participants.values(),
        key=lambda item: (str(item.get("last_message_at") or ""), int(item.get("message_count") or 0)),
        reverse=True,
    )


def _agent_pending_message_payload(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_id": str(entry.get("message_id") or ""),
        "sequence": _agent_entry_sequence(entry),
        "sender_name": str(entry.get("sender_name") or ""),
        "sender_wechat_id": str(entry.get("sender_wechat_id") or ""),
        "received_at": str(entry.get("received_at") or ""),
        "text": _agent_compact_text(_agent_entry_text(entry), 240),
        "attachment_count": len(entry.get("attachments") if isinstance(entry.get("attachments"), list) else []),
    }


def _agent_message_aggregation(
    conversation_id: str,
    entries: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
    participants: list[dict[str, Any]],
    topic_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    recent_user_entries = _agent_user_entries_with_actionable_content(entries)[-12:]
    pending_senders = _dedupe_strings([str(entry.get("sender_name") or "") for entry in pending_entries])
    recent_senders = _dedupe_strings([str(entry.get("sender_name") or "") for entry in recent_user_entries])
    lead_topic = topic_candidates[0] if topic_candidates else {}
    return {
        "schema": "conversation_message_aggregation_v1",
        "conversation_id": conversation_id,
        "status": "needs_agent_reply" if pending_entries else "settled",
        "recent_message_count": len(recent_user_entries),
        "pending_message_count": len(pending_entries),
        "participant_count": len(participants),
        "recent_senders": recent_senders,
        "pending_senders": pending_senders,
        "lead_topic": {
            "topic_id": str(lead_topic.get("topic_id") or ""),
            "title": str(lead_topic.get("title") or ""),
            "score": int(lead_topic.get("score") or 0),
        },
        "summary": _agent_aggregate_summary(recent_user_entries, pending_entries, lead_topic),
    }


def _agent_aggregate_summary(
    recent_user_entries: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
    lead_topic: dict[str, Any],
) -> str:
    source = pending_entries or recent_user_entries[-3:]
    if not source:
        return ""
    senders = _dedupe_strings([str(entry.get("sender_name") or "") for entry in source])
    topic = str(lead_topic.get("title") or "").strip()
    latest = _agent_compact_text(_agent_entry_text(source[-1]), 120)
    sender_text = "、".join(senders) if senders else "用户"
    if topic:
        return f"{sender_text}围绕“{topic}”继续发言；最新一句：{latest}"
    return f"{sender_text}继续发言；最新一句：{latest}"


def _agent_dispatch_preview(
    conversation_id: str,
    entries: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
    topic_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    topic = topic_candidates[0] if topic_candidates else {}
    topic_id = str(topic.get("topic_id") or _agent_topic_id(conversation_id))
    topic_title = str(topic.get("title") or "对话接续")
    if pending_entries:
        tasks.append(
            {
                "kind": "reply",
                "title": "生成接话回复",
                "conversation_id": conversation_id,
                "resource_class": "llm_interactive",
                "concurrency_key": f"conversation:{conversation_id}",
                "topic_id": topic_id,
                "topic_title": topic_title,
                "reason": "pending_user_messages",
                "estimated_cost": max(1, min(4, len(pending_entries))),
            }
        )
    if len(entries) >= 3:
        tasks.append(
            {
                "kind": "memory",
                "title": "维护会话记忆",
                "conversation_id": conversation_id,
                "resource_class": "llm_background",
                "concurrency_key": f"memory:{conversation_id}",
                "topic_id": topic_id,
                "topic_title": topic_title,
                "reason": "session_has_context",
                "estimated_cost": 1,
            }
        )
    incoming_attachment_count = _agent_incoming_attachment_count(entries)
    if incoming_attachment_count:
        tasks.append(
            {
                "kind": "file",
                "title": "检查会话文件上下文",
                "conversation_id": conversation_id,
                "resource_class": "file_io",
                "concurrency_key": f"file:{conversation_id}",
                "topic_id": topic_id,
                "topic_title": topic_title,
                "reason": "conversation_has_incoming_attachments",
                "estimated_cost": min(5, incoming_attachment_count),
            }
        )
    return tasks


def _agent_incoming_attachment_count(entries: list[dict[str, Any]]) -> int:
    count = 0
    for entry in entries:
        role = str(entry.get("role") or "user")
        if role == "assistant" or entry.get("is_self") or role == "self":
            continue
        attachments = entry.get("attachments") if isinstance(entry.get("attachments"), list) else []
        count += len(attachments)
    return count


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
    chat_provider = config.providers["chat"]
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
    provider = config.providers["chat"]
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
    provider = config.providers["chat"]
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
    provider_cfg = config.providers["chat"]
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
        try:
            return {"status": "error", "reachable": True, "http_status": exc.code, "error": f"http_{exc.code}", "url": url}
        finally:
            exc.close()
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
    the API key is attached; a bad base_url can never leak the key to file://,
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
    chat_provider = config.providers["chat"]
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
    channel_database = root / "conversation_channels.sqlite"
    channel_items = _channel_store(root).list_channels() if channel_root.exists() or channel_database.exists() else []
    task_state = build_sidebar_task_manager(root)
    task_groups = _tasks_by_conversation(task_state.get("tasks", []))
    ledger_store = (
        ConversationLedgerStore(root)
        if channel_items and ((root / "conversation_ledgers").exists() or (root / "conversation_ledger.sqlite").exists())
        else None
    )
    state_path = root / "channel_state.sqlite"
    state_store = ChannelStateStore(root) if channel_items or state_path.exists() else None
    if not channel_items:
        persisted_states = state_store.list_states() if state_store is not None else []
        return {
            "status": "ok",
            "policy": CHANNEL_POLICY,
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
    visible_policy = _visible_channel_policy(config)
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
            "is_friend": channel.is_friend,
            "contact_authorization": channel.contact_authorization,
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
        "policy": CHANNEL_POLICY,
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
    root = Path(data_dir).resolve()
    state = bridge_state(root, limit=limit)
    channels_state = channels_state if isinstance(channels_state, dict) else _channel_state(root)
    channels = channels_state.get("items") if isinstance(channels_state.get("items"), list) else []
    bridge_channels = [_bridge_channel_payload(item) for item in channels if isinstance(item, dict)]
    state["channels"] = bridge_channels
    state["channel_count"] = len(bridge_channels)
    state["item_channels"] = _bridge_item_channels(state.get("items", []), bridge_channels)
    state["worker"] = _bridge_worker_public_state(root)
    state["contract"] = {
        **(state.get("contract") if isinstance(state.get("contract"), dict) else {}),
        "channel_sync": "visible service channels are projected into send_bridge.channels; outbox records carry receiver wxid/roomid from the channel registry when available; item_channels groups bridge records by conversation",
    }
    return state


def _bridge_item_channels(items: Any, bridge_channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = items if isinstance(items, list) else []
    channel_map = {
        str(channel.get("conversation_id") or "").strip(): channel
        for channel in bridge_channels
        if str(channel.get("conversation_id") or "").strip()
    }
    groups: dict[str, dict[str, Any]] = {}
    for item in records:
        if not isinstance(item, dict):
            continue
        conversation_id = str(item.get("conversation_id") or "").strip() or "unknown"
        group = groups.get(conversation_id)
        if group is None:
            channel = channel_map.get(conversation_id, {})
            group = {
                "conversation_id": conversation_id,
                "display_name": str(channel.get("display_name") or "").strip() or conversation_id,
                "conversation_type": str(channel.get("conversation_type") or ""),
                "receiver": str(channel.get("receiver") or item.get("receiver") or "").strip(),
                "bridge_ready": bool(channel.get("bridge_ready") or item.get("receiver")),
                "count": 0,
                "latest_at": "",
                "status_counts": {status: 0 for status in BRIDGE_ITEM_STATUSES},
                "items": [],
            }
            groups[conversation_id] = group
        group["items"].append(item)
        group["count"] += 1
        status = str(item.get("status") or "queued").strip() or "queued"
        group["status_counts"][status] = int(group["status_counts"].get(status, 0) or 0) + 1
        latest_at = str(item.get("updated_at") or item.get("created_at") or "")
        if latest_at > group["latest_at"]:
            group["latest_at"] = latest_at
    return sorted(
        groups.values(),
        key=lambda item: (str(item.get("latest_at") or ""), str(item.get("display_name") or "")),
        reverse=True,
    )


def _bridge_channel_payload(channel: dict[str, Any]) -> dict[str, Any]:
    conversation_id = str(channel.get("conversation_id") or "").strip()
    conversation_type = str(channel.get("conversation_type") or "")
    sender_ids = channel.get("sender_wechat_ids") if isinstance(channel.get("sender_wechat_ids"), list) else []
    conversation_key = str(channel.get("conversation_key") or "").strip()
    # Mirror bridge_send._channel_receiver so the panel shows the true receiver:
    # prefer the persisted talker id; for groups only a @chatroom id is valid
    # (a member wxid would misroute the reply privately).
    if conversation_type == "private" and not channel_allows_private_receiver(channel):
        receiver = ""
    elif _looks_like_wechat_receiver(conversation_key):
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
        "is_friend": bool(channel.get("is_friend", False)),
        "contact_authorization": str(channel.get("contact_authorization") or ""),
    }


def _looks_like_wechat_receiver(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text.startswith("wxid_") or text.startswith("gh_") or text.endswith("@chatroom"))


def _visible_channel_policy(config: Any) -> dict[str, set[str]]:
    return {
        "accepted_contacts": {str(item).strip() for item in config.accepted_contacts if str(item).strip()},
        "accepted_groups": {str(item).strip() for item in config.accepted_groups if str(item).strip()},
    }


def _sidebar_channel_visible(channel: Any, policy: dict[str, set[str]] | None = None) -> tuple[bool, str]:
    title = str(channel.chat_title or "").strip()
    if not title:
        return False, "empty_title"
    if title.lower() in {"wechat agent console", "windows powershell", "powershell", "codex"}:
        return False, "tool_window"
    if _channel_is_unidentified_private(channel, policy or {}):
        return False, "private_contact_unknown_or_unidentified"
    if _channel_has_visible_trust(channel, policy or {}):
        return True, ""
    if _looks_like_probe_fragment(title):
        return False, "probe_fragment"
    if _looks_like_mojibake(title):
        return False, "mojibake"
    return False, "untrusted_channel"


def _channel_is_unidentified_private(channel: Any, policy: dict[str, set[str]]) -> bool:
    if str(getattr(channel, "conversation_type", "") or "") != "private":
        return False
    conversation_id = str(getattr(channel, "conversation_id", "") or "").strip()
    title = str(getattr(channel, "chat_title", "") or "").strip()
    identity = str(getattr(channel, "conversation_key", "") or "").strip()
    sender_ids = [str(item).strip() for item in getattr(channel, "sender_wechat_ids", []) if str(item).strip()]
    if not identity and sender_ids:
        identity = sender_ids[0]
    sender_names = [str(item).strip() for item in getattr(channel, "sender_names", []) if str(item).strip()]
    accepted_contacts = policy.get("accepted_contacts", set())
    accepted_candidates = {identity, title, *sender_ids, *sender_names}
    accepted_candidates.discard("")
    if accepted_candidates.intersection(accepted_contacts):
        return False
    if identity and not _looks_like_wechat_receiver(identity):
        return False
    titles = [title, *sender_names]
    return not any(_channel_title_identifies_private_contact(title, identity) for title in titles)


def _channel_title_identifies_private_contact(title: str, identity: str) -> bool:
    text = str(title or "").strip()
    if not text:
        return False
    if text == identity and _looks_like_wechat_receiver(identity):
        return False
    if _looks_like_wechat_receiver(text):
        return False
    if text.lower() in {"unknown", "unknown contact", "unknown user", "未知", "未知联系人", "未知用户", "system", "none", "null"}:
        return False
    if _looks_like_mojibake(text):
        return False
    if not any(char.isalnum() for char in text):
        return False
    return True


def _channel_has_visible_trust(channel: Any, policy: dict[str, set[str]]) -> bool:
    if bool(getattr(channel, "trusted_channel_source", False)):
        return True
    source_names = {str(item).strip() for item in getattr(channel, "source_names", []) if str(item).strip()}
    if source_names.intersection({"backend_events_jsonl", "backend_file_watcher", "manual_backend_event", "weflow_discovery", "weflow_pull"}):
        return True
    conversation_id = str(getattr(channel, "conversation_id", "")).strip()
    title = str(getattr(channel, "chat_title", "")).strip()
    sender_names = {str(item).strip() for item in getattr(channel, "sender_names", []) if str(item).strip()}
    sender_ids = {str(item).strip() for item in getattr(channel, "sender_wechat_ids", []) if str(item).strip()}
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
    if len(title) <= 2 and not re.search(r"[\u4e00-\u9fffA-Za-z]", title) and not _contains_emoji_symbol(title):
        return True
    return False


def _looks_like_mojibake(title: str) -> bool:
    if _contains_emoji_symbol(title):
        return False
    suspicious = ("�", "锟", "锛", "绔", "馃", "鈥", "鐚", "鍟", "娴", "灏", "鏃", "闀", "涓", "鎺", "乬")
    if any(token in title for token in suspicious):
        return True
    non_ascii = sum(1 for char in title if ord(char) > 127)
    if non_ascii >= 2 and not re.search(r"[\u4e00-\u9fff]", title):
        return True
    return False


def _contains_emoji_symbol(value: str) -> bool:
    for char in str(value or ""):
        if unicodedata.category(char) == "So" and ord(char) >= 0x2600:
            return True
    return False


def _reason_counts(hidden: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in hidden:
        reason = str(item.get("hidden_reason", "unknown"))
        counts[reason] = counts.get(reason, 0) + 1
    return counts

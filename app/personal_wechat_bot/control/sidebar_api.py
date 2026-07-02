from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, is_dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.conversation.channel_store import ConversationChannelStore
from app.personal_wechat_bot.config.loader import load_config, migrate_file_allowed_extensions
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool
from app.personal_wechat_bot.persona.runtime_cards import RuntimeCardStore
from app.personal_wechat_bot.runtime.hook_pull_runner import HookMessagePullRunner
from app.personal_wechat_bot.runtime.polling_runner import PollingRunner
from app.personal_wechat_bot.runtime.process_lock import ProcessLock, ProcessLockError
from app.personal_wechat_bot.runtime.weflow_state_summary import summarize_weflow_bridge_state
from app.personal_wechat_bot.runtime.weflow_worker_metrics import WeflowWorkerMetrics
from app.personal_wechat_bot.tools.permissions import resolve_allowed_roots
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
from app.personal_wechat_bot.wechat_driver.backend_events import BackendEventJsonlDriver
from app.personal_wechat_bot.wechat_driver.backend_events import append_backend_event_payload
from app.personal_wechat_bot.wechat_driver.bridge_send import bridge_ack, bridge_state
from app.personal_wechat_bot.wechat_driver.hook_events import HookEventJsonlImporter
from app.personal_wechat_bot.wechat_driver.hook_source_bridge import (
    WeFlowHttpBridge,
    require_weflow_ready,
    weflow_health_status,
)
from app.personal_wechat_bot.wechat_driver.voice_cache_resolver import WeChatVoiceCacheResolver


QUEUE_STATUSES = ("pending", "approved", "queued_to_bridge", "rejected", "sent", "failed")
_WEFLOW_WORKERS: dict[str, dict[str, Any]] = {}
_WEFLOW_LOCK = threading.Lock()


def build_sidebar_state(data_dir: str | Path = "data") -> dict[str, Any]:
    config = load_config(data_dir)
    queues = {status: list_confirm_queue(data_dir, status=status) for status in QUEUE_STATUSES}
    channels = _channel_state(data_dir)
    send_bridge = bridge_state(data_dir, limit=12)
    return {
        "status": "ok",
        "role": "visual_audit_console",
        "capture": {
            "owner": "backend_message_sources",
            "sidebar_role": "audit_and_send_controls_only",
            "window_probe_role": "diagnostic_only",
            "supports_multi_conversation": True,
            "send_driver_boundary": "windows_guarded requires foreground WeChat for output; backend events can receive multiple conversations without page OCR",
            "input_pipeline": "POST /api/backend-events or append-backend-event -> backend_events.jsonl -> run-agent/poll-backend-events -> conversation_ledgers",
            "background_send_status": _background_send_status(config, send_bridge),
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
    return bridge_state(data_dir, limit=50)


def build_sidebar_weflow_state(data_dir: str | Path = "data") -> dict[str, Any]:
    root = Path(data_dir)
    persisted = _read_json(root / "weflow_sidebar_state.json", {})
    worker = _weflow_worker_state(root)
    try:
        migration = migrate_file_allowed_extensions(root)
    except Exception as exc:
        migration = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "ok",
        "base_url": str(persisted.get("base_url") or "http://127.0.0.1:5031"),
        "token_env": str(persisted.get("token_env") or "WEFLOW_API_TOKEN"),
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
        "bridge_state": summarize_weflow_bridge_state(root / "weflow_bridge_state.json"),
        "last_health": persisted.get("last_health", {}) if isinstance(persisted, dict) else {},
        "last_pull": persisted.get("last_pull", {}) if isinstance(persisted, dict) else {},
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
    return result


def sidebar_weflow_pull_once(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    result = _run_sidebar_weflow_once(data_dir, payload)
    _write_weflow_sidebar_state(data_dir, {"last_pull": result, **_weflow_public_params(_weflow_params(data_dir, payload))})
    return result


def sidebar_weflow_start(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    key = str(root)
    with _WEFLOW_LOCK:
        existing = _WEFLOW_WORKERS.get(key)
        if existing and existing.get("thread") and existing["thread"].is_alive():
            return {"status": "ok", "worker": _weflow_worker_state(root), "message": "WeFlow 后台拉取已经在运行"}
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
    return {"status": "ok", "worker": _weflow_worker_state(root), "message": "WeFlow 后台拉取已启动"}


def sidebar_weflow_stop(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    key = str(root)
    with _WEFLOW_LOCK:
        worker = _WEFLOW_WORKERS.get(key)
        if worker and worker.get("stop"):
            worker["stop"].set()
    return {"status": "ok", "worker": _weflow_worker_state(root), "message": "WeFlow 后台拉取停止信号已发送"}


def sidebar_weflow_dependency_status(data_dir: str | Path = "data") -> dict[str, Any]:
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
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "status": "ok" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
        "dependencies": sidebar_weflow_dependency_status(data_dir),
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
    reason = str(payload.get("reason", "")).strip()
    external_message_id = str(payload.get("external_message_id") or payload.get("externalMessageId") or "").strip()
    extra = payload.get("payload")
    return bridge_ack(
        data_dir,
        bridge_id,
        status=status,
        reason=reason,
        external_message_id=external_message_id,
        payload=extra if isinstance(extra, dict) else {},
    )


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
    )
    return {
        "params": params,
        "bridge": bridge,
        "runner": runner,
        "weflow_ready": weflow_ready,
        "media_roots": media_roots,
    }


def _run_weflow_pull_tick(context: dict[str, Any]) -> dict[str, Any]:
    params = context["params"]
    bridge: WeFlowHttpBridge = context["bridge"]
    runner: HookMessagePullRunner = context["runner"]
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
    )
    pull = runner.run_once()
    return {
        "status": "ok" if source.status == "ok" and pull.get("status") == "ok" else "partial_error",
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


def _run_sidebar_weflow_once(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    context = _build_weflow_pull_context(data_dir, payload)
    return _run_weflow_pull_tick(context)


def _weflow_background_loop(root: Path, payload: dict[str, Any], stop_event: threading.Event) -> None:
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
        return
    context: dict[str, Any] | None = None
    try:
        while not stop_event.is_set():
            tick_started = time.monotonic()
            try:
                if context is None:
                    context = _build_weflow_pull_context(root, payload)
                result = _run_weflow_pull_tick(context)
                duration = time.monotonic() - tick_started
                source_status = str(result.get("source", {}).get("status") or "")
                if source_status == "error":
                    # A total pull failure usually means WeFlow went away. Drop the
                    # context so the next tick rebuilds it and re-runs the health /
                    # fork-marker check before pulling again.
                    context = None
                consumer_lock.heartbeat()
                with _WEFLOW_LOCK:
                    worker = _WEFLOW_WORKERS.get(key, {})
                    metrics = worker.get("metrics")
                    if isinstance(metrics, WeflowWorkerMetrics):
                        metrics.record_tick(result, duration)
                    worker["last_status"] = result.get("status", "")
                    worker["last_tick_at"] = time.time()
                    _WEFLOW_WORKERS[key] = worker
                _write_weflow_sidebar_state(root, {"last_pull": result, "last_error": "", **_weflow_public_params(_weflow_params(root, payload))})
            except Exception as exc:
                context = None
                duration = time.monotonic() - tick_started
                error = f"{type(exc).__name__}: {exc}"
                consumer_lock.heartbeat()
                with _WEFLOW_LOCK:
                    worker = _WEFLOW_WORKERS.get(key, {})
                    metrics = worker.get("metrics")
                    if isinstance(metrics, WeflowWorkerMetrics):
                        metrics.record_error(error, duration)
                    worker["last_status"] = "error"
                    worker["last_error"] = error
                    worker["last_tick_at"] = time.time()
                    _WEFLOW_WORKERS[key] = worker
                _write_weflow_sidebar_state(root, {"last_error": error, **_weflow_public_params(_weflow_params(root, payload))})
            stop_event.wait(interval)
    finally:
        consumer_lock.release()


def _weflow_params(data_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(data_dir)
    token_env = str(payload.get("token_env") or payload.get("tokenEnv") or "WEFLOW_API_TOKEN").strip()
    token = str(payload.get("token") or "").strip() or os.environ.get(token_env, "") or os.environ.get("WEFLOW_API_TOKEN", "")
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
        }
        if isinstance(metrics, WeflowWorkerMetrics):
            snapshot = metrics.snapshot(running=running)
            state["loops"] = snapshot["loops"]
            state["metrics"] = snapshot
    return state


def _write_weflow_sidebar_state(data_dir: str | Path, update: dict[str, Any]) -> None:
    path = Path(data_dir) / "weflow_sidebar_state.json"
    current = _read_json(path, {})
    payload = current if isinstance(current, dict) else {}
    payload.update(update)
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_json(path, payload)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


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


def _background_send_status(config: Any, bridge: dict[str, Any]) -> str:
    if str(getattr(config, "send_driver", "")) == "bridge_outbox":
        if not bool(getattr(config, "send_enabled", False)):
            return "bridge_outbox_configured_disabled"
        if int(bridge.get("manual_bound_count", 0) or 0) > 0:
            return "bridge_outbox_ready_for_manual_channels"
        return "bridge_outbox_waiting_for_manual_capture"
    return "bridge_outbox_manual_capture_only_available"


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

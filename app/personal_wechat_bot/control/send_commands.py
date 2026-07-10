from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.config.loader import load_config, save_config
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.domain.models import ReplyCandidate, ToolCallResult, utc_now_iso
from app.personal_wechat_bot.reply_gate.send_audit import SendAuditLog
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue
from app.personal_wechat_bot.reply_gate.send_executor import GuardedSendExecutor
from app.personal_wechat_bot.tasks.manager import TaskStatusStore, task_priority_score
from app.personal_wechat_bot.wechat_driver.send_driver_factory import build_send_driver, probe_send_driver
from app.personal_wechat_bot.wechat_driver.bridge_send import (
    BridgeAckStatus,
    bridge_requeue_resolved,
    is_terminal_bridge_ack_status,
)


_BRIDGE_ID_RE = re.compile(r"(?<![A-Za-z0-9_])bridge:[^\s,;，；。)）\]】]+")


def set_send_controls(
    data_dir: str | Path,
    *,
    mode: str | None = None,
    enabled: bool | None = None,
    driver: str | None = None,
    backend: str | None = None,
    weflow_base_url: str | None = None,
    weflow_token_env: str | None = None,
    weflow_send_text_path: str | None = None,
    weflow_send_file_path: str | None = None,
    weflow_send_timeout_seconds: float | None = None,
    wechat_native_base_url: str | None = None,
    wechat_native_send_text_path: str | None = None,
    wechat_native_send_image_path: str | None = None,
    wechat_native_send_file_path: str | None = None,
    wechat_native_status_path: str | None = None,
    wechat_native_timeout_seconds: float | None = None,
    wechat_native_verify_timeout_seconds: float | None = None,
    wechat_native_file_verify_timeout_seconds: float | None = None,
    confirm_required: bool | None = None,
    max_chars: int | None = None,
    min_interval_seconds: int | None = None,
) -> dict[str, Any]:
    config = load_config(data_dir)
    if mode is not None:
        if mode not in {"dry_run", "confirm", "auto"}:
            raise ValueError("mode must be dry_run, confirm, or auto")
        config.mode = mode
        if confirm_required is None:
            config.send_confirm_required = mode != "auto"
    if enabled is not None:
        config.send_enabled = enabled
    if driver is not None:
        config.send_driver = driver
    if backend is not None:
        normalized_backend = str(backend or "").strip().lower()
        if normalized_backend not in {"dry_run", "weflow_http", "wechat_native_http"}:
            raise ValueError("send_backend must be dry_run, weflow_http, or wechat_native_http")
        config.send_backend = normalized_backend
    if weflow_base_url is not None:
        config.weflow_base_url = str(weflow_base_url or "").strip() or "http://127.0.0.1:5031"
    if weflow_token_env is not None:
        config.weflow_token_env = str(weflow_token_env or "").strip() or "WEFLOW_API_TOKEN"
    if weflow_send_text_path is not None:
        config.weflow_send_text_path = str(weflow_send_text_path or "").strip() or "/send/text"
    if weflow_send_file_path is not None:
        config.weflow_send_file_path = str(weflow_send_file_path or "").strip() or "/send/file"
    if weflow_send_timeout_seconds is not None:
        config.weflow_send_timeout_seconds = max(1.0, float(weflow_send_timeout_seconds))
    if wechat_native_base_url is not None:
        config.wechat_native_base_url = str(wechat_native_base_url or "").strip() or "http://127.0.0.1:30001"
    if wechat_native_send_text_path is not None:
        config.wechat_native_send_text_path = str(wechat_native_send_text_path or "").strip() or "/SendTextMsg"
    if wechat_native_send_image_path is not None:
        config.wechat_native_send_image_path = str(wechat_native_send_image_path or "").strip() or "/SendImgMsg"
    if wechat_native_send_file_path is not None:
        config.wechat_native_send_file_path = str(wechat_native_send_file_path or "").strip() or "/send_file_msg"
    if wechat_native_status_path is not None:
        config.wechat_native_status_path = str(wechat_native_status_path or "").strip() or "/QueryDB/status"
    if wechat_native_timeout_seconds is not None:
        config.wechat_native_timeout_seconds = max(1.0, float(wechat_native_timeout_seconds))
    if wechat_native_verify_timeout_seconds is not None:
        config.wechat_native_verify_timeout_seconds = max(0.0, float(wechat_native_verify_timeout_seconds))
    if wechat_native_file_verify_timeout_seconds is not None:
        config.wechat_native_file_verify_timeout_seconds = max(0.0, float(wechat_native_file_verify_timeout_seconds))
    if confirm_required is not None:
        config.send_confirm_required = confirm_required
    if max_chars is not None:
        config.send_max_chars = max_chars
    if min_interval_seconds is not None:
        config.send_min_interval_seconds = min_interval_seconds
    save_config(config)
    return _send_config_payload(config)


def list_confirm_queue(data_dir: str | Path, status: str = "pending") -> dict[str, Any]:
    _repair_obsolete_sidebar_test_confirm_items(data_dir)
    queue = _queue(data_dir)
    items = queue.list_by_status(status)
    return {"status": "ok", "filter": status, "count": len(items), "items": items}


def _repair_obsolete_sidebar_test_confirm_items(data_dir: str | Path) -> list[dict[str, Any]]:
    """Retire old manual sidebar probes after the operator switches to auto mode."""

    try:
        config = load_config(data_dir)
    except Exception:
        return []
    if str(getattr(config, "mode", "") or "") != "auto" or bool(getattr(config, "send_confirm_required", True)):
        return []
    queue = _queue(data_dir)
    retired_message_ids = _obsolete_sidebar_confirm_test_message_ids(data_dir)
    if not retired_message_ids:
        return []
    reason = "obsolete_sidebar_confirm_test_task:auto_mode_active"
    repaired: list[dict[str, Any]] = []
    for item in queue.list_by_status("approved"):
        reply = item.get("reply") if isinstance(item.get("reply"), dict) else {}
        message_id = str(reply.get("message_id") or "").strip()
        if message_id not in retired_message_ids:
            continue
        queue_id = str(item.get("queue_id") or "")
        if not queue_id:
            continue
        try:
            updated = queue.mark_send_result(queue_id, "failed", reason, reviewer="system")
        except Exception:
            continue
        repaired.append(updated)
        _audit(data_dir).append(
            "confirm_queue_repaired",
            queue_id=queue_id,
            status="failed",
            reason=reason,
            reviewer="system",
            payload={"message_id": message_id},
        )
        _safe_sync_queue_item_to_ledger(data_dir, updated, status="failed", reason=reason)
        _update_send_task_from_queue_item(
            data_dir,
            updated,
            action="cancel",
            patch={"progress": 100, "phase": "旧 sidebar 自测审核项已退休", "detail": reason},
        )
    return repaired


def _obsolete_sidebar_confirm_test_message_ids(data_dir: str | Path) -> set[str]:
    try:
        tasks = TaskStatusStore(data_dir).state(limit=1000).get("tasks", [])
    except Exception:
        return set()
    ids: set[str] = set()
    task_items = tasks if isinstance(tasks, list) else []
    for task in task_items:
        if str(task.get("kind") or "") != "send":
            continue
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        message_id = str(metadata.get("message_id") or "").strip()
        if not message_id.startswith("sidebar_channel_test_"):
            continue
        evidence = " ".join(str(task.get(key) or "") for key in ("detail", "last_error", "phase"))
        if "obsolete_sidebar_confirm_test_task" in evidence:
            ids.add(message_id)
    return ids


def approve_confirm_item(data_dir: str | Path, queue_id: str, *, reviewer: str, note: str = "") -> dict[str, Any]:
    item = _queue(data_dir).approve(queue_id, reviewer=reviewer, note=note)
    _audit(data_dir).append(
        "confirm_approve",
        queue_id=queue_id,
        status=item["status"],
        reviewer=reviewer,
        note=note,
        payload=_queue_item_audit_payload(item),
    )
    _safe_sync_queue_item_to_ledger(data_dir, item, status="approved", reason=note or "confirm_approved")
    _update_send_task_from_queue_item(
        data_dir,
        item,
        action="resume",
        patch={"progress": 55, "phase": "审核通过，等待发送", "detail": note or "confirm_approved", "blocker": ""},
    )
    return {"status": "ok", "item": item}


def reject_confirm_item(data_dir: str | Path, queue_id: str, *, reviewer: str, note: str = "") -> dict[str, Any]:
    item = _queue(data_dir).reject(queue_id, reviewer=reviewer, note=note)
    _audit(data_dir).append(
        "confirm_reject",
        queue_id=queue_id,
        status=item["status"],
        reviewer=reviewer,
        note=note,
        payload=_queue_item_audit_payload(item),
    )
    _safe_sync_queue_item_to_ledger(data_dir, item, status="rejected", reason=note or "confirm_rejected")
    _update_send_task_from_queue_item(
        data_dir,
        item,
        action="cancel",
        patch={"progress": 100, "phase": "发送审核已拒绝", "detail": note or "confirm_rejected"},
    )
    return {"status": "ok", "item": item}


def remove_confirm_item(data_dir: str | Path, queue_id: str, *, reviewer: str = "local_user", note: str = "") -> dict[str, Any]:
    item = _queue(data_dir).remove(queue_id)
    removal_reason = note or "removed_from_confirm_queue"
    _audit(data_dir).append(
        "confirm_remove",
        queue_id=queue_id,
        status=str(item.get("status", "")),
        reviewer=reviewer,
        note=removal_reason,
        payload=_queue_item_audit_payload(item),
    )
    ledger_sync_error = _safe_sync_queue_item_to_ledger(
        data_dir,
        item,
        status="removed",
        reason=removal_reason,
    )
    _update_send_task_from_queue_item(
        data_dir,
        item,
        action="cancel",
        patch={"progress": 100, "phase": "待审回复已移除", "detail": removal_reason},
    )
    return {"status": "ok", "removed": True, "item": item, "ledger_sync_error": ledger_sync_error}


def send_approved_confirm_item(data_dir: str | Path, queue_id: str, driver: Any | None = None) -> dict[str, Any]:
    config = load_config(data_dir)
    queue = _queue(data_dir)
    item = queue.get(queue_id)
    if item is None:
        raise KeyError(f"queue_id not found: {queue_id}")
    if item.get("status") != "approved":
        _audit(data_dir).append(
            "confirm_send_blocked",
            queue_id=queue_id,
            status="blocked",
            reason=f"queue item status is {item.get('status')}",
        )
        _update_send_task_from_queue_item(
            data_dir,
            item,
            action="block",
            patch={"progress": 60, "phase": "发送被阻塞", "detail": f"queue item status is {item.get('status')}"},
        )
        return {"status": "blocked", "reason": f"queue item status is {item.get('status')}", "queue_id": queue_id}
    reply = _reply_from_queue_item(item)
    _update_send_task_from_reply(
        data_dir,
        reply,
        action="start",
        patch={"progress": 65, "phase": "正在提交发送", "detail": ""},
    )
    driver = driver if driver is not None else build_send_driver(config)
    result = GuardedSendExecutor(config, driver).execute_confirmed(reply)
    if _send_result_should_stay_approved(result.reason):
        _audit(data_dir).append(
            "confirm_send_blocked",
            queue_id=queue_id,
            status="blocked",
            reason=result.reason,
            payload={"conversation_id": reply.conversation_id, "message_id": reply.message_id},
        )
        _update_send_task_from_reply(
            data_dir,
            reply,
            action="block",
            patch={"progress": 65, "phase": "发送条件未满足", "detail": result.reason, "last_error": result.reason},
        )
        return {
            "status": "blocked",
            "reason": result.reason,
            "send_result": result.__dict__,
            "item": item,
            "queue_status": str(item.get("status", "")),
        }
    final_status = result.status if result.status in {"sent", "accepted", "queued_to_bridge"} else "failed"
    send_payload = result.__dict__
    updated = queue.mark_send_result(
        queue_id,
        final_status,
        result.reason,
        extra=_queue_send_result_extra(send_payload),
    )
    _audit(data_dir).append(
        "confirm_send_attempt",
        queue_id=queue_id,
        status=final_status,
        reason=result.reason,
        payload={
            "conversation_id": reply.conversation_id,
            "message_id": reply.message_id,
            "send_result": send_payload,
        },
    )
    # The message is already sent and the queue is already marked above. A failure
    # while syncing the ledger must not mask a successful send, so swallow it here
    # (recording it to the audit log) and still return the real send status.
    ledger_sync_error = _safe_sync_queue_item_to_ledger(
        data_dir, updated, status=final_status, reason=result.reason, send_result=send_payload
    )
    _finish_send_task_from_result(data_dir, reply, final_status, result.reason)
    return {
        "status": final_status,
        "send_result": result.__dict__,
        "item": updated,
        "ledger_sync_error": ledger_sync_error,
    }


def sync_bridge_ack_to_send_state(
    data_dir: str | Path,
    bridge_id: str,
    *,
    status: str,
    reason: str = "",
    external_message_id: str = "",
) -> dict[str, Any]:
    bridge_id = str(bridge_id or "").strip()
    if not bridge_id:
        return {"status": "skipped", "reason": "bridge_id_empty"}
    status = str(status or "").strip()
    if not is_terminal_bridge_ack_status(status):
        return {
            "status": "skipped",
            "reason": "bridge_ack_not_terminal",
            "bridge_id": bridge_id,
            "ack_status": status,
        }
    queue = _queue(data_dir)
    queue_item: dict[str, Any] | None = None
    queue_updated: dict[str, Any] = {}
    queue_error = ""
    queue_sync_status = "not_found"
    mapped_queue_status = (
        "sent"
        if status == BridgeAckStatus.SENT
        else ("accepted" if status == BridgeAckStatus.ACCEPTED else "failed")
    )
    ledger_status = status
    bridge_ack_reason = reason or f"bridge_ack:{status}"
    queue_reason = bridge_ack_reason
    ack_context: dict[str, Any] = {}

    def _apply_queue_bridge_ack(current_item: dict[str, Any]) -> dict[str, Any]:
        nonlocal bridge_ack_reason, ledger_status, queue_reason, queue_sync_status
        current_queue_status = str(current_item.get("status", ""))
        expected_bridge_ids = _bridge_ids_from_queue_item(current_item, bridge_id)
        bridge_acks = _queue_bridge_acks_with_ack(
            current_item,
            bridge_id,
            ack_status=status,
            queue_status=mapped_queue_status,
            reason=bridge_ack_reason,
            external_message_id=external_message_id,
        )
        aggregate_queue_status = (
            _aggregate_queue_bridge_status(expected_bridge_ids, bridge_acks)
            if len(expected_bridge_ids) > 1
            else mapped_queue_status
        )
        if len(expected_bridge_ids) > 1:
            queue_reason = _queue_bridge_ack_summary(expected_bridge_ids, bridge_acks)
        ack_context.update(
            {
                "expected_bridge_ids": expected_bridge_ids,
                "bridge_acks": bridge_acks,
                "aggregate_queue_status": aggregate_queue_status,
            }
        )
        if current_queue_status == BridgeAckStatus.SENT and aggregate_queue_status != BridgeAckStatus.SENT:
            queue_sync_status = "preserved_sent"
            ledger_status = BridgeAckStatus.SENT
            preserved_reason = str(current_item.get("note", "")) or bridge_ack_reason
            bridge_ack_reason = preserved_reason
            queue_reason = preserved_reason
            return current_item
        current_item["status"] = aggregate_queue_status
        current_item["reviewed_at"] = utc_now_iso()
        current_item["reviewer"] = "local_user"
        current_item["note"] = queue_reason
        current_item["bridge_ids"] = expected_bridge_ids
        current_item["bridge_acks"] = bridge_acks
        current_item["last_bridge_ack"] = bridge_acks.get(bridge_id, {})
        queue_sync_status = "updated"
        return current_item

    try:
        queue_item, updated_queue_item, _queue_changed = queue.update_referencing_bridge(bridge_id, _apply_queue_bridge_ack)
        queue_updated = updated_queue_item or {}
    except Exception as exc:
        queue_error = f"{type(exc).__name__}: {exc}"

    if queue_item is not None and queue_updated:
        try:
            ledger_sync_error = _safe_sync_queue_item_to_ledger(
                data_dir,
                queue_updated,
                status=str(queue_updated.get("status", ack_context.get("aggregate_queue_status", mapped_queue_status))),
                reason=queue_reason,
                send_result={
                    "message_id": bridge_id,
                    "status": str(queue_updated.get("status", ack_context.get("aggregate_queue_status", mapped_queue_status))),
                    "reason": queue_reason,
                    "sent_at": utc_now_iso()
                    if str(queue_updated.get("status", ack_context.get("aggregate_queue_status", mapped_queue_status))) == "sent"
                    else "",
                    "external_message_id": external_message_id,
                },
            )
            if ledger_sync_error:
                queue_error = ledger_sync_error
        except Exception as exc:
            queue_error = f"{type(exc).__name__}: {exc}"

    ledger_updates = _sync_bridge_ack_to_ledgers(
        data_dir,
        bridge_id,
        status=ledger_status,
        reason=bridge_ack_reason,
        external_message_id=external_message_id,
    )
    task_updates = _finish_send_task_for_bridge(data_dir, bridge_id, status=status, reason=bridge_ack_reason)
    result = {
        "status": "ok",
        "bridge_id": bridge_id,
        "queue_item_found": queue_item is not None,
        "queue_item": queue_updated,
        "queue_sync_status": queue_sync_status,
        "queue_error": queue_error,
        "ledger_updates": ledger_updates,
        "task_updates": task_updates,
    }
    _append_bridge_ack_audit(
        data_dir,
        bridge_id=bridge_id,
        ack_status=status,
        queue_status=str(queue_updated.get("status") or mapped_queue_status),
        reason=bridge_ack_reason,
        external_message_id=external_message_id,
        queue_item=queue_updated if queue_updated else queue_item,
        result=result,
    )
    return result


def retry_bridge_item(
    data_dir: str | Path,
    bridge_id: str,
    *,
    reviewer: str = "local_user",
    note: str = "",
) -> dict[str, Any]:
    bridge_id = str(bridge_id or "").strip()
    if not bridge_id:
        raise ValueError("bridge_id is required")
    reason = note or f"manual_bridge_retry:{reviewer}"
    retry = bridge_requeue_resolved(data_dir, bridge_id, reason=reason)
    new_bridge_id = str(retry.get("new_bridge_id") or "")
    if not new_bridge_id:
        raise RuntimeError("bridge retry did not return a new bridge_id")
    queue_item = _queue(data_dir).requeue_bridge_result(
        bridge_id,
        new_bridge_id,
        f"retry_to_non_foreground_bridge:{new_bridge_id}",
        reviewer=reviewer,
    )
    ledger_updates = _requeue_bridge_send_result_in_ledgers(
        data_dir,
        bridge_id,
        new_bridge_id,
        reason=f"retry_to_non_foreground_bridge:{new_bridge_id}",
    )
    task_updates = _requeue_send_tasks_for_bridge(
        data_dir,
        bridge_id,
        new_bridge_id,
        reason=f"retry_to_non_foreground_bridge:{new_bridge_id}",
    )
    _audit(data_dir).append(
        "bridge_retry",
        queue_id=bridge_id,
        status="queued_to_bridge",
        reason=f"retry_to_non_foreground_bridge:{new_bridge_id}",
        reviewer=reviewer,
        note=note,
        payload={
            "old_bridge_id": bridge_id,
            "new_bridge_id": new_bridge_id,
            "queue_item_found": queue_item is not None,
            "ledger_updates": ledger_updates,
            "task_update_count": len(task_updates),
        },
    )
    return {
        "status": "ok",
        "old_bridge_id": bridge_id,
        "new_bridge_id": new_bridge_id,
        "queue_item_found": queue_item is not None,
        "queue_item": queue_item or {},
        "ledger_updates": ledger_updates,
        "task_updates": task_updates,
        "bridge": retry.get("state", {}),
    }


def list_send_audit(
    data_dir: str | Path,
    *,
    limit: int = 20,
    status: str | None = None,
    include_resolved: bool = False,
    compact_transitions: bool = False,
) -> dict[str, Any]:
    items = _audit(data_dir).list_recent(
        limit=limit,
        status=status,
        include_resolved=include_resolved,
        compact_transitions=compact_transitions,
    )
    return {"status": "ok", "count": len(items), "items": items}


def clear_send_audit(data_dir: str | Path) -> dict[str, Any]:
    cleared_count = _audit(data_dir).clear()
    return {"status": "ok", "count": 0, "items": [], "cleared_count": cleared_count}


def probe_send_controls(data_dir: str | Path, *, driver: str | None = None) -> dict[str, Any]:
    config = load_config(data_dir)
    if driver is not None:
        config.send_driver = driver
    probe = probe_send_driver(config)
    return {"status": "ok", "probe": probe}


def _update_send_task_from_queue_item(
    data_dir: str | Path,
    item: dict[str, Any],
    *,
    action: str,
    patch: dict[str, Any],
) -> None:
    try:
        reply = _reply_from_queue_item(item)
    except Exception:
        return
    _update_send_task_from_reply(data_dir, reply, action=action, patch=patch)


def _update_send_task_from_reply(
    data_dir: str | Path,
    reply: ReplyCandidate,
    *,
    action: str,
    patch: dict[str, Any],
) -> None:
    task_id = _send_task_id(reply.message_id)
    try:
        store = TaskStatusStore(data_dir)
        store.create(
            {
                "task_id": task_id,
                "title": "发送回复",
                "kind": "send",
                "conversation_id": reply.conversation_id,
                "scope": f"conversation:{reply.conversation_id}",
                "topic_id": _reply_topic_id(reply.message_id),
                "topic_title": "回复发送",
                "resource_class": "send_bridge",
                "priority": 85,
                "estimated_cost": 1,
                "metadata": {"message_id": reply.message_id},
            }
        )
        store.transition(task_id, action, patch)
    except Exception:
        return


def _finish_send_task_from_result(
    data_dir: str | Path,
    reply: ReplyCandidate,
    status: str,
    reason: str,
) -> None:
    bridge_ids = _bridge_ids_from_reason(reason)
    bridge_id = bridge_ids[0] if bridge_ids else ""
    if status == "queued_to_bridge":
        try:
            TaskStatusStore(data_dir).update(
                _send_task_id(reply.message_id),
                {
                    "status": "queued",
                    "progress": 75,
                    "phase": "等待非前台桥发送",
                    "detail": reason,
                    "external_id": bridge_id,
                    "metadata": {
                        "message_id": reply.message_id,
                        "bridge_id": bridge_id,
                        "bridge_ids": bridge_ids,
                        "bridge_acks": {},
                        "send_reason": reason,
                    },
                },
            )
        except Exception:
            return
        return
    action = "complete" if status == "sent" else "fail"
    _update_send_task_from_reply(
        data_dir,
        reply,
        action=action,
        patch={
            "progress": 100,
            "phase": _send_task_terminal_phase(status, reason),
            "detail": reason,
            "last_error": "" if action == "complete" else reason,
        },
    )


def _finish_send_task_for_bridge(data_dir: str | Path, bridge_id: str, *, status: str, reason: str) -> list[dict[str, Any]]:
    structured_updates = _sync_send_tasks_for_bridge_ack(data_dir, bridge_id, status=status, reason=reason)
    if structured_updates:
        return structured_updates
    final_status = "completed" if status in {BridgeAckStatus.SENT, BridgeAckStatus.ACCEPTED} else "failed"
    try:
        updated = TaskStatusStore(data_dir).finish_external(
            bridge_id,
            {
                "status": final_status,
                "progress": 100,
                "phase": _send_task_terminal_phase(status, reason, bridge=True),
                "detail": reason,
                "last_error": "" if final_status == "completed" else reason,
            },
        )
        if updated:
            return updated
    except Exception:
        return []
    # If the task was created before the bridge id was known, there is nothing
    # reliable to update here; ledger/queue sync above remains authoritative.
    return []


def _sync_send_tasks_for_bridge_ack(
    data_dir: str | Path,
    bridge_id: str,
    *,
    status: str,
    reason: str,
) -> list[dict[str, Any]]:
    bridge_id = str(bridge_id or "").strip()
    if not bridge_id:
        return []
    atomic_updates = _sync_send_tasks_for_bridge_ack_atomically(data_dir, bridge_id, status=status, reason=reason)
    if atomic_updates is not None:
        return atomic_updates
    try:
        store = TaskStatusStore(data_dir)
        state = store.state(limit=1000)
    except Exception:
        return []
    tasks = state.get("tasks") if isinstance(state.get("tasks"), list) else []
    updated: list[dict[str, Any]] = []
    for task in tasks:
        if str(task.get("kind") or "") != "send":
            continue
        current_status = str(task.get("status") or "")
        if current_status in {"completed", "failed", "cancelled"} and status != BridgeAckStatus.SENT:
            continue
        if not _task_references_bridge(task, bridge_id):
            continue
        task_id = str(task.get("task_id") or "")
        if not task_id:
            continue
        bridge_ids = _bridge_ids_from_task(task, bridge_id)
        bridge_acks = _task_bridge_acks_with_ack(task, bridge_id, status=status, reason=reason)
        aggregate_status = (
            _aggregate_queue_bridge_status(bridge_ids, bridge_acks)
            if len(bridge_ids) > 1
            else _queue_status_from_bridge_ack(status)
        )
        summary = _queue_bridge_ack_summary(bridge_ids, bridge_acks)
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        primary_bridge_id = str(metadata.get("bridge_id") or (bridge_ids[0] if bridge_ids else bridge_id))
        next_metadata = {
            **metadata,
            "bridge_id": primary_bridge_id,
            "bridge_ids": bridge_ids,
            "bridge_acks": bridge_acks,
            "last_bridge_ack": bridge_acks.get(bridge_id, {}),
        }
        patch: dict[str, Any]
        if aggregate_status == "queued_to_bridge":
            patch = {
                "status": "queued",
                "progress": 85,
                "phase": "等待非前台桥发送",
                "detail": summary,
                "last_error": "",
                "external_id": str(task.get("external_id") or (bridge_ids[0] if bridge_ids else bridge_id)),
                "metadata": next_metadata,
            }
        else:
            final_status = "completed" if aggregate_status in {"sent", "accepted"} else "failed"
            patch = {
                "status": final_status,
                "progress": 100,
                "phase": _send_task_terminal_phase(aggregate_status, summary, bridge=True),
                "detail": summary,
                "last_error": "" if final_status == "completed" else summary,
                "external_id": str(task.get("external_id") or (bridge_ids[0] if bridge_ids else bridge_id)),
                "metadata": next_metadata,
            }
        try:
            updated.append(store.update(task_id, patch))
        except Exception:
            continue
    return updated


def _sync_send_tasks_for_bridge_ack_atomically(
    data_dir: str | Path,
    bridge_id: str,
    *,
    status: str,
    reason: str,
) -> list[dict[str, Any]] | None:
    try:
        store = TaskStatusStore(data_dir)
    except Exception:
        return None

    def mutate(tasks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[str, str, dict[str, Any]]]]:
        next_tasks: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        events: list[tuple[str, str, dict[str, Any]]] = []
        for task in tasks:
            if not isinstance(task, dict) or str(task.get("kind") or "") != "send":
                next_tasks.append(task)
                continue
            if not _task_references_bridge(task, bridge_id):
                next_tasks.append(task)
                continue
            task_id = str(task.get("task_id") or "")
            if not task_id:
                next_tasks.append(task)
                continue
            patch = _send_task_patch_for_bridge_ack(task, bridge_id, status=status, reason=reason)
            if not patch:
                next_tasks.append(task)
                continue
            patched = _merge_task_for_bridge_ack(task, patch)
            next_tasks.append(patched)
            updated.append(patched)
            events.append((task_id, "bridge_ack_sync", patched))
        return next_tasks, updated, events

    try:
        updated = store.scheduler_store.update_tasks_atomically(mutate)
        if updated:
            store._write_projection_from_sqlite()
        return updated
    except Exception:
        return None


def _send_task_patch_for_bridge_ack(
    task: dict[str, Any],
    bridge_id: str,
    *,
    status: str,
    reason: str,
) -> dict[str, Any]:
    current_status = str(task.get("status") or "")
    bridge_ids = _bridge_ids_from_task(task, bridge_id)
    bridge_acks = _task_bridge_acks_with_ack(task, bridge_id, status=status, reason=reason)
    aggregate_status = (
        _aggregate_queue_bridge_status(bridge_ids, bridge_acks)
        if len(bridge_ids) > 1
        else _queue_status_from_bridge_ack(status)
    )
    summary = _queue_bridge_ack_summary(bridge_ids, bridge_acks)
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    primary_bridge_id = str(metadata.get("bridge_id") or (bridge_ids[0] if bridge_ids else bridge_id))
    next_metadata = {
        **metadata,
        "bridge_id": primary_bridge_id,
        "bridge_ids": bridge_ids,
        "bridge_acks": bridge_acks,
        "last_bridge_ack": bridge_acks.get(bridge_id, {}),
    }
    external_id = str(task.get("external_id") or (bridge_ids[0] if bridge_ids else bridge_id))
    terminal_statuses = {"completed", "failed", "cancelled"}
    if current_status == "completed" and aggregate_status != "sent":
        return {
            "status": "completed",
            "progress": max(100, int(task.get("progress") or 0)),
            "phase": str(task.get("phase") or _send_task_terminal_phase("sent", summary, bridge=True)),
            "detail": str(task.get("detail") or summary),
            "last_error": "",
            "external_id": external_id,
            "metadata": next_metadata,
        }
    if current_status in terminal_statuses and aggregate_status == "queued_to_bridge":
        return {
            "status": current_status,
            "progress": int(task.get("progress") or 100),
            "phase": str(task.get("phase") or ""),
            "detail": str(task.get("detail") or summary),
            "last_error": str(task.get("last_error") or ""),
            "external_id": external_id,
            "metadata": next_metadata,
        }
    if aggregate_status == "queued_to_bridge":
        return {
            "status": "queued",
            "progress": 85,
            "phase": "waiting for non-foreground bridge send",
            "detail": summary,
            "last_error": "",
            "external_id": external_id,
            "metadata": next_metadata,
        }
    final_status = "completed" if aggregate_status in {"sent", "accepted"} else "failed"
    return {
        "status": final_status,
        "progress": 100,
        "phase": _send_task_terminal_phase(aggregate_status, summary, bridge=True),
        "detail": summary,
        "last_error": "" if final_status == "completed" else summary,
        "external_id": external_id,
        "metadata": next_metadata,
    }


def _merge_task_for_bridge_ack(task: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    updated = dict(task)
    for key, value in patch.items():
        if key == "metadata":
            updated[key] = value if isinstance(value, dict) else {}
        elif key not in {"task_id", "created_at", "priority_score"}:
            updated[key] = value
    now = utc_now_iso()
    status = str(updated.get("status") or "")
    if status in {"completed", "failed", "cancelled"} and not str(updated.get("finished_at") or ""):
        updated["finished_at"] = now
    updated["updated_at"] = now
    updated["priority_score"] = task_priority_score(updated)
    return updated


def _send_task_id(message_id: str) -> str:
    return f"send-{_task_id_fragment(message_id)}"


def _reply_topic_id(message_id: str) -> str:
    return f"reply-{_task_id_fragment(message_id)}"


def _send_task_terminal_phase(status: str, reason: str, *, bridge: bool = False) -> str:
    if status == BridgeAckStatus.SENT or status == "sent":
        if "dry_run_not_delivered" in str(reason or ""):
            return "非前台桥演练完成，未投递微信" if bridge else "发送演练完成，未投递微信"
        return "非前台桥发送完成" if bridge else "发送完成"
    if status == BridgeAckStatus.ACCEPTED or status == "accepted":
        return "非前台桥已接收，未验证微信送达" if bridge else "发送端口已接收，未验证微信送达"
    return "非前台桥发送失败" if bridge else "发送失败"


def _task_id_fragment(value: str) -> str:
    cleaned = "".join(ch for ch in str(value or "").strip() if ch.isalnum() or ch in {"-", "_", "."})
    return cleaned[:64] or "unknown"


def _queue_send_result_extra(send_result: dict[str, Any]) -> dict[str, Any]:
    bridge_ids = _bridge_ids_from_send_result(send_result)
    extra: dict[str, Any] = {"send_result": send_result}
    if bridge_ids:
        extra["bridge_ids"] = bridge_ids
        extra["bridge_acks"] = {}
    return extra


def _bridge_ids_from_send_result(send_result: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    details = send_result.get("details") if isinstance(send_result.get("details"), dict) else {}
    bridge_ids = details.get("bridge_ids") if isinstance(details.get("bridge_ids"), list) else []
    ids.extend(str(value) for value in bridge_ids)
    ids.extend(_bridge_ids_from_reason(str(send_result.get("reason") or "")))
    message_id = str(send_result.get("message_id") or "").strip()
    if message_id.startswith("bridge:"):
        ids.append(message_id)
    text = details.get("text") if isinstance(details.get("text"), dict) else {}
    text_id = str(text.get("message_id") or text.get("bridge_id") or "").strip()
    if text_id.startswith("bridge:"):
        ids.append(text_id)
    files = details.get("files") if isinstance(details.get("files"), list) else []
    for file_detail in files:
        if not isinstance(file_detail, dict):
            continue
        file_id = str(file_detail.get("message_id") or file_detail.get("bridge_id") or "").strip()
        if file_id.startswith("bridge:"):
            ids.append(file_id)
    return _dedupe_strings(ids)


def _bridge_ids_from_queue_item(item: dict[str, Any], bridge_id: str = "") -> list[str]:
    ids: list[str] = []
    bridge_ids = item.get("bridge_ids") if isinstance(item.get("bridge_ids"), list) else []
    ids.extend(str(value) for value in bridge_ids)
    send_result = item.get("send_result") if isinstance(item.get("send_result"), dict) else {}
    ids.extend(_bridge_ids_from_send_result(send_result))
    if ids:
        if bridge_id:
            ids.append(bridge_id)
        return _dedupe_strings(ids)
    ids.extend(_bridge_ids_from_reason(str(item.get("note") or "")))
    if bridge_id:
        ids.append(bridge_id)
    return _dedupe_strings(ids)


def _queue_bridge_acks_with_ack(
    item: dict[str, Any],
    bridge_id: str,
    *,
    ack_status: str,
    queue_status: str,
    reason: str,
    external_message_id: str,
) -> dict[str, Any]:
    existing = item.get("bridge_acks") if isinstance(item.get("bridge_acks"), dict) else {}
    updated = {str(key): dict(value) for key, value in existing.items() if isinstance(value, dict)}
    updated[bridge_id] = {
        "bridge_id": bridge_id,
        "ack_status": str(ack_status or ""),
        "queue_status": str(queue_status or ""),
        "status": str(queue_status or ""),
        "reason": str(reason or ""),
        "external_message_id": str(external_message_id or ""),
        "updated_at": utc_now_iso(),
    }
    return updated


def _task_bridge_acks_with_ack(task: dict[str, Any], bridge_id: str, *, status: str, reason: str) -> dict[str, Any]:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    existing = metadata.get("bridge_acks") if isinstance(metadata.get("bridge_acks"), dict) else {}
    updated = {str(key): dict(value) for key, value in existing.items() if isinstance(value, dict)}
    queue_status = _queue_status_from_bridge_ack(status)
    updated[bridge_id] = {
        "bridge_id": bridge_id,
        "ack_status": str(status or ""),
        "queue_status": queue_status,
        "status": queue_status,
        "reason": str(reason or ""),
        "updated_at": utc_now_iso(),
    }
    return updated


def _aggregate_queue_bridge_status(bridge_ids: list[str], bridge_acks: dict[str, Any]) -> str:
    statuses: list[str] = []
    for bridge_id in bridge_ids:
        ack = bridge_acks.get(bridge_id) if isinstance(bridge_acks.get(bridge_id), dict) else {}
        statuses.append(str(ack.get("queue_status") or ack.get("status") or "queued_to_bridge"))
    if any(item == "failed" for item in statuses):
        return "failed"
    if any(item == "queued_to_bridge" for item in statuses):
        return "queued_to_bridge"
    if any(item == "accepted" for item in statuses):
        return "accepted"
    if statuses and all(item == "sent" for item in statuses):
        return "sent"
    return statuses[-1] if statuses else "failed"


def _queue_bridge_ack_summary(bridge_ids: list[str], bridge_acks: dict[str, Any]) -> str:
    parts: list[str] = []
    for bridge_id in bridge_ids:
        ack = bridge_acks.get(bridge_id) if isinstance(bridge_acks.get(bridge_id), dict) else {}
        status = str(ack.get("queue_status") or ack.get("status") or "queued_to_bridge")
        reason = str(ack.get("reason") or "pending_bridge_ack")
        parts.append(f"{bridge_id}:{status}:{reason}")
    return "bridge_ack_parts:" + ";".join(parts) if parts else "bridge_ack_parts:empty"


def _queue_status_from_bridge_ack(status: str) -> str:
    if status == BridgeAckStatus.SENT:
        return "sent"
    if status == BridgeAckStatus.ACCEPTED:
        return "accepted"
    return "failed"


def _task_references_bridge(task: dict[str, Any], bridge_id: str) -> bool:
    if str(task.get("external_id") or "") == bridge_id:
        return True
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    if str(metadata.get("bridge_id") or "") == bridge_id:
        return True
    bridge_ids = metadata.get("bridge_ids") if isinstance(metadata.get("bridge_ids"), list) else []
    if bridge_id in {str(value) for value in bridge_ids}:
        return True
    return bridge_id in " ".join(
        str(value or "")
        for value in (
            task.get("detail"),
            task.get("last_error"),
            metadata.get("send_reason"),
        )
    )


def _bridge_ids_from_task(task: dict[str, Any], bridge_id: str = "") -> list[str]:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    ids: list[str] = []
    raw_bridge_ids = metadata.get("bridge_ids") if isinstance(metadata.get("bridge_ids"), list) else []
    ids.extend(str(value) for value in raw_bridge_ids)
    if ids:
        if bridge_id:
            ids.append(bridge_id)
        return _dedupe_strings(ids)
    for value in (
        task.get("external_id"),
        task.get("detail"),
        task.get("last_error"),
        metadata.get("bridge_id"),
        metadata.get("send_reason"),
    ):
        text = str(value or "").strip()
        if text.startswith("bridge:"):
            ids.append(text)
        ids.extend(_bridge_ids_from_reason(text))
    if bridge_id:
        ids.append(bridge_id)
    return _dedupe_strings(ids)


def _bridge_ids_from_reason(reason: str) -> list[str]:
    ids = [match.group(0).strip(".,;:!?，。；：！？") for match in _BRIDGE_ID_RE.finditer(str(reason or ""))]
    return _dedupe_strings([item for item in ids if item.startswith("bridge:")])


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _bridge_id_from_reason(reason: str) -> str:
    bridge_ids = _bridge_ids_from_reason(reason)
    return bridge_ids[-1] if bridge_ids else ""


def _queue(data_dir: str | Path) -> ConfirmQueue:
    return ConfirmQueue(Path(data_dir) / "confirm_queue.jsonl")


def _audit(data_dir: str | Path) -> SendAuditLog:
    return SendAuditLog(Path(data_dir) / "send_audit.jsonl")


def _queue_item_audit_payload(item: dict[str, Any]) -> dict[str, str]:
    reply = item.get("reply") if isinstance(item.get("reply"), dict) else {}
    return {
        "conversation_id": str(reply.get("conversation_id") or item.get("conversation_id") or "").strip(),
        "message_id": str(reply.get("message_id") or item.get("message_id") or "").strip(),
    }


def _append_bridge_ack_audit(
    data_dir: str | Path,
    *,
    bridge_id: str,
    ack_status: str,
    queue_status: str,
    reason: str,
    external_message_id: str,
    queue_item: dict[str, Any] | None,
    result: dict[str, Any],
) -> None:
    queue_id = str((queue_item or {}).get("queue_id") or bridge_id)
    payload = {
        "bridge_id": bridge_id,
        "ack_status": ack_status,
        "queue_item_found": bool(result.get("queue_item_found")),
        "queue_sync_status": str(result.get("queue_sync_status", "")),
        "queue_error": str(result.get("queue_error", "")),
        "external_message_id": external_message_id,
        "ledger_updates": result.get("ledger_updates", []),
        "task_update_count": len(result.get("task_updates", []) if isinstance(result.get("task_updates"), list) else []),
    }
    if queue_item:
        payload.update(_queue_item_audit_payload(queue_item))
    try:
        _audit(data_dir).append(
            "bridge_ack_sync",
            queue_id=queue_id,
            status=queue_status,
            reason=reason,
            payload=payload,
        )
    except Exception:
        return


def _ledger(data_dir: str | Path) -> ConversationLedgerStore:
    return ConversationLedgerStore(Path(data_dir))


def _send_result_should_stay_approved(reason: str) -> bool:
    """Keep retryable settings/precondition failures in the approved queue."""

    text = str(reason or "").strip()
    if text.startswith("weflow_backend_unavailable:"):
        return True
    if text.startswith("wechat_native_backend_unavailable:"):
        return True
    if text.startswith("bridge_worker_stale_config:"):
        return True
    return text in {
        "bridge_worker_config_unknown",
        "send_enabled_false",
        "send_driver_not_configured",
        "send_driver_missing",
        "confirm_required",
    }


def _sync_queue_item_to_ledger(
    data_dir: str | Path,
    item: dict[str, Any],
    *,
    status: str,
    reason: str,
    send_result: dict[str, Any] | None = None,
) -> bool:
    try:
        reply = _reply_from_queue_item(item)
    except Exception:
        return False
    payload = {
        "message_id": reply.message_id,
        "conversation_id": reply.conversation_id,
        "status": status,
        "reason": reason,
        **(send_result or {}),
    }
    if status == "sent" and not payload.get("sent_at"):
        payload["sent_at"] = utc_now_iso()
    return _ledger(data_dir).update_reply_send_result_for_candidate(reply, payload)


def _safe_sync_queue_item_to_ledger(
    data_dir: str | Path,
    item: dict[str, Any],
    *,
    status: str,
    reason: str,
    send_result: dict[str, Any] | None = None,
) -> str:
    """Sync to the ledger, swallowing failures so a queue transition that already
    succeeded is never masked by a ledger write error. Returns "" on success or a
    short error string (also written to the audit log) on failure."""
    audit = _audit(data_dir)
    queue_id = str(item.get("queue_id", ""))
    had_unresolved_failure = audit.has_unresolved_ledger_sync_failure(queue_id=queue_id)
    try:
        updated = _sync_queue_item_to_ledger(data_dir, item, status=status, reason=reason, send_result=send_result)
        if had_unresolved_failure and updated:
            audit.append(
                "ledger_sync_recovered",
                queue_id=queue_id,
                status=status,
                reason=reason,
                payload=send_result or {},
            )
        return ""
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        audit.append(
            "ledger_sync_failed",
            queue_id=queue_id,
            status=status,
            reason=error,
        )
        return error


def _sync_bridge_ack_to_ledgers(
    data_dir: str | Path,
    bridge_id: str,
    *,
    status: str,
    reason: str,
    external_message_id: str = "",
) -> list[str]:
    root = Path(data_dir) / "conversation_ledgers"
    if not root.exists():
        return []
    changed: list[str] = []
    store = _ledger(data_dir)
    for messages_path in root.glob("*/messages.jsonl"):
        conversation_id = _conversation_id_from_messages(messages_path) or messages_path.parent.name
        try:
            updated = store.update_bridge_send_result(
                conversation_id,
                bridge_id,
                status=status,
                reason=reason,
                external_message_id=external_message_id,
            )
        except Exception:
            # A single conversation's ledger failing to update must not abort the
            # whole ack fan-out; skip it and keep syncing the rest.
            continue
        if updated:
            changed.append(conversation_id)
    return changed


def _requeue_bridge_send_result_in_ledgers(
    data_dir: str | Path,
    old_bridge_id: str,
    new_bridge_id: str,
    *,
    reason: str,
) -> list[str]:
    root = Path(data_dir) / "conversation_ledgers"
    if not root.exists():
        return []
    changed: list[str] = []
    store = _ledger(data_dir)
    for messages_path in root.glob("*/messages.jsonl"):
        conversation_id = _conversation_id_from_messages(messages_path) or messages_path.parent.name
        try:
            updated = store.requeue_bridge_send_result(
                conversation_id,
                old_bridge_id,
                new_bridge_id,
                reason=reason,
            )
        except Exception:
            continue
        if updated:
            changed.append(conversation_id)
    return changed


def _requeue_send_tasks_for_bridge(
    data_dir: str | Path,
    old_bridge_id: str,
    new_bridge_id: str,
    *,
    reason: str,
) -> list[dict[str, Any]]:
    try:
        store = TaskStatusStore(data_dir)
        state = store.state(limit=500)
    except Exception:
        return []
    tasks = state.get("tasks") if isinstance(state.get("tasks"), list) else []
    updated: list[dict[str, Any]] = []
    for task in tasks:
        if str(task.get("kind") or "") != "send":
            continue
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        haystack = " ".join(
            str(value or "")
            for value in (
                task.get("external_id"),
                task.get("detail"),
                task.get("last_error"),
                metadata.get("bridge_id"),
                metadata.get("send_reason"),
            )
        )
        if old_bridge_id not in haystack:
            continue
        task_id = str(task.get("task_id") or "")
        if not task_id:
            continue
        next_metadata = {
            **metadata,
            "bridge_id": new_bridge_id,
            "retry_of": old_bridge_id,
            "send_reason": reason,
        }
        try:
            patched = store.update(
                task_id,
                {
                    "status": "queued",
                    "progress": 70,
                    "phase": "等待非前台桥重投",
                    "detail": reason,
                    "last_error": "",
                    "finished_at": "",
                    "external_id": new_bridge_id,
                    "metadata": next_metadata,
                },
            )
        except Exception:
            continue
        updated.append(patched)
    return updated


def _conversation_id_from_messages(messages_path: Path) -> str:
    try:
        with messages_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict):
                    return str(payload.get("conversation_id", "") or "").strip()
                return ""
    except (OSError, json.JSONDecodeError):
        return ""
    return ""


def _send_config_payload(config: Any) -> dict[str, Any]:
    send_backend = str(getattr(config, "send_backend", "dry_run") or "dry_run").strip().lower()
    return {
        "mode": config.mode,
        "send_enabled": config.send_enabled,
        "send_driver": config.send_driver,
        "send_backend": send_backend,
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
    }


def _reply_from_queue_item(item: dict[str, Any]) -> ReplyCandidate:
    raw = item.get("reply")
    if not isinstance(raw, dict):
        raise ValueError("queue item missing reply payload")
    tool_raw = raw.get("tool_result")
    tool_result = None
    if isinstance(tool_raw, dict):
        tool_result = ToolCallResult(
            call_id=str(tool_raw.get("call_id", "")),
            tool_name=str(tool_raw.get("tool_name", "")),
            status=tool_raw.get("status", "failed"),
            summary=str(tool_raw.get("summary", "")),
            output_refs=list(tool_raw.get("output_refs", [])),
            error=tool_raw.get("error"),
            completed_at=str(tool_raw.get("completed_at", "")),
            payload=dict(tool_raw.get("payload", {})),
        )
    return ReplyCandidate(
        message_id=str(raw.get("message_id", "")),
        conversation_id=str(raw.get("conversation_id", "")),
        text=str(raw.get("text", "")),
        send_mode=raw.get("send_mode", "confirm"),
        model=str(raw.get("model", "")),
        policy_hits=list(raw.get("policy_hits", [])),
        tool_result=tool_result,
        plan=str(raw.get("plan", "")),
        monitor=str(raw.get("monitor", "")),
        summary=str(raw.get("summary", "")),
        attachments=_reply_attachments(raw.get("attachments", [])),
        send_metadata=dict(raw.get("send_metadata", {})) if isinstance(raw.get("send_metadata"), dict) else {},
        created_at=str(raw.get("created_at", "")),
    )


def _reply_attachments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]

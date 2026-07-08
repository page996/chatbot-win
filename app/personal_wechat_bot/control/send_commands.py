from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.config.loader import load_config, save_config
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.domain.models import ReplyCandidate, ToolCallResult, utc_now_iso
from app.personal_wechat_bot.reply_gate.send_audit import SendAuditLog
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue
from app.personal_wechat_bot.reply_gate.send_executor import GuardedSendExecutor
from app.personal_wechat_bot.tasks.manager import TaskStatusStore
from app.personal_wechat_bot.wechat_driver.send_driver_factory import build_send_driver, probe_send_driver
from app.personal_wechat_bot.wechat_driver.bridge_send import BridgeAckStatus, is_terminal_bridge_ack_status


def set_send_controls(
    data_dir: str | Path,
    *,
    mode: str | None = None,
    enabled: bool | None = None,
    driver: str | None = None,
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
    if confirm_required is not None:
        config.send_confirm_required = confirm_required
    if max_chars is not None:
        config.send_max_chars = max_chars
    if min_interval_seconds is not None:
        config.send_min_interval_seconds = min_interval_seconds
    save_config(config)
    return _send_config_payload(config)


def list_confirm_queue(data_dir: str | Path, status: str = "pending") -> dict[str, Any]:
    queue = _queue(data_dir)
    items = queue.list_by_status(status)
    return {"status": "ok", "filter": status, "count": len(items), "items": items}


def approve_confirm_item(data_dir: str | Path, queue_id: str, *, reviewer: str, note: str = "") -> dict[str, Any]:
    item = _queue(data_dir).approve(queue_id, reviewer=reviewer, note=note)
    _audit(data_dir).append("confirm_approve", queue_id=queue_id, status=item["status"], reviewer=reviewer, note=note)
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
    _audit(data_dir).append("confirm_reject", queue_id=queue_id, status=item["status"], reviewer=reviewer, note=note)
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
    _audit(data_dir).append(
        "confirm_remove",
        queue_id=queue_id,
        status=str(item.get("status", "")),
        reviewer=reviewer,
        note=note or "removed_from_confirm_queue",
    )
    _update_send_task_from_queue_item(
        data_dir,
        item,
        action="cancel",
        patch={"progress": 100, "phase": "待审回复已移除", "detail": note or "removed_from_confirm_queue"},
    )
    return {"status": "ok", "removed": True, "item": item}


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
    final_status = result.status if result.status in {"sent", "queued_to_bridge"} else "failed"
    updated = queue.mark_send_result(queue_id, final_status, result.reason)
    _audit(data_dir).append(
        "confirm_send_attempt",
        queue_id=queue_id,
        status=final_status,
        reason=result.reason,
        payload={"conversation_id": reply.conversation_id, "message_id": reply.message_id},
    )
    # The message is already sent and the queue is already marked above. A failure
    # while syncing the ledger must not mask a successful send, so swallow it here
    # (recording it to the audit log) and still return the real send status.
    ledger_sync_error = _safe_sync_queue_item_to_ledger(
        data_dir, updated, status=final_status, reason=result.reason, send_result=result.__dict__
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
    queue_item = queue.find_by_bridge_id(bridge_id)
    queue_updated: dict[str, Any] = {}
    queue_error = ""
    queue_sync_status = "not_found"
    mapped_queue_status = "sent" if status == BridgeAckStatus.SENT else "failed"
    ledger_status = status
    sync_reason = reason or f"bridge_ack:{status}"
    if queue_item is not None:
        try:
            current_queue_status = str(queue_item.get("status", ""))
            if current_queue_status == BridgeAckStatus.SENT and mapped_queue_status != BridgeAckStatus.SENT:
                queue_updated = queue_item
                queue_sync_status = "preserved_sent"
                ledger_status = BridgeAckStatus.SENT
                sync_reason = str(queue_item.get("note", "")) or sync_reason
            else:
                queue_updated = queue.mark_send_result(
                    str(queue_item.get("queue_id", "")),
                    mapped_queue_status,
                    sync_reason,
                )
                queue_sync_status = "updated"
            _sync_queue_item_to_ledger(
                data_dir,
                queue_updated,
                status=str(queue_updated.get("status", mapped_queue_status)),
                reason=sync_reason,
                send_result={
                    "message_id": bridge_id,
                    "status": str(queue_updated.get("status", mapped_queue_status)),
                    "reason": sync_reason,
                    "sent_at": utc_now_iso() if str(queue_updated.get("status", mapped_queue_status)) == "sent" else "",
                    "external_message_id": external_message_id,
                },
            )
        except Exception as exc:
            queue_error = f"{type(exc).__name__}: {exc}"

    ledger_updates = _sync_bridge_ack_to_ledgers(
        data_dir,
        bridge_id,
        status=ledger_status,
        reason=sync_reason,
        external_message_id=external_message_id,
    )
    _finish_send_task_for_bridge(data_dir, bridge_id, status=status, reason=sync_reason)
    return {
        "status": "ok",
        "bridge_id": bridge_id,
        "queue_item_found": queue_item is not None,
        "queue_item": queue_updated,
        "queue_sync_status": queue_sync_status,
        "queue_error": queue_error,
        "ledger_updates": ledger_updates,
    }


def list_send_audit(data_dir: str | Path, *, limit: int = 20, status: str | None = None) -> dict[str, Any]:
    items = _audit(data_dir).list_recent(limit=limit, status=status)
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
    bridge_id = _bridge_id_from_reason(reason)
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
                    "metadata": {"message_id": reply.message_id, "bridge_id": bridge_id, "send_reason": reason},
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
            "phase": "发送完成" if action == "complete" else "发送失败",
            "detail": reason,
            "last_error": "" if action == "complete" else reason,
        },
    )


def _finish_send_task_for_bridge(data_dir: str | Path, bridge_id: str, *, status: str, reason: str) -> None:
    final_status = "completed" if status == BridgeAckStatus.SENT else "failed"
    try:
        updated = TaskStatusStore(data_dir).finish_external(
            bridge_id,
            {
                "status": final_status,
                "progress": 100,
                "phase": "非前台桥发送完成" if final_status == "completed" else "非前台桥发送失败",
                "detail": reason,
                "last_error": "" if final_status == "completed" else reason,
            },
        )
        if updated:
            return
    except Exception:
        return
    # If the task was created before the bridge id was known, there is nothing
    # reliable to update here; ledger/queue sync above remains authoritative.


def _send_task_id(message_id: str) -> str:
    return f"send-{_task_id_fragment(message_id)}"


def _reply_topic_id(message_id: str) -> str:
    return f"reply-{_task_id_fragment(message_id)}"


def _task_id_fragment(value: str) -> str:
    cleaned = "".join(ch for ch in str(value or "").strip() if ch.isalnum() or ch in {"-", "_", "."})
    return cleaned[:64] or "unknown"


def _bridge_id_from_reason(reason: str) -> str:
    marker = "bridge:"
    text = str(reason or "")
    index = text.rfind(marker)
    if index < 0:
        return ""
    candidate = text[index:].split()[0].strip("，,.;；")
    return candidate if candidate.startswith(marker) else ""


def _queue(data_dir: str | Path) -> ConfirmQueue:
    return ConfirmQueue(Path(data_dir) / "confirm_queue.jsonl")


def _audit(data_dir: str | Path) -> SendAuditLog:
    return SendAuditLog(Path(data_dir) / "send_audit.jsonl")


def _ledger(data_dir: str | Path) -> ConversationLedgerStore:
    return ConversationLedgerStore(Path(data_dir))


def _send_result_should_stay_approved(reason: str) -> bool:
    """Keep retryable settings/precondition failures in the approved queue."""

    text = str(reason or "").strip()
    return text in {
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
    try:
        _sync_queue_item_to_ledger(data_dir, item, status=status, reason=reason, send_result=send_result)
        return ""
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _audit(data_dir).append(
            "ledger_sync_failed",
            queue_id=str(item.get("queue_id", "")),
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
    return {
        "mode": config.mode,
        "send_enabled": config.send_enabled,
        "send_driver": config.send_driver,
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

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.personal_wechat_bot.config.loader import load_config, save_config
from app.personal_wechat_bot.domain.models import ReplyCandidate, ToolCallResult
from app.personal_wechat_bot.reply_gate.send_audit import SendAuditLog
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue
from app.personal_wechat_bot.reply_gate.send_executor import GuardedSendExecutor
from app.personal_wechat_bot.wechat_driver.send_driver_factory import build_send_driver, probe_send_driver


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
    return {"status": "ok", "item": item}


def reject_confirm_item(data_dir: str | Path, queue_id: str, *, reviewer: str, note: str = "") -> dict[str, Any]:
    item = _queue(data_dir).reject(queue_id, reviewer=reviewer, note=note)
    _audit(data_dir).append("confirm_reject", queue_id=queue_id, status=item["status"], reviewer=reviewer, note=note)
    return {"status": "ok", "item": item}


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
        return {"status": "blocked", "reason": f"queue item status is {item.get('status')}", "queue_id": queue_id}
    reply = _reply_from_queue_item(item)
    driver = driver if driver is not None else build_send_driver(config)
    result = GuardedSendExecutor(config, driver).execute_confirmed(reply)
    final_status = result.status if result.status in {"sent", "queued_to_bridge"} else "failed"
    updated = queue.mark_send_result(queue_id, final_status, result.reason)
    _audit(data_dir).append(
        "confirm_send_attempt",
        queue_id=queue_id,
        status=final_status,
        reason=result.reason,
        payload={"conversation_id": reply.conversation_id, "message_id": reply.message_id},
    )
    return {"status": final_status, "send_result": result.__dict__, "item": updated}


def list_send_audit(data_dir: str | Path, *, limit: int = 20, status: str | None = None) -> dict[str, Any]:
    items = _audit(data_dir).list_recent(limit=limit, status=status)
    return {"status": "ok", "count": len(items), "items": items}


def probe_send_controls(data_dir: str | Path, *, driver: str | None = None) -> dict[str, Any]:
    config = load_config(data_dir)
    if driver is not None:
        config.send_driver = driver
    probe = probe_send_driver(config)
    return {"status": "ok", "probe": probe}


def _queue(data_dir: str | Path) -> ConfirmQueue:
    return ConfirmQueue(Path(data_dir) / "confirm_queue.jsonl")


def _audit(data_dir: str | Path) -> SendAuditLog:
    return SendAuditLog(Path(data_dir) / "send_audit.jsonl")


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
        created_at=str(raw.get("created_at", "")),
    )

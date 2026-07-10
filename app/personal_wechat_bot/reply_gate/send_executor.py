from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.domain.models import ReplyCandidate, SendResult


@runtime_checkable
class SendingDriver(Protocol):
    def send_message(self, conversation_id: str, text: str) -> SendResult: ...


@dataclass(frozen=True)
class SendExecutionDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class FileSendResult:
    path: str
    name: str
    status: str
    reason: str
    message_id: str = ""


class GuardedSendExecutor:
    def __init__(self, config: BotConfig, driver: SendingDriver | None):
        self.config = config
        self.driver = driver

    def execute_confirmed(self, reply: ReplyCandidate) -> SendResult:
        return self._execute(reply, confirmed=True)

    def execute_auto(self, reply: ReplyCandidate) -> SendResult:
        # Auto mode still goes through all send guards and the bridge/ack chain,
        # but it must not be stopped by the human-confirmation flag.
        return self._execute(reply, confirmed=True)

    def _execute(self, reply: ReplyCandidate, *, confirmed: bool) -> SendResult:
        decision = self._can_send(reply, confirmed=False)
        if confirmed:
            decision = self._can_send(reply, confirmed=True)
        if not decision.allowed:
            return SendResult(reply.message_id, reply.conversation_id, "failed", decision.reason)
        assert self.driver is not None
        text_result: SendResult | None = None
        if reply.text.strip():
            text_result = self.driver.send_message(reply.conversation_id, reply.text)
        file_results = self._send_reply_files(reply)
        if text_result is not None and file_results:
            return _combined_text_and_file_send_result(reply, text_result, file_results)
        if text_result is not None:
            return SendResult(
                text_result.message_id or reply.message_id,
                reply.conversation_id,
                text_result.status,
                text_result.reason,
                text_result.sent_at,
            )
        if file_results:
            return _combined_file_send_result(reply, file_results)
        return SendResult(reply.message_id, reply.conversation_id, "failed", "empty_reply")

    def send_reply_files(self, reply: ReplyCandidate) -> list[FileSendResult]:
        """Public entry to deliver a reply's outgoing files (used by confirm-send)."""
        return self._send_reply_files(reply)

    def _send_reply_files(self, reply: ReplyCandidate) -> list[FileSendResult]:
        """Deliver each outgoing attachment that has a real, sendable path.

        Integrity-first: a file is sent even if its text parse was blocked, as
        long as the driver supports file sending and the path exists. Drivers
        without a ``send_file`` method (e.g. legacy/read-only) simply skip files.
        """
        results: list[FileSendResult] = []
        driver = self.driver
        if driver is None or not hasattr(driver, "send_file"):
            return results
        for attachment in _sendable_attachments(reply.attachments):
            path = str(attachment.get("path", "")).strip()
            name = str(attachment.get("name") or Path(path).name)
            if not path or not Path(path).exists():
                results.append(FileSendResult(path, name, "failed", "file_not_found"))
                continue
            outcome = driver.send_file(reply.conversation_id, path, "")  # type: ignore[attr-defined]
            results.append(
                FileSendResult(path, name, outcome.status, outcome.reason, outcome.message_id or "")
            )
        return results

    def _can_send(self, reply: ReplyCandidate, *, confirmed: bool) -> SendExecutionDecision:
        if not self.config.send_enabled:
            return SendExecutionDecision(False, "send_enabled_false")
        if self.config.send_driver in {"", "not_implemented"}:
            return SendExecutionDecision(False, "send_driver_not_configured")
        if self.driver is None:
            return SendExecutionDecision(False, "send_driver_missing")
        if self.config.send_confirm_required and not confirmed:
            return SendExecutionDecision(False, "confirm_required")
        if not reply.text.strip():
            # A reply with no text but with sendable files is still valid.
            if not _sendable_attachments(reply.attachments):
                return SendExecutionDecision(False, "empty_reply")
            if not hasattr(self.driver, "send_file"):
                return SendExecutionDecision(False, "file_send_driver_missing")
        if len(reply.text) > self.config.send_max_chars:
            return SendExecutionDecision(False, "reply_too_long")
        return SendExecutionDecision(True, "allowed")


def _sendable_attachments(attachments: list[dict] | None) -> list[dict]:
    if not attachments:
        return []
    sendable: list[dict] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        if str(item.get("path", "")).strip():
            sendable.append(item)
    return sendable


def _combined_file_send_result(reply: ReplyCandidate, results: list[FileSendResult]) -> SendResult:
    status = _combined_status([item.status for item in results])
    message_id = next((item.message_id for item in results if item.message_id), reply.message_id)
    reason = ";".join(f"{item.name}:{item.status}:{item.reason}" for item in results)
    return SendResult(
        message_id,
        reply.conversation_id,
        status,
        reason,
        details=_combined_details(text_result=None, file_results=results),
    )


def _combined_text_and_file_send_result(
    reply: ReplyCandidate,
    text_result: SendResult,
    file_results: list[FileSendResult],
) -> SendResult:
    statuses = [text_result.status, *(item.status for item in file_results)]
    status = _combined_status(statuses)
    message_id = text_result.message_id or next((item.message_id for item in file_results if item.message_id), reply.message_id)
    file_reason = ";".join(f"{item.name}:{item.status}:{item.reason}" for item in file_results)
    reason = f"text:{text_result.status}:{text_result.reason}"
    if file_reason:
        reason = f"{reason};files:{file_reason}"
    return SendResult(
        message_id,
        reply.conversation_id,
        status,
        reason,
        text_result.sent_at,
        details=_combined_details(text_result=text_result, file_results=file_results),
    )


def _combined_details(
    *,
    text_result: SendResult | None,
    file_results: list[FileSendResult],
) -> dict[str, object]:
    details: dict[str, object] = {
        "kind": "multi_part_send" if text_result is not None and file_results else "file_send",
        "bridge_ids": [],
        "files": [asdict(item) for item in file_results],
    }
    bridge_ids: list[str] = []
    if text_result is not None:
        text_payload = {
            "status": text_result.status,
            "reason": text_result.reason,
            "message_id": text_result.message_id,
            "sent_at": text_result.sent_at or "",
        }
        details["text"] = text_payload
        if text_result.message_id:
            bridge_ids.append(text_result.message_id)
    for item in file_results:
        if item.message_id:
            bridge_ids.append(item.message_id)
    details["bridge_ids"] = bridge_ids
    details["part_count"] = (1 if text_result is not None else 0) + len(file_results)
    return details


def _combined_status(statuses: list[str]) -> str:
    cleaned = [str(item or "").strip() for item in statuses if str(item or "").strip()]
    if not cleaned:
        return "failed"
    if any(item == "failed" for item in cleaned):
        return "failed"
    if any(item == "queued_to_bridge" for item in cleaned):
        return "queued_to_bridge"
    if any(item == "accepted" for item in cleaned):
        return "accepted"
    if all(item == "sent" for item in cleaned):
        return "sent"
    return cleaned[-1] or "failed"

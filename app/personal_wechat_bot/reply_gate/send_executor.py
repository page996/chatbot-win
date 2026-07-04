from __future__ import annotations

from dataclasses import dataclass
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
        decision = self._can_send(reply, confirmed=True)
        if not decision.allowed:
            return SendResult(reply.message_id, reply.conversation_id, "failed", decision.reason)
        assert self.driver is not None
        result = self.driver.send_message(reply.conversation_id, reply.text)
        self._send_reply_files(reply)
        return SendResult(result.message_id or reply.message_id, reply.conversation_id, result.status, result.reason, result.sent_at)

    def execute_auto(self, reply: ReplyCandidate) -> SendResult:
        decision = self._can_send(reply, confirmed=False)
        if not decision.allowed:
            return SendResult(reply.message_id, reply.conversation_id, "failed", decision.reason)
        assert self.driver is not None
        result = self.driver.send_message(reply.conversation_id, reply.text)
        self._send_reply_files(reply)
        return SendResult(result.message_id or reply.message_id, reply.conversation_id, result.status, result.reason, result.sent_at)

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

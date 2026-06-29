from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.domain.models import ReplyCandidate, SendResult


class SendingDriver(Protocol):
    def send_message(self, conversation_id: str, text: str) -> SendResult: ...


@dataclass(frozen=True)
class SendExecutionDecision:
    allowed: bool
    reason: str


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
        return SendResult(reply.message_id, reply.conversation_id, result.status, result.reason, result.sent_at)

    def execute_auto(self, reply: ReplyCandidate) -> SendResult:
        decision = self._can_send(reply, confirmed=False)
        if not decision.allowed:
            return SendResult(reply.message_id, reply.conversation_id, "failed", decision.reason)
        assert self.driver is not None
        result = self.driver.send_message(reply.conversation_id, reply.text)
        return SendResult(reply.message_id, reply.conversation_id, result.status, result.reason, result.sent_at)

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
            return SendExecutionDecision(False, "empty_reply")
        if len(reply.text) > self.config.send_max_chars:
            return SendExecutionDecision(False, "reply_too_long")
        return SendExecutionDecision(True, "allowed")

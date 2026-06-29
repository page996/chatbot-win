from __future__ import annotations

from app.personal_wechat_bot.domain.models import ReplyCandidate, SendMode, SendResult
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue
from app.personal_wechat_bot.reply_gate.send_executor import GuardedSendExecutor


class ReplyGate:
    def __init__(
        self,
        mode: SendMode = "dry_run",
        confirm_queue: ConfirmQueue | None = None,
        auto_executor: GuardedSendExecutor | None = None,
    ):
        self.mode = mode
        self.confirm_queue = confirm_queue
        self.auto_executor = auto_executor

    def handle(self, reply: ReplyCandidate) -> SendResult:
        if self.mode == "dry_run":
            return SendResult(reply.message_id, reply.conversation_id, "skipped", "dry_run")
        if self.mode == "confirm":
            queue_id = self.confirm_queue.enqueue(reply) if self.confirm_queue else ""
            reason = f"confirm_required:{queue_id}" if queue_id else "confirm_required"
            return SendResult(reply.message_id, reply.conversation_id, "queued_for_confirm", reason)
        if self.auto_executor is None:
            return SendResult(reply.message_id, reply.conversation_id, "failed", "auto_send_executor_missing")
        return self.auto_executor.execute_auto(reply)

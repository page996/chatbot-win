from __future__ import annotations

from app.personal_wechat_bot.domain.models import ReplyCandidate, SendMode, SendResult
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue


class ReplyGate:
    def __init__(self, mode: SendMode = "dry_run", confirm_queue: ConfirmQueue | None = None):
        self.mode = mode
        self.confirm_queue = confirm_queue

    def handle(self, reply: ReplyCandidate) -> SendResult:
        if self.mode == "dry_run":
            return SendResult(reply.message_id, reply.conversation_id, "skipped", "dry_run")
        if self.mode == "confirm":
            queue_id = self.confirm_queue.enqueue(reply) if self.confirm_queue else ""
            reason = f"confirm_required:{queue_id}" if queue_id else "confirm_required"
            return SendResult(reply.message_id, reply.conversation_id, "queued_for_confirm", reason)
        return SendResult(reply.message_id, reply.conversation_id, "failed", "real sending not implemented")

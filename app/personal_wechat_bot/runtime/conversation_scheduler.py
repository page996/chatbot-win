from __future__ import annotations

from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable

from app.personal_wechat_bot.domain.models import RawWeChatMessage
from app.personal_wechat_bot.normalizer.normalizer import MessageNormalizer


MessageHandler = Callable[[RawWeChatMessage], dict[str, Any] | None]


@dataclass(frozen=True)
class SchedulerResult:
    processed: list[dict[str, Any]]
    max_running_seen: int


class ConversationScheduler:
    def __init__(self, handler: MessageHandler, max_parallel_conversations: int = 2):
        if max_parallel_conversations < 1:
            raise ValueError("max_parallel_conversations must be at least 1")
        self.handler = handler
        self.max_parallel_conversations = max_parallel_conversations
        self.normalizer = MessageNormalizer()

    def process_batch(self, messages: list[RawWeChatMessage]) -> SchedulerResult:
        queues: dict[str, deque[RawWeChatMessage]] = defaultdict(deque)
        conversation_order: list[str] = []
        for raw in messages:
            normalized = self.normalizer.normalize(raw)
            if normalized is None:
                continue
            if normalized.conversation_id not in queues:
                conversation_order.append(normalized.conversation_id)
            queues[normalized.conversation_id].append(raw)

        pending_conversations = deque(conversation_order)
        running: dict[Future[dict[str, Any] | None], tuple[str, RawWeChatMessage]] = {}
        processed: list[dict[str, Any]] = []
        max_running_seen = 0

        with ThreadPoolExecutor(max_workers=self.max_parallel_conversations) as executor:
            while pending_conversations or running:
                while pending_conversations and len(running) < self.max_parallel_conversations:
                    conversation_id = pending_conversations.popleft()
                    queue = queues[conversation_id]
                    if not queue:
                        continue
                    raw = queue.popleft()
                    future = executor.submit(self.handler, raw)
                    running[future] = (conversation_id, raw)
                    max_running_seen = max(max_running_seen, len(running))

                if not running:
                    continue

                done, _ = wait(running.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    conversation_id, raw = running.pop(future)
                    try:
                        item = future.result()
                    except Exception as exc:
                        item = {
                            "error": {"type": type(exc).__name__, "message": str(exc)},
                            "raw_id": raw.raw_id,
                            "chat_title": raw.chat_title,
                            "conversation_id": conversation_id,
                        }
                    if item is not None:
                        processed.append(item)
                    if queues[conversation_id]:
                        pending_conversations.append(conversation_id)

        return SchedulerResult(processed=processed, max_running_seen=max_running_seen)

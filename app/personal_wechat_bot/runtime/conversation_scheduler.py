from __future__ import annotations

from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from typing import Any, Callable

from app.personal_wechat_bot.domain.models import NormalizedMessage, RawWeChatMessage
from app.personal_wechat_bot.conversation.session_store import is_reset_command
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
        grouped: dict[str, list[tuple[RawWeChatMessage, NormalizedMessage]]] = defaultdict(list)
        conversation_order: list[str] = []
        for raw in messages:
            normalized = self.normalizer.normalize(raw)
            if normalized is None:
                continue
            if normalized.conversation_id not in grouped:
                conversation_order.append(normalized.conversation_id)
            grouped[normalized.conversation_id].append((raw, normalized))

        queues: dict[str, deque[RawWeChatMessage]] = defaultdict(deque)
        for conversation_id in conversation_order:
            for raw in _defer_non_latest_live_messages(grouped[conversation_id]):
                queues[conversation_id].append(raw)

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


def _defer_non_latest_live_messages(
    entries: list[tuple[RawWeChatMessage, NormalizedMessage]],
) -> list[RawWeChatMessage]:
    last_live_index = -1
    for index, (_, message) in enumerate(entries):
        if _is_live_timeline_message(message):
            last_live_index = index

    if last_live_index < 0:
        return [raw for raw, _ in entries]

    latest_live_message = entries[last_live_index][1]
    latest_reply_anchor = latest_live_message if _is_reply_candidate(latest_live_message) else None
    anchor_raw_id = entries[last_live_index][0].raw_id

    deferred: list[RawWeChatMessage] = []
    for index, (raw, message) in enumerate(entries):
        if (
            index != last_live_index
            and _is_reply_candidate(message)
            and (latest_reply_anchor is not None or index < last_live_index)
        ):
            deferred.append(_defer_reply(raw, anchor_raw_id=anchor_raw_id))
        else:
            deferred.append(raw)
    return deferred


def _is_live_timeline_message(message: NormalizedMessage) -> bool:
    if message.metadata.get("context_only"):
        return False
    if str(message.metadata.get("event_type") or "").strip() == "recall":
        return False
    return True


def _is_reply_candidate(message: NormalizedMessage) -> bool:
    return (
        _is_live_timeline_message(message)
        and not message.is_self
        and not is_reset_command(message.text, metadata=message.metadata)
    )


def _defer_reply(raw: RawWeChatMessage, *, anchor_raw_id: str) -> RawWeChatMessage:
    metadata = dict(raw.driver_meta)
    metadata.update(
        {
            "context_only": True,
            "deferred_reply": True,
            "deferred_reply_reason": "batched_conversation_has_later_live_message",
            "deferred_reply_anchor_raw_id": anchor_raw_id,
        }
    )
    return replace(raw, driver_meta=metadata)

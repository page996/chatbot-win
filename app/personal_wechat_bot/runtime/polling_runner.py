from __future__ import annotations

import time
from typing import Any

from app.personal_wechat_bot.bootstrap import BotRuntime
from app.personal_wechat_bot.processor.message_processor import MessageProcessor
from app.personal_wechat_bot.runtime.conversation_scheduler import ConversationScheduler
from app.personal_wechat_bot.runtime.resource_scheduler import ResourceSchedule
from app.personal_wechat_bot.wechat_driver.base import WeChatDriver


class PollingRunner:
    def __init__(
        self,
        runtime: BotRuntime,
        driver: WeChatDriver,
        poll_interval_seconds: float = 1.0,
        *,
        workload: str = "interactive",
    ):
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        self.runtime = runtime
        self.driver = driver
        self.poll_interval_seconds = poll_interval_seconds
        self.workload = _normalize_workload(workload)
        self.processor = MessageProcessor(runtime)
        schedule = _runtime_schedule(runtime, self.workload)
        self.scheduler = ConversationScheduler(
            self.processor.process,
            max_parallel_conversations=schedule.max_parallel_conversations,
        )

    def run_once(self) -> dict[str, Any]:
        if not self.driver.health_check():
            return {"status": "driver_unhealthy", "processed": []}

        setattr(self.runtime, "active_driver", self.driver)
        messages = self.driver.read_new_messages()
        workload = _batch_workload(messages, self.workload)
        schedule = _runtime_schedule(self.runtime, workload)
        self.scheduler.max_parallel_conversations = schedule.max_parallel_conversations
        scheduler_result = self.scheduler.process_batch(messages)
        return {
            "status": "ok",
            "processed": scheduler_result.processed,
            "max_running_seen": scheduler_result.max_running_seen,
            "resource_schedule": schedule.to_dict(),
        }

    def run_forever(self, max_loops: int | None = None) -> dict[str, Any]:
        loops = 0
        processed_count = 0
        processed: list[dict[str, Any]] = []
        while max_loops is None or loops < max_loops:
            result = self.run_once()
            loops += 1
            processed_count += len(result["processed"])
            processed.extend(result["processed"])
            if max_loops is None or loops < max_loops:
                time.sleep(self.poll_interval_seconds)
        return {"status": "stopped", "loops": loops, "processed_count": processed_count, "processed": processed}


def _runtime_schedule(runtime: BotRuntime, workload: str) -> ResourceSchedule:
    scheduler = getattr(runtime, "resource_scheduler", None)
    if scheduler is not None:
        schedule = getattr(scheduler, "conversation_parallelism", None)
        if callable(schedule):
            try:
                return schedule(workload)
            except Exception:
                pass
    return ResourceSchedule(
        workload=_normalize_workload(workload),
        max_parallel_conversations=_fallback_max_parallel_conversations(runtime),
        llm_total=_fallback_max_parallel_conversations(runtime),
        llm_interactive=_fallback_max_parallel_conversations(runtime),
        llm_background=1,
        media_cpu=2,
        file_io=1,
        gpu_media=1,
        reason="runtime without ResourceScheduler",
    )


def _fallback_max_parallel_conversations(runtime: BotRuntime) -> int:
    key_pool = getattr(runtime, "key_pool", None)
    concurrency_limit = getattr(key_pool, "concurrency_limit", None)
    if callable(concurrency_limit):
        try:
            return max(1, int(concurrency_limit()))
        except Exception:
            pass
    return max(1, int(runtime.model_router.chat_provider().config.max_concurrency or 1))


def _batch_workload(messages: list[Any], configured: str) -> str:
    configured = _normalize_workload(configured)
    if configured == "background":
        return "background"
    if messages and all(_is_context_only_message(message) for message in messages):
        return "background"
    return "interactive"


def _is_context_only_message(message: Any) -> bool:
    meta = getattr(message, "driver_meta", None)
    return isinstance(meta, dict) and bool(meta.get("context_only"))


def _normalize_workload(value: str) -> str:
    text = str(value or "").strip().lower()
    return "background" if text in {"background", "context_only", "backfill", "history"} else "interactive"

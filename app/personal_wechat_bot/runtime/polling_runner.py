from __future__ import annotations

import time
from typing import Any

from app.personal_wechat_bot.bootstrap import BotRuntime
from app.personal_wechat_bot.processor.message_processor import MessageProcessor
from app.personal_wechat_bot.runtime.conversation_scheduler import ConversationScheduler
from app.personal_wechat_bot.wechat_driver.base import WeChatDriver


class PollingRunner:
    def __init__(self, runtime: BotRuntime, driver: WeChatDriver, poll_interval_seconds: float = 1.0):
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        self.runtime = runtime
        self.driver = driver
        self.poll_interval_seconds = poll_interval_seconds
        self.processor = MessageProcessor(runtime)
        self.scheduler = ConversationScheduler(
            self.processor.process,
            max_parallel_conversations=runtime.model_router.chat_provider().config.max_concurrency,
        )

    def run_once(self) -> dict[str, Any]:
        if not self.driver.health_check():
            return {"status": "driver_unhealthy", "processed": []}

        setattr(self.runtime, "active_driver", self.driver)
        scheduler_result = self.scheduler.process_batch(self.driver.read_new_messages())
        return {
            "status": "ok",
            "processed": scheduler_result.processed,
            "max_running_seen": scheduler_result.max_running_seen,
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

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.processor.message_processor import MessageProcessor
from app.personal_wechat_bot.wechat_driver.fake import FakeWeChatDriver


class ReplayRunner:
    def __init__(self, config: BotConfig):
        self.config = config
        self.runtime = build_runtime(config)
        self.processor = MessageProcessor(self.runtime)

    def run(self, fixture_path: str | Path) -> dict[str, Any]:
        driver = FakeWeChatDriver(fixture_path)
        if not driver.health_check():
            raise FileNotFoundError(fixture_path)

        processed = []
        for raw in driver.read_new_messages():
            item = self.processor.process(raw)
            if item is None:
                continue
            processed.append(item)
        return {"fixture": str(fixture_path), "processed": processed}

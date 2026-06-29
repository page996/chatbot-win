from __future__ import annotations

import json
from pathlib import Path

from app.personal_wechat_bot.domain.models import RawWeChatMessage, SendResult


class FakeWeChatDriver:
    def __init__(self, fixture_path: str | Path):
        self.fixture_path = Path(fixture_path)
        self._read = False

    def health_check(self) -> bool:
        return self.fixture_path.exists()

    def read_new_messages(self) -> list[RawWeChatMessage]:
        if self._read:
            return []
        self._read = True
        with self.fixture_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        return [RawWeChatMessage(**item) for item in payload.get("messages", [])]

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        return SendResult(
            message_id="fake-send",
            conversation_id=conversation_id,
            status="failed",
            reason="FakeWeChatDriver never sends messages",
        )

from __future__ import annotations

from typing import Protocol

from app.personal_wechat_bot.domain.models import RawWeChatMessage, SendResult


class WeChatDriver(Protocol):
    def health_check(self) -> bool: ...

    def read_new_messages(self) -> list[RawWeChatMessage]: ...

    def send_message(self, conversation_id: str, text: str) -> SendResult: ...

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.personal_wechat_bot.domain.models import ToolCallRequest, ToolCallResult


@dataclass(frozen=True)
class ToolManifest:
    name: str
    description: str
    version: str = "0.1.0"
    supports_async: bool = False


class Tool(Protocol):
    manifest: ToolManifest

    def run(self, request: ToolCallRequest) -> ToolCallResult: ...

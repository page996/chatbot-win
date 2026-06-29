from __future__ import annotations

from app.personal_wechat_bot.domain.models import ToolCallRequest, ToolCallResult
from app.personal_wechat_bot.logging.event_log import EventLogger
from app.personal_wechat_bot.tools.registry import ToolRegistry


class ToolRuntime:
    def __init__(self, registry: ToolRegistry, logger: EventLogger):
        self.registry = registry
        self.logger = logger

    def execute(self, request: ToolCallRequest) -> ToolCallResult:
        self.logger.log("tool.call", request, conversation_id=request.conversation_id)
        if not self.registry.has(request.tool_name):
            result = ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="failed",
                summary=f"工具未注册：{request.tool_name}",
                error="tool_not_registered",
            )
            self.logger.log("tool.result", result, conversation_id=request.conversation_id)
            return result
        try:
            result = self.registry.get(request.tool_name).run(request)
        except Exception as exc:  # developer-mode details stay local logs.
            result = ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                status="failed",
                summary="工具执行失败",
                error=f"{type(exc).__name__}: {exc}",
            )
        self.logger.log("tool.result", result, conversation_id=request.conversation_id)
        return result

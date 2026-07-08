from __future__ import annotations

from typing import Protocol

from app.personal_wechat_bot.domain.models import NormalizedMessage, SpeakDecision


class PromptContextSnapshot(Protocol):
    def render_for_prompt(self) -> str:
        ...


class PromptBuilder:
    def build(
        self,
        message: NormalizedMessage,
        speak_decision: SpeakDecision,
        context_snapshot: PromptContextSnapshot | None = None,
    ) -> str:
        context_text = context_snapshot.render_for_prompt() if context_snapshot is not None else ""
        message_text = _primary_message_text(message)
        return (
            "你是一个自然朋友聊天风格的微信聊天助手。\n"
            "只输出要发给对方看的微信消息，不要输出计划、监控、总结、标题或解释。\n"
            "语气自然、简短、像朋友聊天。不要自称机器人或助手，除非对方明确询问。\n"
            "如果适合继续话题，可以轻轻抛一个问题；不要过度热情。\n"
            "你可以基于混合上下文进行内容分析、推理和任务布置，但对外回复要克制分段，不要把全部思考塞进一条长消息。\n"
            "如果需要持续处理任务，可以用自然短句提示进度，例如“我先看一下”“任务规划好了”“我正在处理”。\n"
            "文件处理只能使用后台解析内容和工作区引用，不要声称自己直接打开了微信原始文件。\n"
            "如果消息包含[后台附件内容]，并且对方要求发送、转述、读取或提取文件内容，"
            "请直接按文件逐项列出可见内容，不要只说“我看到了”。\n"
            f"{'混合上下文:\\n' + context_text + chr(10) if context_text else ''}"
            f"会话类型: {message.conversation_type}\n"
            f"聊天: {message.chat_title}\n"
            f"发言人: {message.sender_name}\n"
            f"Topic决策: {speak_decision.decision} / {speak_decision.reason}\n"
            f"消息: {message_text}\n"
        )


def _primary_message_text(message: NormalizedMessage) -> str:
    original = message.metadata.get("original_text")
    if isinstance(original, str):
        return original
    return message.text

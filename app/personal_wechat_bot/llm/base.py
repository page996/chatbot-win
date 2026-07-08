from __future__ import annotations

import inspect
from typing import Protocol

from app.personal_wechat_bot.domain.models import NormalizedMessage, SpeakDecision


class LLMClient(Protocol):
    model: str

    def generate_reply(self, prompt: str, *, workload: str = "interactive") -> str: ...

    def classify_topic(
        self,
        recent_messages: list[NormalizedMessage],
        topics: list[str],
        avoid_topics: list[str],
    ) -> SpeakDecision: ...


def generate_reply_with_workload(llm: object, prompt: str, *, workload: str = "interactive") -> str:
    """Call generate_reply with workload when the client supports it.

    Older tests and fake clients only accept ``generate_reply(prompt)``. Keeping
    this small compatibility shim lets background file/memory analysis opt into
    the LLM scheduler without forcing every local stub to grow a keyword arg.
    """

    generate = getattr(llm, "generate_reply")
    try:
        signature = inspect.signature(generate)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        parameters = signature.parameters.values()
        supports_workload = "workload" in signature.parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters
        )
        if supports_workload:
            return generate(prompt, workload=workload)
    return generate(prompt)

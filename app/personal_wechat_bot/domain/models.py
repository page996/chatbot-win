from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


ConversationType = Literal["private", "group"]
RouteAction = Literal["process", "ignore", "duplicate", "blocked"]
SpeakAction = Literal["speak", "silent", "wait"]
SendMode = Literal["dry_run", "confirm", "auto"]
SendStatus = Literal["skipped", "queued_for_confirm", "queued_to_bridge", "sent", "failed"]
ToolStatus = Literal["queued", "running", "completed", "failed", "blocked"]


@dataclass(frozen=True)
class RawWeChatMessage:
    raw_id: str
    chat_title: str
    sender_name: str
    text: str
    is_self: bool = False
    is_group: bool = False
    sender_wechat_id: str | None = None
    observed_at: str = field(default_factory=utc_now_iso)
    driver_meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedMessage:
    message_id: str
    conversation_id: str
    conversation_type: ConversationType
    chat_title: str
    sender_name: str
    text: str
    is_self: bool
    received_at: str
    sender_wechat_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteDecision:
    message_id: str
    conversation_id: str
    action: RouteAction
    reason: str
    requires_topic_decision: bool = False


@dataclass(frozen=True)
class SpeakDecision:
    conversation_id: str
    decision: SpeakAction
    reason: str
    topic: str | None = None
    confidence: float = 0.0
    style_context: str = ""
    daily_trace_context: str = ""


@dataclass(frozen=True)
class ToolCallRequest:
    tool_name: str
    call_id: str
    conversation_id: str
    requested_by: str
    arguments: dict[str, Any]
    created_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class ToolCallResult:
    call_id: str
    tool_name: str
    status: ToolStatus
    summary: str
    output_refs: list[str] = field(default_factory=list)
    error: str | None = None
    completed_at: str = field(default_factory=utc_now_iso)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplyCandidate:
    message_id: str
    conversation_id: str
    text: str
    send_mode: SendMode
    model: str
    policy_hits: list[str] = field(default_factory=list)
    tool_result: ToolCallResult | None = None
    plan: str = ""
    monitor: str = ""
    summary: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    send_metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class SendResult:
    message_id: str
    conversation_id: str
    status: SendStatus
    reason: str
    sent_at: str | None = None


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    source_domain: str
    language: str
    snippet: str
    translated_title: str
    translated_summary: str
    relevance_score: float
    spam_score: float
    model_relevance: dict[str, Any]

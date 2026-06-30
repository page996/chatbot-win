from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.conversation.session_store import DEFAULT_SESSION_ID, ConversationSessionStore
from app.personal_wechat_bot.domain.models import RawWeChatMessage, SendResult, utc_now_iso
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.normalizer.normalizer import conversation_id_for
from app.personal_wechat_bot.vision.ocr import RapidOcrSubprocessEngine
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import BackendAttachmentParser
from app.personal_wechat_bot.workspace.attachment_pipeline import AttachmentPipeline, IncomingAttachment
from app.personal_wechat_bot.workspace.file_workspace import FileWorkspace


@dataclass(frozen=True)
class BackendAttachment:
    path: str
    original_name: str = ""
    kind: str = "file"


@dataclass(frozen=True)
class BackendMessageEvent:
    raw_id: str
    chat_title: str
    sender_name: str
    text: str
    is_group: bool = False
    sender_wechat_id: str | None = None
    observed_at: str = ""
    attachments: tuple[BackendAttachment, ...] = ()
    quote: dict[str, Any] | None = None


class BackendEventJsonlDriver:
    """Read local backend message events without touching the WeChat UI."""

    def __init__(
        self,
        event_path: str | Path,
        file_index: FileIndex,
        allowed_input_roots: list[Path],
        allowed_extensions: list[str],
        max_input_bytes: int,
        attachment_parser: BackendAttachmentParser | None = None,
        file_workspace: FileWorkspace | None = None,
        attachment_pipeline: AttachmentPipeline | None = None,
        session_store: ConversationSessionStore | None = None,
        context_store: ConversationSessionStore | None = None,
    ):
        self.event_path = Path(event_path)
        self.file_index = file_index
        self.allowed_input_roots = allowed_input_roots
        self.allowed_extensions = allowed_extensions
        self.max_input_bytes = max_input_bytes
        self.attachment_parser = attachment_parser or BackendAttachmentParser(RapidOcrSubprocessEngine())
        self.file_workspace = file_workspace or FileWorkspace(self.event_path.parent / "file_workspace")
        self.attachment_pipeline = attachment_pipeline or AttachmentPipeline(
            file_index=self.file_index,
            file_workspace=self.file_workspace,
            attachment_parser=self.attachment_parser,
            allowed_input_roots=self.allowed_input_roots,
            allowed_extensions=self.allowed_extensions,
            max_input_bytes=self.max_input_bytes,
            embedded_media_ocr=self.attachment_parser.ocr_engine,
        )
        self.session_store = session_store or context_store
        self._seen_raw_ids: set[str] = set()

    def health_check(self) -> bool:
        return self.event_path.exists() and self.event_path.is_file()

    def read_new_messages(self) -> list[RawWeChatMessage]:
        if not self.event_path.exists():
            return []

        messages: list[RawWeChatMessage] = []
        for line_no, line in enumerate(self.event_path.read_text(encoding="utf-8").splitlines(), start=1):
            event = _parse_event_line(line, line_no=line_no)
            if event is None or event.raw_id in self._seen_raw_ids:
                continue
            self._seen_raw_ids.add(event.raw_id)
            raw = self._to_raw_message(event, line_no=line_no)
            if raw is not None:
                messages.append(raw)
        return messages

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        return SendResult(
            message_id="backend-event-send",
            conversation_id=conversation_id,
            status="failed",
            reason="backend_event_driver_never_sends",
        )

    def _to_raw_message(self, event: BackendMessageEvent, line_no: int) -> RawWeChatMessage | None:
        conversation_type = "group" if event.is_group else "private"
        conversation_id = conversation_id_for(conversation_type, event.chat_title)
        session_id = (
            self.session_store.current_session_id(conversation_id)
            if self.session_store is not None
            else DEFAULT_SESSION_ID
        )
        attachments = [_attachment_pending(item) for item in event.attachments]
        text = _compose_pending_message_text(event.text, attachments)
        if not text.strip():
            return None
        return RawWeChatMessage(
            raw_id=event.raw_id,
            chat_title=event.chat_title,
            sender_name=event.sender_name,
            sender_wechat_id=event.sender_wechat_id,
            text=text,
            is_group=event.is_group,
            observed_at=event.observed_at or utc_now_iso(),
            driver_meta={
                "source": "backend_events_jsonl",
                "event_path": str(self.event_path),
                "line_no": line_no,
                "conversation_id_hint": conversation_id,
                "session_id": session_id,
                "original_text": event.text,
                "backend_attachments_pending": True,
                "attachments": attachments,
                "quote": event.quote or {},
            },
        )

    def enrich_message_attachments(
        self,
        raw: RawWeChatMessage,
        *,
        conversation_id: str,
        session_id: str,
    ) -> RawWeChatMessage:
        if raw.driver_meta.get("source") != "backend_events_jsonl":
            return raw
        pending = raw.driver_meta.get("backend_attachments_pending")
        if not pending:
            return raw
        attachments_meta = raw.driver_meta.get("attachments", [])
        if not isinstance(attachments_meta, list):
            return raw
        indexed: list[dict[str, Any]] = []
        for item in attachments_meta:
            if not isinstance(item, dict):
                continue
            if item.get("status") != "pending":
                indexed.append(item)
                continue
            attachment = BackendAttachment(
                path=str(item.get("path", "")),
                original_name=str(item.get("name", "")),
                kind=str(item.get("kind", "file") or "file"),
            )
            indexed.append(
                self.attachment_pipeline.process(
                    IncomingAttachment(
                        path=attachment.path,
                        original_name=attachment.original_name,
                        kind=attachment.kind,
                    ),
                    conversation_id=conversation_id,
                    session_id=session_id,
                )
            )
        allowed = [item for item in indexed if item.get("status") == "indexed"]
        blocked = [item for item in indexed if item.get("status") == "blocked"]
        text = _compose_message_text(str(raw.driver_meta.get("original_text", raw.text)), allowed, blocked)
        meta = dict(raw.driver_meta)
        meta["attachments"] = indexed
        meta["backend_attachments_pending"] = False
        return RawWeChatMessage(
            raw_id=raw.raw_id,
            chat_title=raw.chat_title,
            sender_name=raw.sender_name,
            text=text,
            is_self=raw.is_self,
            is_group=raw.is_group,
            sender_wechat_id=raw.sender_wechat_id,
            observed_at=raw.observed_at,
            driver_meta=meta,
        )


def append_backend_event(
    event_path: str | Path,
    *,
    chat_title: str,
    sender_name: str,
    text: str = "",
    sender_wechat_id: str = "",
    is_group: bool = False,
    attachments: list[str] | None = None,
    observed_at: str = "",
    quote: dict[str, Any] | None = None,
) -> str:
    payload = {
        "chat_title": chat_title,
        "sender_name": sender_name,
        "sender_wechat_id": sender_wechat_id,
        "text": text,
        "is_group": is_group,
        "observed_at": observed_at or utc_now_iso(),
        "attachments": [{"path": item} for item in attachments or []],
    }
    if quote:
        payload["quote"] = quote
    raw_id = _event_raw_id(payload)
    payload["raw_id"] = raw_id
    path = Path(event_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return raw_id


def _parse_event_line(line: str, line_no: int) -> BackendMessageEvent | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    chat_title = str(payload.get("chat_title", "")).strip()
    sender_name = str(payload.get("sender_name", "")).strip()
    if not chat_title or not sender_name:
        return None
    text = str(payload.get("text", "")).strip()
    attachments = _parse_attachments(payload.get("attachments", []))
    quote = _parse_quote(payload.get("quote"))
    raw_id = str(payload.get("raw_id", "")).strip() or _event_raw_id({**payload, "_line_no": line_no})
    return BackendMessageEvent(
        raw_id=raw_id,
        chat_title=chat_title,
        sender_name=sender_name,
        sender_wechat_id=str(payload.get("sender_wechat_id", "")).strip() or None,
        text=text,
        is_group=bool(payload.get("is_group", False)),
        observed_at=str(payload.get("observed_at", "")).strip(),
        attachments=attachments,
        quote=quote,
    )


def _parse_attachment(value: Any) -> BackendAttachment | None:
    if isinstance(value, str):
        path = value.strip()
        return BackendAttachment(path=path) if path else None
    if not isinstance(value, dict):
        return None
    path = str(value.get("path", "")).strip()
    if not path:
        return None
    return BackendAttachment(
        path=path,
        original_name=str(value.get("original_name", "")).strip(),
        kind=str(value.get("kind", "file")).strip() or "file",
    )


def _parse_attachments(values: Any) -> tuple[BackendAttachment, ...]:
    if not isinstance(values, list):
        return ()
    parsed: list[BackendAttachment] = []
    for item in values:
        attachment = _parse_attachment(item)
        if attachment is not None:
            parsed.append(attachment)
    return tuple(parsed)


def _parse_quote(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        text = value.strip()
        return {"text": text} if text else None
    if not isinstance(value, dict):
        return None
    quote = {
        "message_id": str(value.get("message_id") or value.get("quoted_message_id") or "").strip(),
        "sender_name": str(value.get("sender_name") or value.get("quoted_sender_name") or "").strip(),
        "text": str(value.get("text") or value.get("quoted_text") or value.get("content") or "").strip(),
        "received_at": str(value.get("received_at") or value.get("quoted_received_at") or "").strip(),
        "source": str(value.get("source") or "backend_events_jsonl").strip(),
    }
    cleaned = {key: item for key, item in quote.items() if item}
    return cleaned or None


def _compose_message_text(
    text: str,
    allowed_attachments: list[dict[str, Any]],
    blocked_attachments: list[dict[str, Any]],
) -> str:
    lines = [text.strip()] if text.strip() else []
    for item in allowed_attachments:
        workspace = item.get("workspace") or {}
        workspace_ref = str(workspace.get("workspace_dir", "")).strip()
        workspace_note = f" workspace_ref={workspace_ref}" if workspace_ref else ""
        lines.append(f"[后台附件] {item['name']} file_id={item['file_id']} kind={item['kind']}{workspace_note}")
        parsed = item.get("parse") or {}
        summary = str(parsed.get("summary", "")).strip()
        if summary:
            lines.append(f"[后台附件解析] {item['name']} status={parsed.get('status', '')} summary={summary}")
        parsed_text = str(parsed.get("text", "")).strip()
        if parsed_text:
            lines.append(f"[后台附件内容]\n{parsed_text}")
    for item in blocked_attachments:
        lines.append(f"[后台附件已阻止] {item['name']} kind={item['kind']}")
    return "\n".join(lines).strip()


def _attachment_pending(attachment: BackendAttachment) -> dict[str, Any]:
    return {
        "status": "pending",
        "path": attachment.path,
        "name": attachment.original_name or Path(attachment.path).name,
        "kind": attachment.kind,
    }


def _compose_pending_message_text(text: str, attachments: list[dict[str, Any]]) -> str:
    lines = [text.strip()] if text.strip() else []
    for item in attachments:
        lines.append(f"[后台附件待处理] {item.get('name', '')} kind={item.get('kind', 'file')}")
    return "\n".join(lines).strip()


def _event_raw_id(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:24]

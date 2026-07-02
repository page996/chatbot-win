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
from app.personal_wechat_bot.wechat_driver.jsonl_bus import append_jsonl
from app.personal_wechat_bot.wechat_driver.voice_cache_resolver import WeChatVoiceCacheResolver
from app.personal_wechat_bot.wechat_driver.voice_transcription import WeChatVoiceTranscriptionBridge, result_payload
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
    event_type: str = "message"
    is_self: bool = False
    is_group: bool = False
    sender_wechat_id: str | None = None
    observed_at: str = ""
    attachments: tuple[BackendAttachment, ...] = ()
    voice: dict[str, Any] | None = None
    quote: dict[str, Any] | None = None
    recall: dict[str, Any] | None = None
    source_payload: dict[str, Any] | None = None
    history: tuple["BackendMessageEvent", ...] = ()


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
        voice_transcription_bridge: WeChatVoiceTranscriptionBridge | None = None,
        voice_cache_resolver: WeChatVoiceCacheResolver | None = None,
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
            embedded_media_asr=self.attachment_parser.asr_engine,
        )
        self.voice_transcription_bridge = voice_transcription_bridge or WeChatVoiceTranscriptionBridge(self.event_path.parent)
        self.voice_cache_resolver = voice_cache_resolver
        self.session_store = session_store or context_store
        self._seen_event_ids: set[str] = set()
        self._seen_message_raw_ids: set[str] = set()

    def health_check(self) -> bool:
        return self.event_path.exists() and self.event_path.is_file()

    def read_new_messages(self) -> list[RawWeChatMessage]:
        if not self.event_path.exists():
            return []

        messages: list[RawWeChatMessage] = []
        for line_no, line in enumerate(self.event_path.read_text(encoding="utf-8").splitlines(), start=1):
            event = _parse_event_line(line, line_no=line_no)
            if event is None or event.raw_id in self._seen_event_ids:
                continue
            self._seen_event_ids.add(event.raw_id)
            for raw in self._event_messages(event, line_no=line_no):
                if raw.raw_id in self._seen_message_raw_ids:
                    continue
                self._seen_message_raw_ids.add(raw.raw_id)
                messages.append(raw)
        return messages

    def _event_messages(self, event: BackendMessageEvent, line_no: int) -> list[RawWeChatMessage]:
        messages: list[RawWeChatMessage] = []
        for index, history_event in enumerate(event.history):
            raw = self._to_raw_message(history_event, line_no=line_no, history_index=index, context_only=True)
            if raw is not None:
                messages.append(raw)
        current = self._to_raw_message(event, line_no=line_no)
        if current is not None:
            messages.append(current)
        return messages

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        return SendResult(
            message_id="backend-event-send",
            conversation_id=conversation_id,
            status="failed",
            reason="backend_event_driver_never_sends",
        )

    def _to_raw_message(
        self,
        event: BackendMessageEvent,
        line_no: int,
        *,
        history_index: int | None = None,
        context_only: bool = False,
    ) -> RawWeChatMessage | None:
        conversation_type = "group" if event.is_group else "private"
        meta_source = event.source_payload or {}
        if not isinstance(meta_source, dict):
            meta_source = {}
        conversation_key = _conversation_key(meta_source, event.chat_title)
        conversation_id = conversation_id_for(conversation_type, conversation_key)
        session_id = (
            self.session_store.current_session_id(conversation_id)
            if self.session_store is not None
            else DEFAULT_SESSION_ID
        )
        attachments = [_attachment_pending(item) for item in event.attachments]
        voice = _normalize_voice(event.voice, allow_pending=True)
        voice_pending = _voice_needs_transcription(voice)
        text = _compose_pending_message_text(_message_text_with_voice(event.text, voice), attachments, voice)
        if not text.strip():
            if event.event_type == "recall":
                text = ""
            else:
                return None
        allow_empty = event.event_type == "recall"
        context_only = context_only or event.event_type == "recall" or _truthy(meta_source.get("context_only") or meta_source.get("contextOnly"))
        source_name = str(meta_source.get("source") or meta_source.get("adapter") or "backend_events_jsonl")
        hook_meta = meta_source.get("hook") if isinstance(meta_source.get("hook"), dict) else {}
        source_line_no = _safe_int(meta_source.get("source_line_no") or meta_source.get("sourceLineNo"), 0)
        source_offset = _safe_int(meta_source.get("source_offset") or meta_source.get("sourceOffset"), 0)
        batch_index = _safe_int(meta_source.get("batch_index") or meta_source.get("batchIndex"), 0)
        batch_count = _safe_int(meta_source.get("batch_count") or meta_source.get("batchCount"), 1)
        import_sequence = _safe_int(meta_source.get("import_sequence") or meta_source.get("importSequence"), 0)
        recall = event.recall or {}
        if event.event_type == "recall" and not recall:
            recall = {
                "target_raw_id": str(meta_source.get("target_raw_id") or meta_source.get("targetRawId") or ""),
                "target_message_id": str(meta_source.get("target_message_id") or meta_source.get("targetMessageId") or ""),
                "reason": str(meta_source.get("reason") or "wechat_recall"),
            }
            recall = {key: value for key, value in recall.items() if value}
        if event.event_type == "recall" and not recall:
            return None
        return RawWeChatMessage(
            raw_id=event.raw_id if history_index is None else f"{event.raw_id}:history:{history_index}",
            chat_title=event.chat_title,
            sender_name=event.sender_name,
            sender_wechat_id=event.sender_wechat_id,
            text=text,
            is_self=event.is_self,
            is_group=event.is_group,
            observed_at=event.observed_at or utc_now_iso(),
            driver_meta={
                "source": "backend_events_jsonl",
                "backend_event_source": source_name,
                "event_type": event.event_type,
                "conversation_key": conversation_key,
                "event_path": str(self.event_path),
                "line_no": line_no,
                "source_path": str(meta_source.get("source_path") or meta_source.get("sourcePath") or ""),
                "source_line_no": source_line_no,
                "source_offset": source_offset,
                "source_batch_index": batch_index,
                "source_batch_count": batch_count,
                "import_sequence": import_sequence,
                "history_index": history_index,
                "conversation_id_hint": conversation_id,
                "session_id": session_id,
                "original_text": event.text,
                "voice": voice,
                "backend_attachments_pending": bool(attachments),
                "backend_voice_pending": voice_pending,
                "backend_media_pending": bool(attachments) or voice_pending,
                "attachments": attachments,
                "quote": event.quote or {},
                "recall": recall,
                "hook": hook_meta,
                "ordering": _ordering_metadata(
                    event,
                    meta_source,
                    line_no=line_no,
                    source_line_no=source_line_no,
                    source_offset=source_offset,
                    batch_index=batch_index,
                    import_sequence=import_sequence,
                ),
                "source_payload": meta_source,
                "context_only": context_only,
                "allow_empty_message": allow_empty,
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
        pending = raw.driver_meta.get("backend_media_pending") or raw.driver_meta.get("backend_attachments_pending")
        if not pending:
            return raw
        meta = dict(raw.driver_meta)
        voice = _normalize_voice(meta.get("voice"), allow_pending=True)
        voice_fallback_attachment: dict[str, Any] | None = None
        if meta.get("backend_voice_pending"):
            voice, voice_fallback_attachment = self._transcribe_pending_voice(
                conversation_id,
                session_id,
                voice,
                chat_title=raw.chat_title,
                observed_at=raw.observed_at,
            )
            meta["voice"] = voice
            meta["backend_voice_pending"] = False
        attachments_meta = raw.driver_meta.get("attachments", [])
        if not isinstance(attachments_meta, list):
            attachments_meta = []
        indexed: list[dict[str, Any]] = []
        if voice_fallback_attachment is not None:
            indexed.append(voice_fallback_attachment)
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
        text = _compose_message_text(
            _message_text_with_voice(str(raw.driver_meta.get("original_text", raw.text)), voice),
            [item for item in allowed if not _is_voice_fallback_attachment(item)],
            blocked,
        )
        if not text.strip():
            text = _voice_status_text(voice)
        meta["attachments"] = indexed
        meta["backend_attachments_pending"] = False
        meta["backend_media_pending"] = False
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

    def _transcribe_pending_voice(
        self,
        conversation_id: str,
        session_id: str,
        voice: dict[str, Any],
        *,
        chat_title: str = "",
        observed_at: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if not voice or str(voice.get("text", "")).strip():
            return voice, None
        if self.voice_transcription_bridge is None:
            bridge_payload = {"status": "blocked", "error": "wechat_voice_bridge_not_configured"}
            fallback_attachment = self._local_asr_voice_fallback(
                conversation_id,
                session_id,
                voice,
                chat_title=chat_title,
                observed_at=observed_at,
            )
            return _voice_from_transcription_failure(voice, bridge_payload, fallback_attachment), fallback_attachment
        result = self.voice_transcription_bridge.transcribe_selected_voice(conversation_id)
        payload = result_payload(result)
        if result.status == "transcribed" and result.text.strip():
            return _normalize_voice(
                {
                    **voice,
                    "status": "transcribed",
                    "source": result.source,
                    "text": result.text,
                    "method": result.method,
                    "bridge": payload,
                },
                allow_pending=True,
            ), None
        fallback_attachment = self._local_asr_voice_fallback(
            conversation_id,
            session_id,
            voice,
            chat_title=chat_title,
            observed_at=observed_at,
        )
        fallback_text = _voice_fallback_text(fallback_attachment)
        if fallback_text:
            return _normalize_voice(
                {
                    **voice,
                    "status": "transcribed",
                    "source": "local_asr_fallback",
                    "text": fallback_text,
                    "method": "file_workspace_local_asr",
                    "bridge": payload,
                    "fallback": _voice_fallback_summary(fallback_attachment),
                },
                allow_pending=True,
            ), fallback_attachment
        return _voice_from_transcription_failure(voice, payload, fallback_attachment), fallback_attachment

    def _local_asr_voice_fallback(
        self,
        conversation_id: str,
        session_id: str,
        voice: dict[str, Any],
        *,
        chat_title: str = "",
        observed_at: str = "",
    ) -> dict[str, Any] | None:
        audio_path = _voice_audio_path(voice)
        resolver_payload: dict[str, Any] | None = None
        if not audio_path and self.voice_cache_resolver is not None:
            result = self.voice_cache_resolver.resolve(voice, chat_title=chat_title, observed_at=observed_at)
            resolver_payload = result.to_dict()
            if result.status == "resolved" and result.path:
                audio_path = result.path
        if not audio_path:
            if resolver_payload is None:
                return None
            return {
                "status": "blocked",
                "source": "wechat_voice_cache_resolver",
                "name": _voice_audio_name(voice) or "wechat_voice_cache",
                "kind": "audio",
                "reason": resolver_payload.get("reason", "voice_audio_path_missing"),
                "voice_cache": resolver_payload,
            }
        fallback = self.attachment_pipeline.process(
            IncomingAttachment(
                path=audio_path,
                original_name=_voice_audio_name(voice) or Path(audio_path).name,
                kind="audio",
                source="backend_event_voice_audio" if resolver_payload is None else "wechat_voice_cache_resolver",
            ),
            conversation_id=conversation_id,
            session_id=session_id,
        )
        fallback["source"] = "backend_event_voice_audio" if resolver_payload is None else "wechat_voice_cache_resolver"
        if resolver_payload is not None:
            fallback["voice_cache"] = resolver_payload
        return fallback


def append_backend_event(
    event_path: str | Path,
    *,
    chat_title: str,
    sender_name: str,
    text: str = "",
    event_type: str = "message",
    sender_wechat_id: str = "",
    is_self: bool = False,
    is_group: bool = False,
    attachments: list[str | dict[str, Any]] | None = None,
    voice: dict[str, Any] | None = None,
    observed_at: str = "",
    quote: dict[str, Any] | None = None,
    recall: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    raw_id: str = "",
    source_payload: dict[str, Any] | None = None,
) -> str:
    payload = {
        "event_type": event_type or "message",
        "chat_title": chat_title,
        "sender_name": sender_name,
        "sender_wechat_id": sender_wechat_id,
        "text": text,
        "is_self": bool(is_self),
        "is_group": is_group,
        "observed_at": observed_at or utc_now_iso(),
        "attachments": [_attachment_payload(item) for item in attachments or []],
    }
    parsed_voice = _normalize_voice(voice, allow_pending=True)
    if parsed_voice:
        payload["voice"] = parsed_voice
    if quote:
        payload["quote"] = quote
    if recall:
        payload["recall"] = recall
    if history:
        payload["history"] = [_history_payload(item) for item in history if isinstance(item, dict)]
    if source_payload:
        payload["source_payload"] = source_payload
    raw_id = raw_id.strip() or _event_raw_id(payload)
    payload["raw_id"] = raw_id
    append_jsonl(event_path, payload)
    return raw_id


def append_backend_event_payload(event_path: str | Path, payload: dict[str, Any]) -> str:
    """Append a structured backend-captured WeChat event to the local event bus."""

    if not isinstance(payload, dict):
        raise ValueError("backend event payload must be a JSON object")
    event_type = str(payload.get("event_type") or payload.get("eventType") or "message").strip().lower()
    chat_title = str(payload.get("chat_title") or payload.get("chatTitle") or "").strip()
    sender_name = str(payload.get("sender_name") or payload.get("senderName") or "").strip()
    if event_type == "recall" and not sender_name:
        sender_name = "system"
    if not chat_title:
        raise ValueError("chat_title is required")
    if not sender_name:
        raise ValueError("sender_name is required")
    attachments = payload.get("attachments", payload.get("attachment", []))
    if isinstance(attachments, (str, dict)):
        attachments = [attachments]
    if not isinstance(attachments, list):
        attachments = []
    history = payload.get("history", [])
    if not isinstance(history, list):
        history = []
    quote = payload.get("quote")
    parsed_quote = _parse_quote(quote) if quote is not None else None
    recall = _parse_recall(payload.get("recall") or payload)
    source_payload = payload.get("source_payload") or payload.get("sourcePayload") or {}
    if not isinstance(source_payload, dict):
        source_payload = {}
    return append_backend_event(
        event_path,
        chat_title=chat_title,
        sender_name=sender_name,
        sender_wechat_id=str(payload.get("sender_wechat_id") or payload.get("senderWechatId") or "").strip(),
        text=str(payload.get("text", "")).strip(),
        event_type=event_type,
        is_self=bool(payload.get("is_self", payload.get("isSelf", False))),
        is_group=bool(payload.get("is_group", payload.get("group", False))),
        attachments=attachments,
        voice=_voice_payload(payload),
        observed_at=str(payload.get("observed_at") or payload.get("observedAt") or "").strip(),
        quote=parsed_quote,
        recall=recall,
        history=[item for item in history if isinstance(item, dict)],
        raw_id=str(payload.get("raw_id") or payload.get("rawId") or "").strip(),
        source_payload=source_payload,
    )


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
    return _parse_event_payload(payload, line_no=line_no, include_history=True)


def _parse_history(values: Any, *, parent: dict[str, Any], line_no: int) -> tuple[BackendMessageEvent, ...]:
    if not isinstance(values, list):
        return ()
    history: list[BackendMessageEvent] = []
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            continue
        merged = {
            "chat_title": parent.get("chat_title", ""),
            "sender_name": parent.get("sender_name", ""),
            "sender_wechat_id": parent.get("sender_wechat_id", ""),
            "is_self": parent.get("is_self", False),
            "is_group": parent.get("is_group", False),
            **item,
        }
        event = _parse_event_payload(merged, line_no=line_no, suffix=f"history:{index}")
        if event is not None:
            history.append(event)
    return tuple(history)


def _parse_event_payload(
    payload: dict[str, Any],
    *,
    line_no: int,
    suffix: str = "",
    include_history: bool = False,
) -> BackendMessageEvent | None:
    chat_title = str(payload.get("chat_title", "")).strip()
    event_type = str(payload.get("event_type") or "message").strip().lower()
    sender_name = str(payload.get("sender_name", "")).strip()
    if event_type == "recall" and not sender_name:
        sender_name = "system"
    if not chat_title or not sender_name:
        return None
    text = str(payload.get("text", "")).strip()
    attachments = _parse_attachments(payload.get("attachments", []))
    quote = _parse_quote(payload.get("quote"))
    recall = _parse_recall(payload.get("recall") or payload)
    voice = _normalize_voice(payload.get("voice"), allow_pending=True)
    seed = {**payload, "_line_no": line_no}
    if suffix:
        seed["_suffix"] = suffix
    raw_id = str(payload.get("raw_id", "")).strip() or _event_raw_id(seed)
    return BackendMessageEvent(
        raw_id=raw_id,
            chat_title=chat_title,
            sender_name=sender_name,
            sender_wechat_id=str(payload.get("sender_wechat_id", "")).strip() or None,
            text=text,
            event_type=event_type,
            is_self=bool(payload.get("is_self", False)),
            is_group=bool(payload.get("is_group", False)),
            observed_at=str(payload.get("observed_at", "")).strip(),
            attachments=attachments,
            voice=voice,
            quote=quote,
            recall=recall,
            source_payload=_source_payload(payload),
            history=_parse_history(payload.get("history", []), parent=payload, line_no=line_no) if include_history else (),
        )


def _attachment_payload(value: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, str):
        path = value.strip()
        return {"path": path} if path else {}
    if not isinstance(value, dict):
        return {}
    path = str(value.get("path", "")).strip()
    payload: dict[str, Any] = {"path": path}
    original_name = str(value.get("original_name") or value.get("name") or "").strip()
    kind = str(value.get("kind") or "file").strip()
    if original_name:
        payload["original_name"] = original_name
    if kind:
        payload["kind"] = kind
    return {key: item for key, item in payload.items() if item}


def _history_payload(value: dict[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    attachments = payload.get("attachments", [])
    if isinstance(attachments, (str, dict)):
        attachments = [attachments]
    if isinstance(attachments, list):
        payload["attachments"] = [_attachment_payload(item) for item in attachments if isinstance(item, (str, dict))]
    voice = _voice_payload(payload)
    if voice:
        payload["voice"] = voice
    quote = _parse_quote(payload.get("quote")) if "quote" in payload else None
    if quote:
        payload["quote"] = quote
    elif "quote" in payload:
        payload.pop("quote", None)
    return payload


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


def _parse_recall(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    target_raw_id = str(
        value.get("target_raw_id")
        or value.get("targetRawId")
        or value.get("recalled_raw_id")
        or value.get("recalledRawId")
        or ""
    ).strip()
    target_message_id = str(
        value.get("target_message_id")
        or value.get("targetMessageId")
        or value.get("recalled_message_id")
        or value.get("recalledMessageId")
        or ""
    ).strip()
    if not target_raw_id and not target_message_id:
        return None
    recall = {
        "target_raw_id": target_raw_id,
        "target_message_id": target_message_id,
        "reason": str(value.get("reason") or "wechat_recall").strip(),
        "sender_name": str(value.get("sender_name") or value.get("senderName") or "").strip(),
        "sender_wechat_id": str(value.get("sender_wechat_id") or value.get("senderWechatId") or "").strip(),
        "observed_at": str(value.get("observed_at") or value.get("observedAt") or "").strip(),
    }
    cleaned = {key: item for key, item in recall.items() if item}
    return cleaned or None


def _source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    source_payload = payload.get("source_payload") or payload.get("sourcePayload")
    if isinstance(source_payload, dict):
        return dict(source_payload)
    return {}


def _conversation_key(meta_source: dict[str, Any], chat_title: str) -> str:
    """Stable conversation identity used to derive conversation_id.

    Prefer the upstream talker id (wxid / roomid) so two contacts that share a
    display name never collapse into the same conversation, and so the driver
    and the normalizer derive the exact same conversation_id. Falls back to the
    chat title only when no talker id is available (e.g. manual backend events).
    """

    return (
        str(
            meta_source.get("conversation_key")
            or meta_source.get("conversationKey")
            or meta_source.get("talker_id")
            or meta_source.get("talkerId")
            or meta_source.get("talker")
            or chat_title
        ).strip()
        or chat_title
    )


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _ordering_metadata(
    event: BackendMessageEvent,
    source_payload: dict[str, Any],
    *,
    line_no: int,
    source_line_no: int,
    source_offset: int,
    batch_index: int,
    import_sequence: int,
) -> dict[str, Any]:
    hook = source_payload.get("hook") if isinstance(source_payload.get("hook"), dict) else {}
    ordering = hook.get("ordering") if isinstance(hook.get("ordering"), dict) else {}
    return {
        key: value
        for key, value in {
            "observed_at": event.observed_at,
            "conversation_key": source_payload.get("conversation_key") or source_payload.get("talker_id") or event.chat_title,
            "message_type": source_payload.get("message_type"),
            "sort_key": source_payload.get("sort_key") or hook.get("sort_key") or ordering.get("sort_key"),
            "server_id": source_payload.get("server_id") or hook.get("server_id") or ordering.get("server_id"),
            "local_id": source_payload.get("local_id") or hook.get("local_id") or ordering.get("local_id"),
            "message_key": source_payload.get("message_key") or hook.get("message_key") or ordering.get("message_key"),
            "create_time": source_payload.get("create_time") or hook.get("create_time") or ordering.get("create_time"),
            "local_type": source_payload.get("local_type") or hook.get("local_type") or ordering.get("local_type"),
            "msg_id": source_payload.get("msg_id") or hook.get("msg_id") or ordering.get("msg_id"),
            "backend_line_no": line_no,
            "source_line_no": source_line_no,
            "source_offset": source_offset,
            "batch_index": batch_index,
            "import_sequence": import_sequence,
        }.items()
        if value not in {"", None}
    }


def _voice_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    raw = payload.get("voice")
    if isinstance(raw, dict):
        voice = dict(raw)
    else:
        voice = {}
    explicit_voice_keys = {
        "voice_text",
        "voiceText",
        "transcript",
        "voice_status",
        "voiceStatus",
        "voice_duration",
        "voiceDuration",
        "voice_audio",
        "voiceAudio",
        "voice_audio_path",
        "voiceAudioPath",
        "voice_audio_name",
        "voiceAudioName",
    }
    has_voice_marker = bool(voice) or any(str(payload.get(key, "")).strip() for key in explicit_voice_keys)
    if not has_voice_marker:
        return None
    text = str(
        voice.get("text")
        or payload.get("voice_text")
        or payload.get("voiceText")
        or payload.get("transcript")
        or ""
    ).strip()
    status = str(voice.get("status") or payload.get("voice_status") or payload.get("voiceStatus") or ("transcribed" if text else "pending")).strip()
    duration = str(
        voice.get("duration")
        or payload.get("voice_duration")
        or payload.get("voiceDuration")
        or ""
    ).strip()
    audio_path = str(
        voice.get("audio_path")
        or voice.get("path")
        or voice.get("file_path")
        or payload.get("voice_audio")
        or payload.get("voiceAudio")
        or payload.get("voice_audio_path")
        or payload.get("voiceAudioPath")
        or ""
    ).strip()
    audio_name = str(
        voice.get("audio_name")
        or voice.get("name")
        or payload.get("voice_audio_name")
        or payload.get("voiceAudioName")
        or ""
    ).strip()
    if not text and status not in {"pending", "selected", "detected", "blocked", "failed"} and not audio_path:
        return None
    return _normalize_voice(
        {
            **voice,
            "status": status,
            "text": text,
            "duration": duration,
            "audio_path": audio_path,
            "audio_name": audio_name,
        },
        allow_pending=True,
    )


def _normalize_voice(value: Any, *, allow_pending: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    if not value:
        return {}
    text = str(value.get("text", "")).strip()
    status = str(value.get("status") or ("transcribed" if text else "pending")).strip().lower()
    if not text and not (allow_pending and status in {"pending", "selected", "detected", "blocked", "failed"}):
        return {}
    return {
        "status": status,
        "source": str(value.get("source") or "wechat_builtin_voice_to_text").strip(),
        "text": text,
        "duration": str(value.get("duration", "")).strip(),
        **_optional_voice_fields(value),
    }


def _message_text_with_voice(text: str, voice: dict[str, Any]) -> str:
    clean_text = text.strip()
    voice_text = str(voice.get("text", "")).strip() if voice else ""
    if not voice_text:
        return clean_text
    if clean_text and _normalize_for_match(clean_text) == _normalize_for_match(voice_text):
        return clean_text
    if clean_text:
        return f"{clean_text}\n[微信语音转文字]\n{voice_text}"
    return voice_text


def _voice_needs_transcription(voice: dict[str, Any]) -> bool:
    if not voice:
        return False
    if str(voice.get("text", "")).strip():
        return False
    return str(voice.get("status") or "pending").strip() in {"pending", "selected", "detected"}


def _voice_status_text(voice: dict[str, Any]) -> str:
    if not voice:
        return ""
    text = str(voice.get("text", "")).strip()
    if text:
        return text
    status = str(voice.get("status") or "pending").strip()
    error = str(voice.get("error", "")).strip()
    suffix = f" error={error}" if error else ""
    return f"[微信语音转文字未完成] status={status}{suffix}".strip()


def _voice_audio_path(voice: dict[str, Any]) -> str:
    for key in ("audio_path", "path", "file_path", "local_path"):
        value = str(voice.get(key, "")).strip()
        if value:
            return value
    audio = voice.get("audio")
    if isinstance(audio, dict):
        for key in ("path", "file_path", "local_path"):
            value = str(audio.get(key, "")).strip()
            if value:
                return value
    return ""


def _voice_audio_name(voice: dict[str, Any]) -> str:
    for key in ("audio_name", "name", "filename", "file_name"):
        value = str(voice.get(key, "")).strip()
        if value:
            return value
    return ""


def _voice_fallback_text(attachment: dict[str, Any] | None) -> str:
    if not isinstance(attachment, dict) or attachment.get("status") != "indexed":
        return ""
    parse = attachment.get("parse")
    if not isinstance(parse, dict):
        return ""
    if parse.get("kind") != "audio":
        return ""
    return str(parse.get("raw_text") or parse.get("text") or "").strip()


def _voice_fallback_summary(attachment: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(attachment, dict):
        return {"status": "not_available", "reason": "voice_audio_path_missing"}
    parse = attachment.get("parse") if isinstance(attachment.get("parse"), dict) else {}
    return {
        "source": attachment.get("source", ""),
        "status": attachment.get("status", ""),
        "name": attachment.get("name", ""),
        "kind": attachment.get("kind", ""),
        "file_id": attachment.get("file_id", ""),
        "parse_status": parse.get("status", ""),
        "parse_error": parse.get("error", ""),
        "voice_cache": attachment.get("voice_cache", {}),
    }


def _is_voice_fallback_attachment(attachment: dict[str, Any]) -> bool:
    return str(attachment.get("source", "")).strip() in {"backend_event_voice_audio", "wechat_voice_cache_resolver"}


def _voice_from_transcription_failure(
    voice: dict[str, Any],
    bridge_payload: dict[str, Any],
    fallback_attachment: dict[str, Any] | None,
) -> dict[str, Any]:
    fallback = _voice_fallback_summary(fallback_attachment)
    error = str(bridge_payload.get("error") or fallback.get("parse_error") or "voice_transcription_unavailable")
    return {
        **voice,
        "status": str(bridge_payload.get("status") or "blocked"),
        "source": str(bridge_payload.get("source") or voice.get("source") or "wechat_builtin_voice_to_text"),
        "text": "",
        "method": str(bridge_payload.get("method") or ""),
        "error": error,
        "blockers": bridge_payload.get("blockers", []),
        "bridge": bridge_payload,
        "fallback": fallback,
    }


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


def _compose_pending_message_text(text: str, attachments: list[dict[str, Any]], voice: dict[str, Any] | None = None) -> str:
    lines = [text.strip()] if text.strip() else []
    if voice and _voice_needs_transcription(voice):
        duration = str(voice.get("duration", "")).strip()
        duration_note = f" duration={duration}" if duration else ""
        lines.append(f"[微信语音待转文字]{duration_note}")
    for item in attachments:
        lines.append(f"[后台附件待处理] {item.get('name', '')} kind={item.get('kind', 'file')}")
    return "\n".join(lines).strip()


def _optional_voice_fields(value: dict[str, Any]) -> dict[str, Any]:
    optional: dict[str, Any] = {}
    for key in ("method", "error", "blockers", "bridge", "fallback", "audio_path", "audio_name"):
        item = value.get(key)
        if item not in (None, "", [], {}):
            optional[key] = item
    return optional


def _event_raw_id(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:24]


def _normalize_for_match(text: str) -> str:
    return " ".join(text.split()).lower()

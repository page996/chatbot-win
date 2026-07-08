from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.bootstrap import BotRuntime
from app.personal_wechat_bot.domain.models import NormalizedMessage, RawWeChatMessage, ReplyCandidate, SpeakDecision
from app.personal_wechat_bot.tasks.manager import TaskStatusStore
from app.personal_wechat_bot.tools.permissions import resolve_allowed_roots
from app.personal_wechat_bot.vision.ocr import build_default_ocr_engine
from app.personal_wechat_bot.voice.asr import LocalAsrSubprocessEngine
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import BackendAttachmentParser
from app.personal_wechat_bot.workspace.attachment_pipeline import AttachmentPipeline, IncomingAttachment


class MessageProcessor:
    def __init__(self, runtime: BotRuntime):
        self.runtime = runtime
        self._outgoing_attachment_pipeline: AttachmentPipeline | None = None

    def process(self, raw: RawWeChatMessage) -> dict[str, Any] | None:
        self.runtime.event_logger.log("message.raw", asdict(raw))
        message = self.runtime.normalizer.normalize(raw)
        if message is None:
            return None

        self.runtime.event_logger.log("message.normalized", message, message_id=message.message_id)
        original_message_id = message.message_id

        reset_session_id = self.runtime.session_store.maybe_reset_for_message(message)
        if reset_session_id:
            pending_context_item = {"session_id": reset_session_id, "reset": True}
        else:
            pending_context_item = None
        session_id = reset_session_id or self.runtime.session_store.current_session_id_for_message(message)
        message = self._with_session_id(message, session_id)

        recall_item = self._handle_recall_event(message)
        if recall_item is not None:
            self._mark_message_done(message, original_message_id)
            return recall_item

        raw = self._enrich_backend_media(raw, message.conversation_id, session_id)
        if raw.driver_meta.get("backend_media_pending") is False or raw.driver_meta.get("backend_attachments_pending") is False:
            enriched = self.runtime.normalizer.normalize(raw)
            enriched = self._with_session_id(enriched, session_id) if enriched is not None else None
            if enriched is not None and enriched != message:
                message = enriched
                self.runtime.event_logger.log("message.enriched", message, message_id=message.message_id)

        route = self.runtime.router.decide(message)
        self.runtime.event_logger.log("route.decision", route, message_id=message.message_id)
        item: dict[str, Any] = {"message": asdict(message), "route": asdict(route)}
        if pending_context_item is not None:
            item["context"] = pending_context_item

        ledger_result = self.runtime.ledger_store.append_message_result(message)
        ledger_entry = ledger_result.entry
        item["ledger"] = {"status": ledger_result.status, "entry_id": ledger_entry.entry_id}
        if ledger_result.status in {"created", "updated"}:
            link_annotations = self._annotate_links(ledger_entry)
            if link_annotations:
                item["link_annotations"] = link_annotations
            memory = self._maintain_memory(message.conversation_id, session_id)
            if memory:
                item["memory"] = memory
            if _message_has_attachments(message):
                self.runtime.ledger_store.refresh_file_refs(message.conversation_id)
        if ledger_result.status != "created":
            if not self._should_retry_existing_message(message, route):
                item["context_only"] = True
                self._mark_message_done(message, original_message_id)
                return item
            item["retry_existing"] = True

        if message.is_self:
            item["context_only"] = True
            self._mark_message_done(message)
            return item

        if route.action != "process":
            if route.action in {"ignore", "duplicate"}:
                self._mark_message_done(message)
            return item

        if message.metadata.get("context_only"):
            item["context_only"] = True
            self._mark_message_done(message, original_message_id)
            return item

        speak = self.runtime.topic_classifier.decide(message)
        speak = self._apply_group_cooldown(
            message.conversation_type,
            message.conversation_id,
            message.received_at,
            speak,
        )
        self.runtime.event_logger.log("topic.decision", speak, message_id=message.message_id)
        item["speak"] = asdict(speak)

        reply_task_id = ""
        if speak.decision == "speak":
            reply_task_id = self._record_reply_task(message, session_id, speak)
            self._transition_task(
                reply_task_id,
                "start",
                {"progress": 15, "phase": "正在生成回复", "detail": speak.reason},
            )
        try:
            reply = self.runtime.conversation.generate_reply(message, speak)
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            self.runtime.event_logger.log("reply.error", error, message_id=message.message_id)
            item["error"] = error
            self._transition_task(
                reply_task_id,
                "fail",
                {"progress": 100, "phase": "回复生成失败", "detail": str(exc), "last_error": str(exc)},
            )
            return item

        if reply is None:
            self._transition_task(
                reply_task_id,
                "complete",
                {"progress": 100, "phase": "无需生成回复", "detail": speak.reason},
            )
            self._mark_message_done(message, original_message_id)
            return item

        self._update_task(reply_task_id, {"progress": 70, "phase": "回复候选已生成"})
        reply = self._enrich_reply_attachments(reply, session_id)
        reply_entry = self.runtime.ledger_store.append_reply(
            reply,
            chat_title=message.chat_title,
            conversation_type=message.conversation_type,
            session_id=session_id,
        )
        memory = self._maintain_memory(message.conversation_id, session_id)
        if memory:
            item["memory_after_reply"] = memory
        self.runtime.event_logger.log("reply.candidate", reply, message_id=message.message_id)
        send = self.runtime.reply_gate.handle(reply)
        self.runtime.ledger_store.update_reply_send_result(message.conversation_id, reply_entry.entry_id, send)
        self.runtime.event_logger.log("send.result", send, message_id=message.message_id)
        self._transition_task(
            reply_task_id,
            "complete",
            {"progress": 100, "phase": "回复候选已入账", "detail": reply.summary or reply.text[:200], "actual_cost": 1},
        )
        self._record_send_task(reply, session_id, send)
        item["reply"] = asdict(reply)
        item["send"] = asdict(send)
        self._mark_message_done(message, original_message_id)
        return item

    def _apply_group_cooldown(
        self,
        conversation_type: str,
        conversation_id: str,
        received_at: str,
        speak: SpeakDecision,
    ) -> SpeakDecision:
        if speak.decision != "speak" or conversation_type != "group":
            return speak
        allowed, reason = self.runtime.cooldown.allow(conversation_id, received_at)
        if allowed:
            return speak
        return SpeakDecision(
            conversation_id=speak.conversation_id,
            decision="wait",
            reason=reason,
            topic=speak.topic,
            confidence=speak.confidence,
            style_context=speak.style_context,
            daily_trace_context=speak.daily_trace_context,
        )

    def _enrich_backend_media(self, raw: RawWeChatMessage, conversation_id: str, session_id: str) -> RawWeChatMessage:
        if not (
            raw.driver_meta.get("backend_media_pending")
            or raw.driver_meta.get("backend_attachments_pending")
            or raw.driver_meta.get("backend_voice_pending")
        ):
            return raw
        driver = getattr(self.runtime, "active_driver", None)
        enrich = getattr(driver, "enrich_message_attachments", None)
        if enrich is None:
            return raw
        try:
            return enrich(raw, conversation_id=conversation_id, session_id=session_id)
        except Exception as exc:
            self.runtime.event_logger.log(
                "message.media_enrich_error",
                {"type": type(exc).__name__, "message": str(exc)},
                message_id=raw.raw_id,
            )
            return raw

    def _record_reply_task(self, message: NormalizedMessage, session_id: str, speak: SpeakDecision) -> str:
        task_id = _task_id("reply", message.message_id)
        title = f"生成回复：{message.chat_title or message.sender_name or message.conversation_id}"
        try:
            TaskStatusStore(self.runtime.config.data_dir).create(
                {
                    "task_id": task_id,
                    "title": title,
                    "kind": "reply",
                    "conversation_id": message.conversation_id,
                    "session_id": session_id,
                    "scope": f"conversation:{message.conversation_id}",
                    "topic_id": _topic_id(message, speak),
                    "topic_title": speak.topic or "回复生成",
                    "resource_class": "llm_interactive",
                    "priority": 80,
                    "estimated_cost": 2,
                    "external_id": message.message_id,
                    "metadata": {
                        "message_id": message.message_id,
                        "sender": message.sender_name,
                        "chat_title": message.chat_title,
                    },
                }
            )
        except Exception:
            return task_id
        return task_id

    def _record_send_task(self, reply: ReplyCandidate, session_id: str, send: Any) -> None:
        task_id = _task_id("send", reply.message_id)
        status = str(getattr(send, "status", "") or "")
        reason = str(getattr(send, "reason", "") or "")
        bridge_id = _bridge_id_from_reason(reason)
        try:
            store = TaskStatusStore(self.runtime.config.data_dir)
            store.create(
                {
                    "task_id": task_id,
                    "title": "发送回复",
                    "kind": "send",
                    "conversation_id": reply.conversation_id,
                    "session_id": session_id,
                    "scope": f"conversation:{reply.conversation_id}",
                    "topic_id": _task_id("reply", reply.message_id),
                    "topic_title": "回复发送",
                    "resource_class": "send_bridge",
                    "priority": 85,
                    "estimated_cost": 1,
                    "external_id": bridge_id,
                    "metadata": {
                        "message_id": reply.message_id,
                        "bridge_id": bridge_id,
                        "send_status": status,
                        "send_reason": reason,
                    },
                }
            )
            if status == "queued_for_confirm":
                store.transition(
                    task_id,
                    "wait",
                    {"progress": 45, "phase": "等待人工审核", "detail": reason, "blocker": "send_confirm_required"},
                )
            elif status == "queued_to_bridge":
                store.update(task_id, {"status": "queued", "progress": 70, "phase": "等待非前台桥发送", "detail": reason})
            elif status in {"sent", "skipped"}:
                store.transition(task_id, "complete", {"progress": 100, "phase": "发送链路已完成", "detail": reason})
            else:
                store.transition(task_id, "fail", {"progress": 100, "phase": "发送失败", "detail": reason, "last_error": reason})
        except Exception:
            return

    def _transition_task(self, task_id: str, action: str, patch: dict[str, Any]) -> None:
        if not task_id:
            return
        try:
            TaskStatusStore(self.runtime.config.data_dir).transition(task_id, action, patch)
        except Exception:
            return

    def _update_task(self, task_id: str, patch: dict[str, Any]) -> None:
        if not task_id:
            return
        try:
            TaskStatusStore(self.runtime.config.data_dir).update(task_id, patch)
        except Exception:
            return

    def _handle_recall_event(self, message: NormalizedMessage) -> dict[str, Any] | None:
        if message.metadata.get("event_type") != "recall":
            return None
        recall = message.metadata.get("recall") if isinstance(message.metadata.get("recall"), dict) else {}
        target_message_id = str(recall.get("target_message_id") or recall.get("message_id") or "").strip()
        target_raw_id = str(recall.get("target_raw_id") or recall.get("raw_id") or "").strip()
        target_id = target_message_id
        if not target_id and target_raw_id:
            target_id = self.runtime.normalizer.normalize(
                RawWeChatMessage(
                    raw_id=target_raw_id,
                    chat_title=message.chat_title,
                    sender_name=str(recall.get("sender_name") or message.sender_name),
                    text=str(recall.get("text") or ""),
                    is_self=bool(recall.get("is_self", False)),
                    is_group=message.conversation_type == "group",
                    sender_wechat_id=str(recall.get("sender_wechat_id") or "") or None,
                    observed_at=str(recall.get("observed_at") or message.received_at),
                    driver_meta={
                        "allow_empty_message": True,
                        "conversation_key": str(message.metadata.get("conversation_key") or message.chat_title),
                    },
                )
            ).message_id
        changed = False
        if target_id:
            changed = self.runtime.ledger_store.mark_recalled(
                message.conversation_id,
                target_id,
                reason=str(recall.get("reason") or "wechat_recall"),
            )
        return {
            "message": asdict(message),
            "route": {
                "message_id": message.message_id,
                "conversation_id": message.conversation_id,
                "action": "ignore",
                "reason": "recall_event",
                "requires_topic_decision": False,
            },
            "recall": {
                "status": "marked" if changed else "not_found",
                "target_message_id": target_id,
                "target_raw_id": target_raw_id,
                "reason": str(recall.get("reason") or "wechat_recall"),
            },
            "context_only": True,
        }

    def _annotate_links(self, ledger_entry) -> list[dict[str, Any]]:
        annotate = getattr(getattr(self.runtime, "link_annotations", None), "annotate_entry", None)
        if annotate is None:
            return []
        try:
            return annotate(ledger_entry)
        except Exception as exc:
            self.runtime.event_logger.log(
                "message.link_annotation_error",
                {"type": type(exc).__name__, "message": str(exc)},
                message_id=ledger_entry.message_id,
            )
            return []

    def _maintain_memory(self, conversation_id: str, session_id: str) -> dict[str, Any]:
        maintain = getattr(getattr(self.runtime, "memory_maintainer", None), "maintain", None)
        if maintain is None:
            return {}
        try:
            result = maintain(conversation_id, session_id=session_id)
            payload = result.__dict__ if hasattr(result, "__dict__") else dict(result)
            self.runtime.event_logger.log("memory.maintained", payload)
            return payload
        except Exception as exc:
            self.runtime.event_logger.log(
                "memory.maintain_error",
                {"type": type(exc).__name__, "message": str(exc)},
            )
            return {}

    def _mark_done(self, *message_ids: str) -> None:
        for message_id in dict.fromkeys(item for item in message_ids if item):
            self.runtime.router.mark_done(message_id)

    def _mark_message_done(self, message: NormalizedMessage, *message_ids: str) -> None:
        mark_message_done = getattr(self.runtime.router, "mark_message_done", None)
        if mark_message_done is not None:
            mark_message_done(message)
        self._mark_done(*message_ids)

    def _should_retry_existing_message(self, message: NormalizedMessage, route) -> bool:
        if message.is_self or message.metadata.get("context_only") or route.action != "process":
            return False
        try:
            entries = self.runtime.ledger_store.read_entries(message.conversation_id, include_removed=True)
        except Exception:
            return False
        for entry in entries:
            if entry.role == "assistant" and entry.message_id == message.message_id:
                return False
        return True

    def _with_session_id(self, message: NormalizedMessage, session_id: str) -> NormalizedMessage:
        metadata = dict(message.metadata)
        metadata["session_id"] = session_id
        return replace(message, metadata=metadata)

    def _enrich_reply_attachments(self, reply: ReplyCandidate, session_id: str) -> ReplyCandidate:
        candidates = self._reply_attachment_candidates(reply)
        if not candidates:
            return reply
        # Parse outgoing attachments in parallel (OCR/ASR are subprocess/IO bound),
        # but preserve candidate order and dedup deterministically afterward. A
        # single attachment stays on the calling thread to avoid pool overhead.
        if len(candidates) == 1:
            processed_list = [self._process_outgoing_attachment(candidates[0], reply.conversation_id, session_id)]
        else:
            processed_list = [None] * len(candidates)
            max_workers = min(4, len(candidates))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                future_to_index = {
                    pool.submit(
                        self._process_outgoing_attachment, attachment, reply.conversation_id, session_id
                    ): index
                    for index, attachment in enumerate(candidates)
                }
                for future in as_completed(future_to_index):
                    index = future_to_index[future]
                    try:
                        processed_list[index] = future.result()
                    except Exception as exc:
                        # A parse failure must not drop the file: keep it as a
                        # blocked attachment so it can still be sent (integrity first).
                        candidate = candidates[index]
                        processed_list[index] = {
                            **candidate,
                            "status": "blocked",
                            "reason": f"outgoing_parse_error:{type(exc).__name__}",
                        }
        enriched: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for processed in processed_list:
            if processed is None:
                continue
            key = (
                str(processed.get("path", "")),
                str(processed.get("name", "")),
                str(processed.get("source", "")),
            )
            if key in seen:
                continue
            seen.add(key)
            enriched.append(processed)
        return replace(reply, attachments=enriched)

    def _reply_attachment_candidates(self, reply: ReplyCandidate) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for item in reply.attachments:
            normalized = _attachment_candidate(item, source="reply_candidate")
            if normalized:
                candidates.append(normalized)
        if reply.tool_result is not None:
            for index, ref in enumerate(reply.tool_result.output_refs):
                normalized = _attachment_candidate(
                    {
                        "path": str(ref),
                        "name": Path(str(ref)).name,
                        "kind": "tool_output",
                        "tool_name": reply.tool_result.tool_name,
                        "call_id": reply.tool_result.call_id,
                        "output_index": index,
                    },
                    source="tool_result",
                )
                if normalized:
                    candidates.append(normalized)
        return candidates

    def _process_outgoing_attachment(
        self,
        attachment: dict[str, Any],
        conversation_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        if attachment.get("status") == "indexed" and isinstance(attachment.get("parse"), dict):
            return attachment
        path = str(attachment.get("path", "")).strip()
        name = str(attachment.get("name") or Path(path).name).strip()
        kind = str(attachment.get("kind") or "file").strip()
        source = str(attachment.get("source") or "reply_candidate").strip()
        if not path:
            return {
                **attachment,
                "status": "blocked",
                "source": source,
                "name": name,
                "kind": kind,
                "reason": "outgoing_attachment_path_missing",
            }
        processed = self._outgoing_pipeline().process(
            IncomingAttachment(
                path=path,
                original_name=name or Path(path).name,
                kind=kind,
                source=source,
            ),
            conversation_id=conversation_id,
            session_id=session_id,
        )
        return {
            **attachment,
            **processed,
            "path": path,
            "source": source,
            "tool_name": attachment.get("tool_name", ""),
            "call_id": attachment.get("call_id", ""),
            "output_index": attachment.get("output_index", ""),
        }

    def _outgoing_pipeline(self) -> AttachmentPipeline:
        if self._outgoing_attachment_pipeline is None:
            config = self.runtime.config
            data_root = Path(config.data_dir)
            roots = [
                *config.file_read_roots,
                str(data_root / "tool_outputs"),
                str(data_root / "file_workspace"),
            ]
            self._outgoing_attachment_pipeline = AttachmentPipeline(
                file_index=self.runtime.file_index,
                file_workspace=self.runtime.file_workspace,
                attachment_parser=BackendAttachmentParser(
                    build_default_ocr_engine(mode=config.ocr_mode),
                    LocalAsrSubprocessEngine(mode=config.asr_mode),
                ),
                allowed_input_roots=resolve_allowed_roots(config.data_dir, roots),
                # Agent-produced files are trusted artifacts: use the relaxed
                # outgoing limits (empty extension list = allow any type) so the
                # agent's own output is never blocked by the inbound guard.
                allowed_extensions=config.outgoing_file_allowed_extensions,
                max_input_bytes=config.outgoing_file_max_bytes,
            )
        return self._outgoing_attachment_pipeline


def _attachment_candidate(value: Any, *, source: str) -> dict[str, Any]:
    if isinstance(value, str):
        path = value.strip()
        if not path:
            return {}
        return {"path": path, "name": Path(path).name, "kind": "file", "source": source}
    if not isinstance(value, dict):
        return {}
    path = str(value.get("path") or value.get("source_ref") or value.get("output_ref") or "").strip()
    name = str(value.get("name") or value.get("filename") or Path(path).name).strip()
    if not path and not name:
        return {}
    kind = str(value.get("kind") or value.get("type") or "file").strip()
    return {
        **value,
        "path": path,
        "name": name,
        "kind": kind,
        "source": str(value.get("source") or source).strip(),
    }


def _message_has_attachments(message: NormalizedMessage) -> bool:
    attachments = message.metadata.get("attachments")
    return isinstance(attachments, list) and any(isinstance(item, dict) for item in attachments)


def _task_id(prefix: str, value: str) -> str:
    cleaned = "".join(ch for ch in str(value or "").strip() if ch.isalnum() or ch in {"-", "_", "."})
    return f"{prefix}-{cleaned[:64] or 'unknown'}"


def _topic_id(message: NormalizedMessage, speak: SpeakDecision) -> str:
    topic = str(speak.topic or "").strip()
    if topic:
        return _task_id("topic", topic)
    return _task_id("message", message.message_id)


def _bridge_id_from_reason(reason: str) -> str:
    marker = "bridge:"
    text = str(reason or "")
    index = text.rfind(marker)
    if index < 0:
        return ""
    candidate = text[index:].split()[0].strip("，,.;；")
    return candidate if candidate.startswith(marker) else ""

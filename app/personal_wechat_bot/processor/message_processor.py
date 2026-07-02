from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any

from app.personal_wechat_bot.bootstrap import BotRuntime
from app.personal_wechat_bot.domain.models import NormalizedMessage, RawWeChatMessage, SpeakDecision


class MessageProcessor:
    def __init__(self, runtime: BotRuntime):
        self.runtime = runtime

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
        session_id = reset_session_id or self.runtime.session_store.current_session_id(message.conversation_id)
        message = self._with_session_id(message, session_id)

        recall_item = self._handle_recall_event(message)
        if recall_item is not None:
            self._mark_done(original_message_id, message.message_id)
            return recall_item

        raw = self._enrich_backend_media(raw, message.conversation_id)
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

        ledger_entry = self.runtime.ledger_store.append_message(message)
        link_annotations = self._annotate_links(ledger_entry)
        if link_annotations:
            item["link_annotations"] = link_annotations
        memory = self._maintain_memory(message.conversation_id, session_id)
        if memory:
            item["memory"] = memory

        if message.is_self:
            item["context_only"] = True
            self.runtime.router.mark_done(message.message_id)
            return item

        if route.action != "process":
            if route.action == "ignore":
                self.runtime.router.mark_done(message.message_id)
            return item

        if message.metadata.get("context_only"):
            item["context_only"] = True
            self._mark_done(original_message_id, message.message_id)
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

        try:
            reply = self.runtime.conversation.generate_reply(message, speak)
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            self.runtime.event_logger.log("reply.error", error, message_id=message.message_id)
            item["error"] = error
            return item

        if reply is None:
            self._mark_done(original_message_id, message.message_id)
            return item

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
        item["reply"] = asdict(reply)
        item["send"] = asdict(send)
        self._mark_done(original_message_id, message.message_id)
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

    def _enrich_backend_media(self, raw: RawWeChatMessage, conversation_id: str) -> RawWeChatMessage:
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
        session_id = self.runtime.session_store.current_session_id(conversation_id)
        try:
            return enrich(raw, conversation_id=conversation_id, session_id=session_id)
        except Exception as exc:
            self.runtime.event_logger.log(
                "message.media_enrich_error",
                {"type": type(exc).__name__, "message": str(exc)},
                message_id=raw.raw_id,
            )
            return raw

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

    def _with_session_id(self, message: NormalizedMessage, session_id: str) -> NormalizedMessage:
        metadata = dict(message.metadata)
        metadata["session_id"] = session_id
        return replace(message, metadata=metadata)

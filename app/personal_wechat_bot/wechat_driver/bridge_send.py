from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.domain.models import SendResult, utc_now_iso
from app.personal_wechat_bot.wechat_driver.window_binding import WeChatWindowBinding, WeChatWindowBindingStore


BRIDGE_OUTBOX_SEND_DRIVER = "bridge_outbox"
ALLOWED_MANUAL_BINDING_STATUSES = {"active", "stale"}


@dataclass(frozen=True)
class BridgeSendProbe:
    driver: str
    implemented: bool
    send_enabled: bool
    health: str
    outbox_path: str
    ack_path: str
    pending_count: int
    ack_count: int
    manual_bound_count: int
    authorization: str
    blockers: list[str]


class BridgeOutboxStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.root = Path(data_dir) / "send_bridge"
        self.outbox_path = self.root / "outbox.jsonl"
        self.ack_path = self.root / "acks.jsonl"
        self.root.mkdir(parents=True, exist_ok=True)
        # Touch outbox/ack files on init so they exist for sidebar/state queries
        # even before the first send. The worker and state() expect readable files.
        for path in (self.outbox_path, self.ack_path):
            if not path.exists():
                path.write_text("", encoding="utf-8")

    def enqueue(
        self,
        conversation_id: str,
        text: str,
        *,
        receiver: str = "",
        manual_binding: WeChatWindowBinding | None = None,
    ) -> dict[str, Any]:
        bridge_id = f"bridge:{conversation_id}:{uuid.uuid4().hex[:12]}"
        record = {
            "bridge_id": bridge_id,
            "conversation_id": conversation_id,
            "receiver": receiver,
            "kind": "text",
            "text": text,
            "status": "queued",
            "created_at": utc_now_iso(),
            "transport": "wcf_bridge",
            "manual_binding": _manual_binding_payload(manual_binding),
        }
        self._append(self.outbox_path, record)
        return record

    def enqueue_file(
        self,
        conversation_id: str,
        path: str,
        *,
        name: str = "",
        caption: str = "",
        receiver: str = "",
        manual_binding: WeChatWindowBinding | None = None,
    ) -> dict[str, Any]:
        bridge_id = f"bridge:{conversation_id}:{uuid.uuid4().hex[:12]}"
        record = {
            "bridge_id": bridge_id,
            "conversation_id": conversation_id,
            "receiver": receiver,
            "kind": "file",
            "path": str(path),
            "name": name or Path(path).name,
            "caption": caption,
            "status": "queued",
            "created_at": utc_now_iso(),
            "transport": "wcf_bridge",
            "manual_binding": _manual_binding_payload(manual_binding),
        }
        self._append(self.outbox_path, record)
        return record

    def append_ack(
        self,
        bridge_id: str,
        *,
        status: str,
        reason: str = "",
        external_message_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"sent", "failed", "blocked"}:
            raise ValueError("status must be sent, failed, or blocked")
        record = {
            "bridge_id": bridge_id,
            "status": status,
            "reason": reason,
            "external_message_id": external_message_id,
            "payload": payload or {},
            "created_at": utc_now_iso(),
        }
        self._append(self.ack_path, record)
        return record

    def state(self, *, limit: int = 30) -> dict[str, Any]:
        outbox = self._read_all(self.outbox_path)
        acks = self._read_all(self.ack_path)
        manual_bindings = manual_bridge_bindings(self.data_dir)
        latest_acks = _latest_by_bridge_id(acks)
        items = []
        for item in outbox[-max(1, limit) :]:
            bridge_id = str(item.get("bridge_id", ""))
            ack = latest_acks.get(bridge_id)
            status = str(ack.get("status", item.get("status", "queued"))) if ack else str(item.get("status", "queued"))
            items.append({**item, "status": status, "ack": ack or {}})
        pending_count = sum(1 for item in outbox if str(latest_acks.get(str(item.get("bridge_id", "")), {}).get("status", item.get("status", "queued"))) == "queued")
        return {
            "status": "ok",
            "driver": BRIDGE_OUTBOX_SEND_DRIVER,
            "outbox_path": str(self.outbox_path),
            "ack_path": str(self.ack_path),
            "count": len(outbox),
            "pending_count": pending_count,
            "ack_count": len(acks),
            "manual_bound_count": len(manual_bindings),
            "manual_bound_conversations": [
                _manual_binding_payload(item)
                for item in sorted(manual_bindings, key=lambda value: value.last_seen_at, reverse=True)
            ],
            "items": items,
            "contract": {
                "producer": "agent writes outbox.jsonl (text + file records)",
                "authorization": "conversation whitelist enforced upstream by the router",
                "consumer": "send_bridge_worker delivers via WeChatFerry (no foreground) and writes acks.jsonl",
                "delivery_claim": "queued_to_bridge_not_confirmed_sent_until_ack",
            },
        }

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _read_all(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
        return records


class BridgeOutboxSendDriver:
    """Non-foreground send bridge producer.

    This driver does not touch the WeChat foreground window. It queues messages
    to a local outbox for a separate bridge process to deliver and acknowledge.
    """

    def __init__(self, *, send_enabled: bool, data_dir: str | Path = "data"):
        self.send_enabled = send_enabled
        self.data_dir = Path(data_dir)
        self.store = BridgeOutboxStore(data_dir)
        self.binding_store = WeChatWindowBindingStore(data_dir)

    def health_check(self) -> bool:
        return self.store.root.exists()

    def probe(self) -> BridgeSendProbe:
        state = self.store.state(limit=1)
        manual_bound_count = int(state.get("manual_bound_count", 0) or 0)
        blockers = [] if self.send_enabled else ["send_enabled_false"]
        return BridgeSendProbe(
            driver=BRIDGE_OUTBOX_SEND_DRIVER,
            implemented=True,
            send_enabled=self.send_enabled,
            health="ready" if not blockers else "blocked",
            outbox_path=str(self.store.outbox_path),
            ack_path=str(self.store.ack_path),
            pending_count=int(state.get("pending_count", 0) or 0),
            ack_count=int(state.get("ack_count", 0) or 0),
            manual_bound_count=manual_bound_count,
            authorization="conversation_whitelist",
            blockers=blockers,
        )

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        if not self.send_enabled:
            return SendResult("bridge-outbox-send", conversation_id, "failed", "send_enabled_false")
        if not text.strip():
            return SendResult("bridge-outbox-send", conversation_id, "failed", "empty_reply")
        # wcf delivers by wxid/roomid, so a manual foreground window binding is no
        # longer required. Whitelist authorization is enforced upstream by the
        # router; attach the binding as metadata when present, but never block on it.
        manual_binding = self._manual_binding_for(conversation_id)
        receiver = self._receiver_for(conversation_id)
        record = self.store.enqueue(conversation_id, text, receiver=receiver, manual_binding=manual_binding)
        return SendResult(
            message_id=str(record["bridge_id"]),
            conversation_id=conversation_id,
            status="queued_to_bridge",
            reason=f"queued_to_non_foreground_bridge:{record['bridge_id']}",
        )

    def send_file(self, conversation_id: str, path: str, caption: str = "") -> SendResult:
        if not self.send_enabled:
            return SendResult("bridge-outbox-send", conversation_id, "failed", "send_enabled_false")
        if not str(path).strip():
            return SendResult("bridge-outbox-send", conversation_id, "failed", "empty_file_path")
        manual_binding = self._manual_binding_for(conversation_id)
        receiver = self._receiver_for(conversation_id)
        record = self.store.enqueue_file(conversation_id, path, caption=caption, receiver=receiver, manual_binding=manual_binding)
        return SendResult(
            message_id=str(record["bridge_id"]),
            conversation_id=conversation_id,
            status="queued_to_bridge",
            reason=f"queued_file_to_non_foreground_bridge:{record['bridge_id']}",
        )

    def _manual_binding_for(self, conversation_id: str) -> WeChatWindowBinding | None:
        binding = self.binding_store.get_binding(conversation_id)
        if binding is None:
            return None
        if binding.status not in ALLOWED_MANUAL_BINDING_STATUSES:
            return None
        return binding

    def _receiver_for(self, conversation_id: str) -> str:
        receiver = _channel_receiver(self.data_dir, conversation_id)
        if receiver:
            return receiver
        # Fall back to the conversation_id only when it is itself a valid wcf
        # receiver (a raw wxid/roomid). A hashed conversation_id is not, so this
        # yields "" and the send fails cleanly rather than misrouting.
        candidate = str(conversation_id or "").strip()
        return candidate if _looks_like_wechat_receiver(candidate) else ""


def bridge_state(data_dir: str | Path, *, limit: int = 30) -> dict[str, Any]:
    return BridgeOutboxStore(data_dir).state(limit=limit)


def _channel_receiver(data_dir: str | Path, conversation_id: str) -> str:
    payload = _channel_payload(data_dir, conversation_id)
    conversation_type = str(payload.get("conversation_type", "") or "") if isinstance(payload, dict) else ""
    # The persisted conversation_key is the true talker id (wxid for private,
    # roomid for groups) the conversation was hashed from — always the correct
    # receiver when present.
    conversation_key = str(payload.get("conversation_key", "") or "").strip() if isinstance(payload, dict) else ""
    if _looks_like_wechat_receiver(conversation_key):
        return conversation_key
    if conversation_type == "group":
        # For a group, only a roomid may receive the reply. sender_wechat_ids
        # holds speaking members' wxids, so falling back to those would deliver
        # the group's reply privately to a member. Require a @chatroom id.
        sender_ids = payload.get("sender_wechat_ids") if isinstance(payload, dict) else []
        if isinstance(sender_ids, list):
            for item in sender_ids:
                candidate = str(item or "").strip()
                if candidate.endswith("@chatroom"):
                    return candidate
        candidate = str(conversation_id or "").strip()
        return candidate if candidate.endswith("@chatroom") else ""
    sender_ids = payload.get("sender_wechat_ids") if isinstance(payload, dict) else []
    if isinstance(sender_ids, list):
        for item in sender_ids:
            candidate = str(item or "").strip()
            if _looks_like_wechat_receiver(candidate):
                return candidate
    candidate = str(conversation_id or "").strip()
    return candidate if _looks_like_wechat_receiver(candidate) else ""


def _channel_payload(data_dir: str | Path, conversation_id: str) -> dict[str, Any]:
    segment = _safe_segment(str(conversation_id or ""))
    path = Path(data_dir) / "conversation_channels" / segment / "channel.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _looks_like_wechat_receiver(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text.startswith("wxid_") or text.startswith("gh_") or text.endswith("@chatroom"))


def _safe_segment(value: str) -> str:
    import re

    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "default"


def bridge_ack(
    data_dir: str | Path,
    bridge_id: str,
    *,
    status: str,
    reason: str = "",
    external_message_id: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record = BridgeOutboxStore(data_dir).append_ack(
        bridge_id,
        status=status,
        reason=reason,
        external_message_id=external_message_id,
        payload=payload,
    )
    return {"status": "ok", "ack": record}


def _latest_by_bridge_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        bridge_id = str(record.get("bridge_id", ""))
        if bridge_id:
            result[bridge_id] = record
    return result


def manual_bridge_bindings(data_dir: str | Path) -> list[WeChatWindowBinding]:
    store = WeChatWindowBindingStore(data_dir)
    bindings: list[WeChatWindowBinding] = []
    for item in store.list_bindings():
        conversation_id = str(item.get("conversation_id", "")).strip()
        if not conversation_id:
            continue
        status = str(item.get("status", "active") or "active")
        if status not in ALLOWED_MANUAL_BINDING_STATUSES:
            continue
        binding = store.get_binding(conversation_id)
        if binding is not None:
            bindings.append(binding)
    return bindings


def _manual_binding_payload(binding: WeChatWindowBinding | None) -> dict[str, Any]:
    if binding is None:
        return {}
    return {
        "conversation_id": binding.conversation_id,
        "conversation_type": binding.conversation_type,
        "chat_title": binding.chat_title,
        "status": binding.status,
        "hwnd": binding.hwnd,
        "title": binding.title,
        "process_id": binding.process_id,
        "process_name": binding.process_name,
        "bound_at": binding.bound_at,
        "last_seen_at": binding.last_seen_at,
    }

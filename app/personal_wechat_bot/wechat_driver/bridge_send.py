from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.conversation.segment import resolve_segment
from app.personal_wechat_bot.domain.models import SendResult, utc_now_iso
from app.personal_wechat_bot.wechat_driver.window_binding import WeChatWindowBinding, WeChatWindowBindingStore


BRIDGE_OUTBOX_SEND_DRIVER = "bridge_outbox"
ALLOWED_MANUAL_BINDING_STATUSES = {"active", "stale"}


class BridgeAckStatus:
    SENT = "sent"
    FAILED = "failed"
    BLOCKED = "blocked"
    RETRY = "retry"
    INFLIGHT = "inflight"


BRIDGE_ACK_STATUSES = frozenset(
    {
        BridgeAckStatus.SENT,
        BridgeAckStatus.FAILED,
        BridgeAckStatus.BLOCKED,
        BridgeAckStatus.RETRY,
        BridgeAckStatus.INFLIGHT,
    }
)
BRIDGE_TERMINAL_ACK_STATUSES = frozenset(
    {BridgeAckStatus.SENT, BridgeAckStatus.FAILED, BridgeAckStatus.BLOCKED}
)


def is_terminal_bridge_ack_status(status: str) -> bool:
    return str(status or "") in BRIDGE_TERMINAL_ACK_STATUSES


_BRIDGE_TERMINAL_ACK_PRIORITY = {
    BridgeAckStatus.FAILED: 1,
    BridgeAckStatus.BLOCKED: 2,
    BridgeAckStatus.SENT: 3,
}


@dataclass(frozen=True)
class BridgeAckState:
    bridge_id: str
    status: str
    ack: dict[str, Any]
    terminal: bool
    ack_count: int
    invalid_count: int


def resolve_bridge_ack_state(
    records: list[dict[str, Any]],
    *,
    bridge_id: str = "",
    default_status: str = "queued",
) -> BridgeAckState:
    """Resolve append-only ack lines into the effective monotonic state.

    Non-terminal markers (retry/inflight) describe work in progress. Once a
    terminal ack exists, stale non-terminal lines must never make the bridge look
    pending again. A sent ack is strongest because a real delivery cannot be
    undone by a later stale failure marker; blocked wins over failed for manual
    operator stops.
    """
    target_bridge_id = str(bridge_id or "")
    latest_valid: dict[str, Any] = {}
    best_terminal: dict[str, Any] = {}
    best_terminal_priority = 0
    valid_count = 0
    invalid_count = 0
    resolved_bridge_id = target_bridge_id

    for record in records:
        if not isinstance(record, dict):
            invalid_count += 1
            continue
        record_bridge_id = str(record.get("bridge_id", ""))
        if target_bridge_id and record_bridge_id != target_bridge_id:
            continue
        status = str(record.get("status", ""))
        if status not in BRIDGE_ACK_STATUSES:
            invalid_count += 1
            continue
        if record_bridge_id:
            resolved_bridge_id = record_bridge_id
        valid_count += 1
        latest_valid = record
        if is_terminal_bridge_ack_status(status):
            priority = _BRIDGE_TERMINAL_ACK_PRIORITY.get(status, 0)
            if priority >= best_terminal_priority:
                best_terminal = record
                best_terminal_priority = priority

    if best_terminal:
        status = str(best_terminal.get("status", default_status))
        return BridgeAckState(
            bridge_id=resolved_bridge_id,
            status=status,
            ack=best_terminal,
            terminal=True,
            ack_count=valid_count,
            invalid_count=invalid_count,
        )
    if latest_valid:
        status = str(latest_valid.get("status", default_status))
        return BridgeAckState(
            bridge_id=resolved_bridge_id,
            status=status,
            ack=latest_valid,
            terminal=False,
            ack_count=valid_count,
            invalid_count=invalid_count,
        )
    return BridgeAckState(
        bridge_id=resolved_bridge_id,
        status=default_status,
        ack={},
        terminal=False,
        ack_count=0,
        invalid_count=invalid_count,
    )


def effective_bridge_ack_states(records: list[dict[str, Any]]) -> dict[str, BridgeAckState]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        bridge_id = str(record.get("bridge_id", ""))
        if bridge_id:
            grouped.setdefault(bridge_id, []).append(record)
    return {
        bridge_id: resolve_bridge_ack_state(group, bridge_id=bridge_id)
        for bridge_id, group in grouped.items()
    }


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
        if status not in BRIDGE_ACK_STATUSES:
            raise ValueError("status must be sent, failed, blocked, retry, or inflight")
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
        ack_states = effective_bridge_ack_states(acks)
        items = []
        for item in outbox[-max(1, limit) :]:
            bridge_id = str(item.get("bridge_id", ""))
            ack_state = ack_states.get(bridge_id)
            ack = ack_state.ack if ack_state is not None else {}
            status = ack_state.status if ack_state is not None else str(item.get("status", "queued"))
            items.append({**item, "status": status, "ack": ack or {}})
        # Pending = not yet terminally acked. A "retry" ack is non-terminal, so a
        # record awaiting another delivery attempt still counts as pending.
        pending_count = sum(
            1
            for item in outbox
            if not ack_states.get(str(item.get("bridge_id", "")), BridgeAckState("", "", {}, False, 0, 0)).terminal
        )
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
        # Hold the store lock so an append can never land inside a concurrent
        # compaction's read-modify-rewrite window (which would drop this record
        # when compaction replaces the file with its pre-append snapshot).
        with self._store_lock():
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _store_lock(self):
        """Cross-process lock serializing append vs compaction of the jsonl files.

        The producer (WeFlow pull process) appends to outbox.jsonl while the
        consumer (bridge worker) periodically compacts both files. Without a
        shared lock, an append between compaction's read and its atomic replace
        is silently lost. Both sides take this short lock; it is unrelated to the
        long-lived single-instance ``.bridge_worker.lock``.
        """
        from app.personal_wechat_bot.runtime.process_lock import blocking_process_lock

        return blocking_process_lock(
            self.root / ".outbox_rw.lock",
            label="bridge_outbox_rw",
            stale_after_seconds=30.0,
            wait_timeout_seconds=15.0,
        )

    def compact(self, *, keep_resolved: int = 500) -> dict[str, int]:
        """Drop old terminally-resolved records from outbox.jsonl + acks.jsonl.

        The bridge worker re-reads both files in full on every tick, so without
        compaction they grow forever and each tick gets slower. A record is
        "resolved" once its latest ack is terminal (sent/failed/blocked); such
        records will never be delivered again, so dropping the oldest ones is
        restart-safe. The most recent ``keep_resolved`` resolved records are
        retained for history/audit, plus every not-yet-resolved (pending/retry)
        record and all acks referencing a retained bridge_id.

        Returns counts of removed outbox/ack lines. A no-op-safe rewrite: if
        nothing is droppable it leaves the files untouched.
        """
        # Hold the store lock across the whole read-modify-write so a concurrent
        # producer append is serialized (either fully before our read, or fully
        # after our replace) and can never be dropped. _rewrite does not re-lock,
        # so there is no reentrancy.
        with self._store_lock():
            outbox = self._read_all(self.outbox_path)
            acks = self._read_all(self.ack_path)
            ack_states = effective_bridge_ack_states(acks)

            resolved_order: list[str] = []
            pending_ids: set[str] = set()
            for item in outbox:
                bridge_id = str(item.get("bridge_id", ""))
                if not bridge_id:
                    continue
                ack_state = ack_states.get(bridge_id)
                if ack_state is not None and ack_state.terminal:
                    resolved_order.append(bridge_id)
                else:
                    pending_ids.add(bridge_id)

            if len(resolved_order) <= max(0, keep_resolved):
                return {"removed_outbox": 0, "removed_acks": 0}

            # Keep the most-recent resolved ids (outbox is FIFO, so tail = newest).
            keep_resolved_ids = set(resolved_order[-keep_resolved:]) if keep_resolved > 0 else set()
            keep_ids = pending_ids | keep_resolved_ids

            new_outbox = [item for item in outbox if str(item.get("bridge_id", "")) in keep_ids]
            new_acks = [ack for ack in acks if str(ack.get("bridge_id", "")) in keep_ids]
            removed_outbox = len(outbox) - len(new_outbox)
            removed_acks = len(acks) - len(new_acks)
            if removed_outbox <= 0 and removed_acks <= 0:
                return {"removed_outbox": 0, "removed_acks": 0}

            self._rewrite(self.outbox_path, new_outbox)
            self._rewrite(self.ack_path, new_acks)
            return {"removed_outbox": removed_outbox, "removed_acks": removed_acks}

    def _rewrite(self, path: Path, records: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        tmp.replace(path)

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
    # Channel dirs are named chat_title_hashPrefix, not the raw conversation_id,
    # so resolve the segment via the channel index. This is the ONLY reliable
    # path for the out-of-process send worker, which has no in-memory cache.
    segment = resolve_segment(data_dir, str(conversation_id or ""))
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


def bridge_ack(
    data_dir: str | Path,
    bridge_id: str,
    *,
    status: str,
    reason: str = "",
    external_message_id: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store = BridgeOutboxStore(data_dir)
    record = store.append_ack(
        bridge_id,
        status=status,
        reason=reason,
        external_message_id=external_message_id,
        payload=payload,
    )
    effective = bridge_ack_state(data_dir, bridge_id, store=store)
    return {
        "status": "ok",
        "ack": record,
        "effective_status": effective.status,
        "effective_ack": effective.ack,
    }


def bridge_ack_state(
    data_dir: str | Path,
    bridge_id: str,
    *,
    store: BridgeOutboxStore | None = None,
) -> BridgeAckState:
    store = store if store is not None else BridgeOutboxStore(data_dir)
    target_bridge_id = str(bridge_id or "")
    records = [
        ack
        for ack in store._read_all(store.ack_path)
        if str(ack.get("bridge_id", "")) == target_bridge_id
    ]
    return resolve_bridge_ack_state(records, bridge_id=target_bridge_id)


def _latest_by_bridge_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        bridge_id: state.ack
        for bridge_id, state in effective_bridge_ack_states(records).items()
        if state.ack
    }


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

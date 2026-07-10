from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.config.loader import load_config
from app.personal_wechat_bot.conversation.channel_admission import channel_allows_private_receiver
from app.personal_wechat_bot.conversation.channel_registry_store import ChannelRegistryStore
from app.personal_wechat_bot.domain.models import SendResult, utc_now_iso
from app.personal_wechat_bot.runtime.process_lock import process_pid_alive
from app.personal_wechat_bot.wechat_driver.send_backends import (
    wechat_native_file_send_blocker,
    wechat_native_http_status,
    weflow_http_status,
)


BRIDGE_OUTBOX_SEND_DRIVER = "bridge_outbox"
BRIDGE_WORKER_LOCK_STALE_SECONDS = 60.0
REAL_BRIDGE_SEND_BACKENDS = frozenset({"weflow_http", "wechat_native_http"})


class BridgeAckStatus:
    SENT = "sent"
    ACCEPTED = "accepted"
    FAILED = "failed"
    BLOCKED = "blocked"
    RETRY = "retry"
    INFLIGHT = "inflight"


BRIDGE_ACK_STATUSES = frozenset(
    {
        BridgeAckStatus.SENT,
        BridgeAckStatus.ACCEPTED,
        BridgeAckStatus.FAILED,
        BridgeAckStatus.BLOCKED,
        BridgeAckStatus.RETRY,
        BridgeAckStatus.INFLIGHT,
    }
)
BRIDGE_TERMINAL_ACK_STATUSES = frozenset(
    {BridgeAckStatus.SENT, BridgeAckStatus.ACCEPTED, BridgeAckStatus.FAILED, BridgeAckStatus.BLOCKED}
)


def is_terminal_bridge_ack_status(status: str) -> bool:
    return str(status or "") in BRIDGE_TERMINAL_ACK_STATUSES


_BRIDGE_TERMINAL_ACK_PRIORITY = {
    BridgeAckStatus.FAILED: 1,
    BridgeAckStatus.BLOCKED: 2,
    BridgeAckStatus.ACCEPTED: 3,
    BridgeAckStatus.SENT: 4,
}

_NON_RETRYABLE_FAILED_REASON_MARKERS = (
    "possible_duplicate",
    "unknown_delivery_state",
    "timeout",
    "timed_out",
    "manual_sidebar_failed",
    "manual_block",
    "file_not_found",
    "deliver_exception",
    "unsupported",
    "http_404",
    "not_found",
)


def _normalize_send_backend(value: str) -> str:
    return str(value or "dry_run").strip().lower()


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
        best_terminal = _normalize_terminal_ack(best_terminal)
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


def _normalize_terminal_ack(record: dict[str, Any]) -> dict[str, Any]:
    status = str(record.get("status", ""))
    if status == BridgeAckStatus.SENT and _sent_ack_is_accepted_unverified(record):
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        normalized_payload = {
            **payload,
            "delivery_verified": False,
            "accepted_unverified": True,
            "normalized_from_status": BridgeAckStatus.SENT,
        }
        return {
            **record,
            "status": BridgeAckStatus.ACCEPTED,
            "original_status": BridgeAckStatus.SENT,
            "payload": normalized_payload,
        }
    return record


def _sent_ack_is_accepted_unverified(record: dict[str, Any]) -> bool:
    reason = str(record.get("reason", "")).strip().lower()
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    backend = str(payload.get("backend", "")).strip().lower()
    operation = str(payload.get("operation", "")).strip().lower()
    if not (
        reason.startswith("wechat_native_http_send_")
        or backend == "wechat_native_http"
        or operation.startswith("wechat_native_http_send_")
    ):
        return False
    if _ack_payload_delivery_verified(payload):
        return False
    return True


def _ack_payload_delivery_verified(payload: dict[str, Any]) -> bool:
    sources = [payload]
    response = payload.get("response")
    if isinstance(response, dict):
        sources.append(response)
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        sources.append(data)
    for source in sources:
        for key in ("delivery_verified", "verified_delivery", "wechat_delivery_verified"):
            if source.get(key) is True:
                return True
        if source.get("verified") is True and source.get("delivery") is True:
            return True
    return False


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


def _bridge_ack_backend(ack: dict[str, Any]) -> str:
    payload = ack.get("payload") if isinstance(ack.get("payload"), dict) else {}
    backend = str(payload.get("backend") or "").strip()
    if backend:
        return backend
    reason = str(ack.get("reason") or "")
    for marker in ("wechat_native_http", "wechat_hook_http", "weflow_http", "dry_run"):
        if marker in reason:
            return marker
    return "queued" if not ack else "unknown"


def _bridge_ack_delivery_verified(ack: dict[str, Any]) -> bool:
    payload = ack.get("payload") if isinstance(ack.get("payload"), dict) else {}
    if payload.get("delivery_verified") is True:
        return True
    verification = payload.get("delivery_verification") if isinstance(payload.get("delivery_verification"), dict) else {}
    return verification.get("verified") is True


def _bridge_ack_accepted_unverified(ack: dict[str, Any]) -> bool:
    if not ack:
        return False
    payload = ack.get("payload") if isinstance(ack.get("payload"), dict) else {}
    if payload.get("accepted_unverified") is True:
        return True
    return str(ack.get("status") or "") == BridgeAckStatus.ACCEPTED and not _bridge_ack_delivery_verified(ack)


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
    authorization: str
    blockers: list[str]
    backend: dict[str, Any] = field(default_factory=dict)


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
            "transport": "send_bridge",
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
    ) -> dict[str, Any]:
        bridge_id = f"bridge:{conversation_id}:{uuid.uuid4().hex[:12]}"
        resolved_path = str(Path(path).expanduser().resolve())
        record = {
            "bridge_id": bridge_id,
            "conversation_id": conversation_id,
            "receiver": receiver,
            "kind": "file",
            "path": resolved_path,
            "name": name or Path(resolved_path).name,
            "caption": caption,
            "status": "queued",
            "created_at": utc_now_iso(),
            "transport": "send_bridge",
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
            raise ValueError("status must be sent, accepted, failed, blocked, retry, or inflight")
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

    def requeue_resolved(self, bridge_id: str, *, reason: str = "manual_bridge_retry") -> dict[str, Any]:
        """Create a fresh outbox record for a resolved item that never landed.

        A terminal ack normally makes a record restart-safe and non-retryable.
        That is still the default. This explicit recovery path is for known
        non-delivery states: failed bridge attempts, or dry-run "sent" records
        whose ack reason says no WeChat delivery happened. It never mutates the
        old record or acks; the new bridge_id becomes the delivery attempt.
        """

        original_id = str(bridge_id or "").strip()
        if not original_id:
            raise ValueError("bridge_id is required")
        outbox = self._read_all(self.outbox_path)
        acks = self._read_all(self.ack_path)
        original = next((item for item in outbox if str(item.get("bridge_id", "")) == original_id), None)
        if original is None:
            raise KeyError(f"bridge_id not found: {original_id}")
        ack_state = effective_bridge_ack_states(acks).get(original_id)
        if ack_state is None or not ack_state.terminal:
            raise ValueError("bridge item is not terminal; wait for the current attempt to finish")
        retryable, retry_reason = bridge_item_retryable(ack_state.status, str(ack_state.ack.get("reason", "")))
        if not retryable:
            raise ValueError(retry_reason or f"bridge item is not retryable from status {ack_state.status}")

        conversation_id = str(original.get("conversation_id", ""))
        kind = str(original.get("kind", "text") or "text")
        receiver = str(original.get("receiver") or "").strip() or _channel_receiver(self.data_dir, conversation_id)
        retry_metadata = {
            "retry_of": original_id,
            "retry_reason": str(reason or "manual_bridge_retry"),
            "previous_status": ack_state.status,
            "previous_reason": str(ack_state.ack.get("reason", "")),
        }
        if kind == "file":
            record = self.enqueue_file(
                conversation_id,
                str(original.get("path", "")),
                name=str(original.get("name", "")),
                caption=str(original.get("caption", "")),
                receiver=receiver,
            )
        else:
            record = self.enqueue(conversation_id, str(original.get("text", "")), receiver=receiver)
        record = {**record, **retry_metadata}
        # The append above wrote the minimal record. Rewrite only the last line's
        # metadata by appending a sidecar ack-like marker would complicate state,
        # so store retry metadata in the outbox record through a small rewrite.
        self._patch_outbox_record(str(record["bridge_id"]), retry_metadata)
        return record

    def _patch_outbox_record(self, bridge_id: str, patch: dict[str, Any]) -> None:
        with self._store_lock():
            records = self._read_all(self.outbox_path)
            changed = False
            for index, item in enumerate(records):
                if str(item.get("bridge_id", "")) != bridge_id:
                    continue
                records[index] = {**item, **patch}
                changed = True
                break
            if changed:
                self._rewrite(self.outbox_path, records)

    def state(self, *, limit: int = 30) -> dict[str, Any]:
        outbox = self._read_all(self.outbox_path)
        acks = self._read_all(self.ack_path)
        ack_states = effective_bridge_ack_states(acks)
        effective_status_counts = {
            BridgeAckStatus.SENT: 0,
            BridgeAckStatus.ACCEPTED: 0,
            BridgeAckStatus.FAILED: 0,
            BridgeAckStatus.BLOCKED: 0,
            BridgeAckStatus.RETRY: 0,
            BridgeAckStatus.INFLIGHT: 0,
            "queued": 0,
        }
        status_counts_by_backend: dict[str, dict[str, int]] = {}
        items = []
        resolved_items: list[dict[str, Any]] = []
        for item in outbox:
            bridge_id = str(item.get("bridge_id", ""))
            ack_state = ack_states.get(bridge_id)
            ack = ack_state.ack if ack_state is not None else {}
            status = ack_state.status if ack_state is not None else str(item.get("status", "queued"))
            retryable, retry_reason = bridge_item_retryable(status, str(ack.get("reason", "")))
            effective_status_counts[status] = effective_status_counts.get(status, 0) + 1
            ack_backend = _bridge_ack_backend(ack)
            backend_counts = status_counts_by_backend.setdefault(ack_backend, {})
            backend_counts[status] = backend_counts.get(status, 0) + 1
            resolved_items.append(
                {
                    **item,
                    "status": status,
                    "ack": ack or {},
                    "ack_backend": ack_backend,
                    "delivery_verified": _bridge_ack_delivery_verified(ack),
                    "accepted_unverified": _bridge_ack_accepted_unverified(ack),
                    "retryable": retryable,
                    "retry_blocker": "" if retryable else retry_reason,
                }
            )
        items = resolved_items[-max(1, limit) :]
        # Pending = not yet terminally acked. A "retry" ack is non-terminal, so a
        # record awaiting another delivery attempt still counts as pending.
        pending_count = sum(
            1
            for item in outbox
            if not ack_states.get(str(item.get("bridge_id", "")), BridgeAckState("", "", {}, False, 0, 0)).terminal
        )
        open_problem_count = (
            effective_status_counts.get(BridgeAckStatus.ACCEPTED, 0)
            + effective_status_counts.get(BridgeAckStatus.BLOCKED, 0)
            + effective_status_counts.get(BridgeAckStatus.RETRY, 0)
            + effective_status_counts.get(BridgeAckStatus.INFLIGHT, 0)
        )
        unverified_by_backend = {
            backend: counts.get(BridgeAckStatus.ACCEPTED, 0)
            for backend, counts in status_counts_by_backend.items()
            if counts.get(BridgeAckStatus.ACCEPTED, 0)
        }
        legacy_hook_unverified_count = unverified_by_backend.get("wechat_hook_http", 0)
        active_unverified_count = max(
            0,
            effective_status_counts.get(BridgeAckStatus.ACCEPTED, 0) - legacy_hook_unverified_count,
        )
        active_problem_count = (
            active_unverified_count
            + effective_status_counts.get(BridgeAckStatus.BLOCKED, 0)
            + effective_status_counts.get(BridgeAckStatus.RETRY, 0)
            + effective_status_counts.get(BridgeAckStatus.INFLIGHT, 0)
        )
        return {
            "status": "ok",
            "driver": BRIDGE_OUTBOX_SEND_DRIVER,
            "outbox_path": str(self.outbox_path),
            "ack_path": str(self.ack_path),
            "count": len(outbox),
            "pending_count": pending_count,
            "ack_count": len(acks),
            "ack_line_count": len(acks),
            "terminal_count": sum(
                effective_status_counts.get(status, 0)
                for status in BRIDGE_TERMINAL_ACK_STATUSES
            ),
            "sent_count": effective_status_counts.get(BridgeAckStatus.SENT, 0),
            "accepted_count": effective_status_counts.get(BridgeAckStatus.ACCEPTED, 0),
            "unverified_count": effective_status_counts.get(BridgeAckStatus.ACCEPTED, 0),
            "failed_count": effective_status_counts.get(BridgeAckStatus.FAILED, 0),
            "blocked_count": effective_status_counts.get(BridgeAckStatus.BLOCKED, 0),
            "retry_count": effective_status_counts.get(BridgeAckStatus.RETRY, 0),
            "inflight_count": effective_status_counts.get(BridgeAckStatus.INFLIGHT, 0),
            "queued_count": effective_status_counts.get("queued", 0),
            "open_problem_count": open_problem_count,
            "active_problem_count": active_problem_count,
            "active_unverified_count": active_unverified_count,
            "legacy_hook_unverified_count": legacy_hook_unverified_count,
            "unverified_by_backend": unverified_by_backend,
            "historical_failed_count": effective_status_counts.get(BridgeAckStatus.FAILED, 0),
            "effective_status_counts": effective_status_counts,
            "status_counts_by_backend": status_counts_by_backend,
            "items": items,
            "contract": {
                "producer": "agent writes outbox.jsonl (text + file records)",
                "authorization": "conversation whitelist enforced upstream by the router",
                "consumer": "send_bridge_worker delivers via the configured backend (no foreground) and writes acks.jsonl",
                "delivery_claim": "sent means delivery verified by backend; accepted means local endpoint accepted but WeChat delivery is unverified",
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

    def __init__(
        self,
        *,
        send_enabled: bool,
        data_dir: str | Path = "data",
        send_backend: str = "dry_run",
        weflow_base_url: str = "http://127.0.0.1:5031",
        weflow_token_env: str = "WEFLOW_API_TOKEN",
        weflow_send_text_path: str = "/send/text",
        weflow_send_file_path: str = "/send/file",
        weflow_send_timeout_seconds: float = 35.0,
        wechat_native_base_url: str = "http://127.0.0.1:30001",
        wechat_native_send_text_path: str = "/SendTextMsg",
        wechat_native_send_image_path: str = "/SendImgMsg",
        wechat_native_send_file_path: str = "/send_file_msg",
        wechat_native_status_path: str = "/QueryDB/status",
        wechat_native_timeout_seconds: float = 15.0,
        wechat_native_verify_timeout_seconds: float = 10.0,
        wechat_native_file_verify_timeout_seconds: float = 45.0,
    ):
        self.send_enabled = send_enabled
        self.data_dir = Path(data_dir)
        self.send_backend = _normalize_send_backend(send_backend)
        self.weflow_base_url = str(weflow_base_url or "http://127.0.0.1:5031")
        self.weflow_token_env = str(weflow_token_env or "WEFLOW_API_TOKEN")
        self.weflow_send_text_path = str(weflow_send_text_path or "/send/text")
        self.weflow_send_file_path = str(weflow_send_file_path or "/send/file")
        self.weflow_send_timeout_seconds = max(1.0, float(weflow_send_timeout_seconds or 35.0))
        self.wechat_native_base_url = str(wechat_native_base_url or "http://127.0.0.1:30001")
        self.wechat_native_send_text_path = str(wechat_native_send_text_path or "/SendTextMsg")
        self.wechat_native_send_image_path = str(wechat_native_send_image_path or "/SendImgMsg")
        self.wechat_native_send_file_path = str(wechat_native_send_file_path or "/send_file_msg")
        self.wechat_native_status_path = str(wechat_native_status_path or "/QueryDB/status")
        self.wechat_native_timeout_seconds = max(1.0, float(wechat_native_timeout_seconds or 15.0))
        self.wechat_native_verify_timeout_seconds = max(0.0, float(wechat_native_verify_timeout_seconds or 0.0))
        self.wechat_native_file_verify_timeout_seconds = max(0.0, float(wechat_native_file_verify_timeout_seconds or 0.0))
        self.store = BridgeOutboxStore(data_dir)

    def health_check(self) -> bool:
        return self.store.root.exists()

    def probe(self) -> BridgeSendProbe:
        state = self.store.state(limit=1)
        blockers = [] if self.send_enabled else ["send_enabled_false"]
        backend = {
            "send_backend": self.send_backend,
            "weflow_http": {},
            "weflow_base_url": self.weflow_base_url,
            "weflow_token_env": self.weflow_token_env,
            "weflow_send_text_path": self.weflow_send_text_path,
            "weflow_send_file_path": self.weflow_send_file_path,
            "wechat_native_http": {},
            "wechat_native_base_url": self.wechat_native_base_url,
            "wechat_native_send_text_path": self.wechat_native_send_text_path,
            "wechat_native_send_image_path": self.wechat_native_send_image_path,
            "wechat_native_send_file_path": self.wechat_native_send_file_path,
            "wechat_native_status_path": self.wechat_native_status_path,
        }
        worker_config = self._worker_lock_config_status()
        backend["worker_config"] = worker_config
        worker_blocker = self._worker_config_blocker(worker_config)
        if self.send_enabled and worker_blocker:
            blockers.append(worker_blocker)
        if self.send_backend == "weflow_http":
            backend["weflow_http"] = weflow_http_status(
                self.weflow_base_url,
                token_env=self.weflow_token_env,
                timeout_seconds=min(self.weflow_send_timeout_seconds, 3.0),
            )
            if self.send_enabled and not backend["weflow_http"].get("available"):
                blockers.append("weflow_http_unavailable")
            elif self.send_enabled and not backend["weflow_http"].get("token_present"):
                blockers.append("weflow_http_token_missing")
            elif self.send_enabled:
                capability_blocker = _weflow_capability_blocker(backend["weflow_http"], kind="text")
                if capability_blocker:
                    blockers.append(capability_blocker)
        if self.send_backend == "wechat_native_http":
            backend["wechat_native_http"] = wechat_native_http_status(
                self.wechat_native_base_url,
                text_path=self.wechat_native_send_text_path,
                image_path=self.wechat_native_send_image_path,
                file_path=self.wechat_native_send_file_path,
                status_path=self.wechat_native_status_path,
                timeout_seconds=min(self.wechat_native_timeout_seconds, 3.0),
            )
            if self.send_enabled and not backend["wechat_native_http"].get("available"):
                blockers.append("wechat_native_http_unavailable")
        return BridgeSendProbe(
            driver=BRIDGE_OUTBOX_SEND_DRIVER,
            implemented=True,
            send_enabled=self.send_enabled,
            health="ready" if not blockers else "blocked",
            outbox_path=str(self.store.outbox_path),
            ack_path=str(self.store.ack_path),
            pending_count=int(state.get("pending_count", 0) or 0),
            ack_count=int(state.get("ack_count", 0) or 0),
            authorization="conversation_whitelist",
            blockers=blockers,
            backend=backend,
        )

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        if not self.send_enabled:
            return SendResult("bridge-outbox-send", conversation_id, "failed", "send_enabled_false")
        backend_blocker = self._backend_blocker(kind="text")
        if backend_blocker:
            return SendResult("bridge-outbox-send", conversation_id, "failed", backend_blocker)
        if not text.strip():
            return SendResult("bridge-outbox-send", conversation_id, "failed", "empty_reply")
        receiver = self._receiver_for(conversation_id)
        receiver_blocker = _receiver_authorization_blocker(
            self.data_dir,
            conversation_id,
            receiver,
            backend_name=self.send_backend,
        )
        if receiver_blocker:
            return SendResult("bridge-outbox-send", conversation_id, "failed", receiver_blocker)
        record = self.store.enqueue(conversation_id, text, receiver=receiver)
        return SendResult(
            message_id=str(record["bridge_id"]),
            conversation_id=conversation_id,
            status="queued_to_bridge",
            reason=f"queued_to_non_foreground_bridge:{record['bridge_id']}",
        )

    def send_file(self, conversation_id: str, path: str, caption: str = "") -> SendResult:
        if not self.send_enabled:
            return SendResult("bridge-outbox-send", conversation_id, "failed", "send_enabled_false")
        backend_blocker = self._backend_blocker(kind="file", path=path)
        if backend_blocker:
            return SendResult("bridge-outbox-send", conversation_id, "failed", backend_blocker)
        if not str(path).strip():
            return SendResult("bridge-outbox-send", conversation_id, "failed", "empty_file_path")
        receiver = self._receiver_for(conversation_id)
        receiver_blocker = _receiver_authorization_blocker(
            self.data_dir,
            conversation_id,
            receiver,
            backend_name=self.send_backend,
        )
        if receiver_blocker:
            return SendResult("bridge-outbox-send", conversation_id, "failed", receiver_blocker)
        record = self.store.enqueue_file(conversation_id, path, caption=caption, receiver=receiver)
        return SendResult(
            message_id=str(record["bridge_id"]),
            conversation_id=conversation_id,
            status="queued_to_bridge",
            reason=f"queued_file_to_non_foreground_bridge:{record['bridge_id']}",
        )

    def _receiver_for(self, conversation_id: str) -> str:
        receiver = _channel_receiver(self.data_dir, conversation_id)
        if receiver:
            return receiver
        # Fall back to the conversation_id only when it is itself a valid WeChat
        # receiver (a raw wxid/roomid). A hashed conversation_id is not, so this
        # yields "" and the send fails cleanly rather than misrouting.
        candidate = str(conversation_id or "").strip()
        if _real_bridge_backend(self.send_backend) and _looks_like_private_wechat_receiver(candidate):
            return ""
        return candidate if _looks_like_wechat_receiver(candidate) else ""

    def _backend_blocker(self, *, kind: str = "text", path: str = "") -> str:
        worker_blocker = self._worker_config_blocker()
        if worker_blocker:
            return worker_blocker
        if self.send_backend == "weflow_http":
            status = weflow_http_status(
                self.weflow_base_url,
                token_env=self.weflow_token_env,
                timeout_seconds=min(self.weflow_send_timeout_seconds, 3.0),
            )
            if not status.get("available"):
                return f"weflow_backend_unavailable:{status.get('reason') or 'weflow_http_unavailable'}"
            if not status.get("token_present"):
                return "weflow_backend_unavailable:weflow_token_missing"
            capability_blocker = _weflow_capability_blocker(status, kind=kind)
            if capability_blocker:
                return capability_blocker
            return ""
        if self.send_backend == "wechat_native_http":
            status = wechat_native_http_status(
                self.wechat_native_base_url,
                text_path=self.wechat_native_send_text_path,
                image_path=self.wechat_native_send_image_path,
                file_path=self.wechat_native_send_file_path,
                status_path=self.wechat_native_status_path,
                timeout_seconds=min(self.wechat_native_timeout_seconds, 3.0),
            )
            if not status.get("available"):
                return f"wechat_native_backend_unavailable:{status.get('reason') or 'wechat_native_http_unavailable'}"
            if str(kind or "").lower() == "file":
                media_blocker = wechat_native_file_send_blocker(
                    path,
                    image_path=self.wechat_native_send_image_path,
                    file_path=self.wechat_native_send_file_path,
                )
                if media_blocker:
                    return media_blocker
            return ""
        if self.send_backend in {"", "dry_run", "dryrun", "mock"}:
            return ""
        return f"send_backend_unsupported:{self.send_backend}"

    def _worker_config_blocker(self, status: dict[str, Any] | None = None) -> str:
        status = status if isinstance(status, dict) else self._worker_lock_config_status()
        config_status = str(status.get("config_status") or "")
        if config_status == "stale":
            backend = str(status.get("backend_name") or "")
            expected = str(status.get("expected_backend") or self.send_backend)
            return f"bridge_worker_stale_config:worker_backend={backend or 'unknown'}:expected_backend={expected or 'unknown'}"
        if config_status == "unknown_legacy_lock":
            return "bridge_worker_config_unknown"
        return ""

    def _worker_lock_config_status(self) -> dict[str, Any]:
        path = self.store.root / ".bridge_worker.lock"
        if not path.exists():
            return {"running": False, "config_status": "not_running"}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"running": False, "config_status": "not_running"}
        if not isinstance(payload, dict):
            return {"running": False, "config_status": "not_running"}
        heartbeat = payload.get("heartbeat_at")
        try:
            heartbeat_age = time.time() - float(heartbeat)
        except (TypeError, ValueError):
            heartbeat_age = BRIDGE_WORKER_LOCK_STALE_SECONDS + 1
        if heartbeat_age > BRIDGE_WORKER_LOCK_STALE_SECONDS:
            return {
                "running": False,
                "config_status": "not_running",
                "heartbeat_age_seconds": heartbeat_age,
            }
        try:
            pid = int(payload.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0 and not process_pid_alive(pid):
            return {
                "running": False,
                "config_status": "not_running",
                "pid": pid,
                "pid_alive": False,
            }
        expected = self._worker_expected_config_signature()
        actual = payload.get("config_signature") if isinstance(payload.get("config_signature"), dict) else {}
        if actual:
            config_match = actual == expected
            config_status = "matched" if config_match else "stale"
        else:
            config_match = None
            config_status = "unknown_legacy_lock"
        return {
            "running": True,
            "config_status": config_status,
            "config_match": config_match,
            "pid": pid,
            "pid_alive": bool(pid <= 0 or process_pid_alive(pid)),
            "backend_name": str(payload.get("backend_name") or ""),
            "expected_backend": self.send_backend,
            "heartbeat_age_seconds": heartbeat_age,
        }

    def _worker_expected_config_signature(self) -> dict[str, Any]:
        return {
            "send_enabled": bool(self.send_enabled),
            "send_driver": BRIDGE_OUTBOX_SEND_DRIVER,
            "send_backend": self.send_backend,
            "weflow_base_url": self.weflow_base_url,
            "weflow_token_env": self.weflow_token_env,
            "weflow_send_text_path": self.weflow_send_text_path,
            "weflow_send_file_path": self.weflow_send_file_path,
            "weflow_send_timeout_seconds": self.weflow_send_timeout_seconds,
            "wechat_native_base_url": self.wechat_native_base_url,
            "wechat_native_send_text_path": self.wechat_native_send_text_path,
            "wechat_native_send_image_path": self.wechat_native_send_image_path,
            "wechat_native_send_file_path": self.wechat_native_send_file_path,
            "wechat_native_status_path": self.wechat_native_status_path,
            "wechat_native_timeout_seconds": self.wechat_native_timeout_seconds,
            "wechat_native_verify_timeout_seconds": self.wechat_native_verify_timeout_seconds,
            "wechat_native_file_verify_timeout_seconds": self.wechat_native_file_verify_timeout_seconds,
        }


def bridge_state(data_dir: str | Path, *, limit: int = 30) -> dict[str, Any]:
    return BridgeOutboxStore(data_dir).state(limit=limit)


def bridge_requeue_resolved(data_dir: str | Path, bridge_id: str, *, reason: str = "manual_bridge_retry") -> dict[str, Any]:
    store = BridgeOutboxStore(data_dir)
    record = store.requeue_resolved(bridge_id, reason=reason)
    return {
        "status": "ok",
        "old_bridge_id": str(bridge_id or "").strip(),
        "new_bridge_id": str(record.get("bridge_id", "")),
        "record": record,
        "state": store.state(limit=30),
    }


def bridge_item_retryable(status: str, reason: str) -> tuple[bool, str]:
    status = str(status or "").strip()
    reason = str(reason or "")
    lowered = reason.lower()
    if status == BridgeAckStatus.FAILED:
        if any(marker in lowered for marker in _NON_RETRYABLE_FAILED_REASON_MARKERS):
            return False, "failed item may have unknown delivery state or needs operator review"
        return True, ""
    if status == BridgeAckStatus.SENT and "dry_run_not_delivered" in lowered:
        return True, ""
    if status == BridgeAckStatus.ACCEPTED:
        return False, "accepted item may already be delivered; wait for verification and do not re-send"
    if status in {"queued", BridgeAckStatus.RETRY, BridgeAckStatus.INFLIGHT}:
        return False, "bridge item is still pending"
    if status == BridgeAckStatus.SENT:
        return False, "bridge item is already marked sent"
    if status == BridgeAckStatus.BLOCKED:
        return False, "bridge item is blocked"
    return False, "bridge item status is not retryable"


def _weflow_capability_blocker(status: dict[str, Any], *, kind: str) -> str:
    capabilities = status.get("send_capabilities") if isinstance(status.get("send_capabilities"), dict) else {}
    key = "file" if str(kind or "").lower() == "file" else "text"
    capability = capabilities.get(key) if isinstance(capabilities.get(key), dict) else {}
    if capability.get("supports") is False:
        backend = str(capabilities.get("backend") or "unknown")
        return f"weflow_backend_unavailable:weflow_{key}_send_not_supported:{backend}"
    return ""


def _channel_receiver(data_dir: str | Path, conversation_id: str) -> str:
    payload = _channel_payload(data_dir, conversation_id)
    conversation_type = str(payload.get("conversation_type", "") or "") if isinstance(payload, dict) else ""
    if conversation_type == "private" and not channel_allows_private_receiver(payload, _load_config_or_none(data_dir)):
        return ""
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


def _receiver_authorization_blocker(
    data_dir: str | Path,
    conversation_id: str,
    receiver: str,
    *,
    backend_name: str,
) -> str:
    if not _real_bridge_backend(backend_name):
        return ""
    receiver = str(receiver or "").strip()
    conversation_id = str(conversation_id or "").strip()
    if receiver.lower() == "filehelper":
        return "" if conversation_id.lower() == "filehelper" else "receiver_not_authorized:receiver_channel_mismatch"
    payload = _channel_payload(data_dir, conversation_id)
    if not receiver:
        if _looks_like_private_wechat_receiver(conversation_id):
            return "receiver_not_authorized:missing_channel"
        if isinstance(payload, dict) and str(payload.get("conversation_type", "") or "") == "private":
            return "receiver_not_authorized:private_contact_unknown_or_unidentified"
        return "missing_receiver"
    if not _looks_like_wechat_receiver(receiver):
        return "receiver_not_authorized:invalid_receiver"
    if not payload:
        return "receiver_not_authorized:missing_channel"

    conversation_type = str(payload.get("conversation_type", "") or "").strip().lower()
    if receiver.endswith("@chatroom") and conversation_type != "group":
        return "receiver_not_authorized:receiver_channel_mismatch"
    if _looks_like_private_wechat_receiver(receiver):
        if conversation_type != "private":
            return "receiver_not_authorized:receiver_channel_mismatch"
        if not channel_allows_private_receiver(payload, _load_config_or_none(data_dir)):
            return "receiver_not_authorized:private_contact_unknown_or_unidentified"

    registered_receiver = _channel_receiver(data_dir, conversation_id)
    if not registered_receiver:
        return "receiver_not_authorized:missing_registered_receiver"
    if receiver != registered_receiver:
        return "receiver_not_authorized:receiver_channel_mismatch"
    return ""


def _load_config_or_none(data_dir: str | Path) -> Any:
    try:
        return load_config(data_dir)
    except Exception:
        return None


def _real_bridge_backend(value: str) -> bool:
    return str(value or "").strip().lower() in REAL_BRIDGE_SEND_BACKENDS


def _looks_like_private_wechat_receiver(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text.startswith(("wxid_", "gh_")))


def _channel_payload(data_dir: str | Path, conversation_id: str) -> dict[str, Any]:
    try:
        return ChannelRegistryStore(data_dir).get(conversation_id) or {}
    except Exception:
        return {}


def _looks_like_wechat_receiver(value: str) -> bool:
    text = str(value or "").strip()
    return bool(
        text.lower() == "filehelper"
        or text.startswith("wxid_")
        or text.startswith("gh_")
        or text.endswith("@chatroom")
    )


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

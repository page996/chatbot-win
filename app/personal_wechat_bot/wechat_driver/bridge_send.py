from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.domain.models import SendResult, utc_now_iso


BRIDGE_OUTBOX_SEND_DRIVER = "bridge_outbox"


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
    blockers: list[str]


class BridgeOutboxStore:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir) / "send_bridge"
        self.outbox_path = self.root / "outbox.jsonl"
        self.ack_path = self.root / "acks.jsonl"
        self.root.mkdir(parents=True, exist_ok=True)

    def enqueue(self, conversation_id: str, text: str) -> dict[str, Any]:
        bridge_id = f"bridge:{conversation_id}:{uuid.uuid4().hex[:12]}"
        record = {
            "bridge_id": bridge_id,
            "conversation_id": conversation_id,
            "text": text,
            "status": "queued",
            "created_at": utc_now_iso(),
            "transport": "external_bridge_required",
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
            "items": items,
            "contract": {
                "producer": "agent writes outbox.jsonl",
                "consumer": "external non-foreground bridge sends to WeChat and writes acks.jsonl or POSTs ack API",
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
        self.store = BridgeOutboxStore(data_dir)

    def health_check(self) -> bool:
        return self.store.root.exists()

    def probe(self) -> BridgeSendProbe:
        state = self.store.state(limit=1)
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
            blockers=blockers,
        )

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        if not self.send_enabled:
            return SendResult("bridge-outbox-send", conversation_id, "failed", "send_enabled_false")
        if not text.strip():
            return SendResult("bridge-outbox-send", conversation_id, "failed", "empty_reply")
        record = self.store.enqueue(conversation_id, text)
        return SendResult(
            message_id=str(record["bridge_id"]),
            conversation_id=conversation_id,
            status="queued_to_bridge",
            reason=f"queued_to_non_foreground_bridge:{record['bridge_id']}",
        )


def bridge_state(data_dir: str | Path, *, limit: int = 30) -> dict[str, Any]:
    return BridgeOutboxStore(data_dir).state(limit=limit)


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

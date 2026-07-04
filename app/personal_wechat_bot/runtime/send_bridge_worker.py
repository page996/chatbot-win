"""Outbox bridge worker: delivers queued messages to WeChat without foreground.

The reply gate / send drivers only *queue* outgoing messages into
``<data_dir>/send_bridge/outbox.jsonl``. This worker is the consumer half: it
reads the outbox, delivers each not-yet-acked record through a
:class:`SendBackend` (WeChatFerry in production, dry-run in tests), writes an ack
to ``acks.jsonl``, and syncs the confirm queue + conversation ledger so the UI
and history reflect real delivery.

Design constraints (see the WeFlow concurrency model memory note):

* **Single instance.** Guarded by a :class:`ProcessLock`, so two bridges never
  double-send the same outbox lines.
* **Serialized sends.** WeChatFerry's ``send_*`` is not thread-safe, so this
  worker delivers strictly one record at a time, in outbox (FIFO) order. That
  also preserves per-conversation ordering for free and interleaves fairly
  across conversations.
* **Restart-safe.** The "already delivered" cursor is derived from terminal acks
  in ``acks.jsonl`` (``sent``/``failed``), not from in-memory state, so a restart
  never re-sends an already-acked record.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.config.loader import load_config
from app.personal_wechat_bot.control.send_commands import sync_bridge_ack_to_send_state
from app.personal_wechat_bot.runtime.process_lock import ProcessLock, ProcessLockError
from app.personal_wechat_bot.wechat_driver.bridge_send import BridgeOutboxStore
from app.personal_wechat_bot.wechat_driver.send_backends import SendBackend, build_send_backend

logger = logging.getLogger(__name__)

# A record is "done" (removed from the pending set) only on a terminal ack.
_TERMINAL_ACK_STATUSES = {"sent", "failed"}

# Substrings marking a transient failure: the backend/receiver may recover, so
# the record is left pending and retried on a later tick rather than dropped.
# Anything not matching is treated as permanent (terminal failed).
_RETRYABLE_REASON_MARKERS = (
    "unavailable",
    "connect_failed",
    "timeout",
    "timed_out",
    "temporarily",
    "econnreset",
    "connection",
    "not_login",
    "is_login",
    "missing_receiver",
)

# How many times a single record may be retried across ticks before giving up
# and writing a terminal failed ack (prevents an unreachable backend from
# retrying one record forever).
_MAX_CROSS_TICK_RETRIES = 8


def _is_retryable_failure(reason: str) -> bool:
    lowered = str(reason or "").lower()
    return any(marker in lowered for marker in _RETRYABLE_REASON_MARKERS)


@dataclass
class BridgeWorkerStats:
    ticks: int = 0
    delivered: int = 0
    failed: int = 0
    skipped: int = 0
    last_error: str = ""
    reasons: dict[str, int] = field(default_factory=dict)

    def record(self, status: str, reason: str = "") -> None:
        if status == "sent":
            self.delivered += 1
        elif status == "failed":
            self.failed += 1
        else:
            self.skipped += 1
        if reason:
            self.reasons[reason] = self.reasons.get(reason, 0) + 1


class BridgeWorker:
    """Consume the outbox and deliver each record through a send backend."""

    def __init__(
        self,
        data_dir: str | Path,
        backend: SendBackend,
        *,
        max_send_attempts: int = 3,
    ):
        self.data_dir = Path(data_dir)
        self.backend = backend
        self.max_send_attempts = max(1, int(max_send_attempts))
        self.store = BridgeOutboxStore(self.data_dir)
        self.stats = BridgeWorkerStats()

    def _acked_bridge_ids(self) -> set[str]:
        acked: set[str] = set()
        for ack in self.store._read_all(self.store.ack_path):
            bridge_id = str(ack.get("bridge_id", ""))
            if bridge_id and str(ack.get("status", "")) in _TERMINAL_ACK_STATUSES:
                acked.add(bridge_id)
        return acked

    def pending_records(self) -> list[dict[str, Any]]:
        """Outbox records with no terminal ack yet, in FIFO order."""
        acked = self._acked_bridge_ids()
        pending: list[dict[str, Any]] = []
        for record in self.store._read_all(self.store.outbox_path):
            bridge_id = str(record.get("bridge_id", ""))
            if not bridge_id or bridge_id in acked:
                continue
            pending.append(record)
        return pending

    def run_once(self) -> int:
        """Deliver all currently-pending records. Returns the count processed."""
        self.stats.ticks += 1
        # Re-sync any terminal acks whose ledger/confirm-queue sync previously
        # failed, so a transient state-write error becomes eventually consistent.
        self._reconcile_unsynced_acks()
        processed = 0
        for record in self.pending_records():
            bridge_id = str(record.get("bridge_id", ""))
            try:
                self._deliver(record)
            except Exception as exc:
                # A poison record (e.g. an embedded-NUL path raising ValueError
                # deep in the backend) must not crash the whole worker and
                # re-crash every restart. Quarantine it with a terminal ack so
                # the FIFO queue advances past it.
                reason = f"deliver_exception:{type(exc).__name__}:{exc}"
                logger.exception("bridge deliver crashed for %s; quarantining", bridge_id)
                self.stats.last_error = reason
                try:
                    self._ack(bridge_id, "failed", reason)
                except Exception:  # pragma: no cover - last-resort survival
                    logger.exception("bridge quarantine ack failed for %s", bridge_id)
            processed += 1
        return processed

    def _deliver(self, record: dict[str, Any]) -> None:
        bridge_id = str(record.get("bridge_id", ""))
        conversation_id = str(record.get("conversation_id", ""))
        kind = str(record.get("kind", "text"))
        receiver = self._receiver_for(conversation_id, record)

        if not receiver:
            # The receiver may be registered by a later channel update, so treat
            # a missing receiver as retryable rather than dropping the reply.
            self._fail_or_retry(bridge_id, "missing_receiver")
            return

        outcome = None
        last_reason = ""
        for attempt in range(self.max_send_attempts):
            if kind == "file":
                path = str(record.get("path", ""))
                if not path or not Path(path).exists():
                    # A vanished/never-present file cannot be re-delivered: terminal.
                    self._ack(bridge_id, "failed", f"file_not_found:{path}")
                    return
                outcome = self.backend.send_file(receiver, path, str(record.get("caption", "")))
            else:
                outcome = self.backend.send_text(receiver, str(record.get("text", "")))
            if outcome.ok:
                break
            last_reason = outcome.reason
            logger.warning(
                "bridge delivery attempt %d/%d failed for %s: %s",
                attempt + 1,
                self.max_send_attempts,
                bridge_id,
                last_reason,
            )
            if attempt < self.max_send_attempts - 1:
                time.sleep(min(2.0, 0.5 * (attempt + 1)))

        if outcome is not None and outcome.ok:
            self._ack(bridge_id, "sent", outcome.reason, external_message_id=outcome.external_message_id)
            return
        self._fail_or_retry(bridge_id, last_reason or "send_failed")

    def _fail_or_retry(self, bridge_id: str, reason: str) -> None:
        """Ack a delivery failure as retryable (stays pending) or terminal.

        A transient reason (backend down, receiver momentarily unavailable) is
        left pending so a later tick retries it, up to a cross-tick cap. A
        permanent reason — or an exhausted retry budget — becomes terminal.
        """
        if _is_retryable_failure(reason):
            prior_retries = self._retry_count(bridge_id)
            if prior_retries < _MAX_CROSS_TICK_RETRIES:
                self._ack(
                    bridge_id,
                    "retry",
                    reason,
                    payload={"retry_attempt": prior_retries + 1, "max_retries": _MAX_CROSS_TICK_RETRIES},
                )
                return
            reason = f"retries_exhausted:{reason}"
        self._ack(bridge_id, "failed", reason)

    def _retry_count(self, bridge_id: str) -> int:
        """Number of non-terminal retry acks already recorded for this record."""
        count = 0
        for ack in self.store._read_all(self.store.ack_path):
            if str(ack.get("bridge_id", "")) == bridge_id and str(ack.get("status", "")) == "retry":
                count += 1
        return count

    def _receiver_for(self, conversation_id: str, record: dict[str, Any]) -> str:
        receiver = str(record.get("receiver") or "").strip()
        if receiver:
            return receiver
        # Legacy outbox records did not carry a receiver. Recover it from the
        # channel registry (roomid for groups, wxid for private) rather than
        # blindly using the hashed conversation_id, which is never a valid
        # wcf receiver and would misroute group replies.
        from app.personal_wechat_bot.wechat_driver.bridge_send import _channel_receiver

        resolved = _channel_receiver(self.data_dir, conversation_id)
        if resolved:
            return resolved
        return conversation_id.strip()

    def _ack(
        self,
        bridge_id: str,
        status: str,
        reason: str,
        *,
        external_message_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        if not bridge_id:
            return
        try:
            self.store.append_ack(
                bridge_id,
                status=status,
                reason=reason,
                external_message_id=external_message_id,
                payload=payload,
            )
        except Exception as exc:  # pragma: no cover - best effort persistence
            self.stats.last_error = f"append_ack_failed:{type(exc).__name__}:{exc}"
            logger.error("bridge %s", self.stats.last_error)
        # A non-terminal retry ack must not trigger ledger/queue sync (the send
        # has not resolved yet) and must not be marked synced.
        if status not in _TERMINAL_ACK_STATUSES and status != "blocked":
            self.stats.record(status, reason)
            return
        # Sync confirm queue + ledger. This is best-effort: a sync failure must
        # not prevent the ack (delivery already happened) from being recorded.
        # For terminal acks we record whether the sync succeeded so a later tick
        # can reconcile any that failed (otherwise the ledger/queue would stay
        # unflipped forever once the record leaves the pending set).
        synced = False
        try:
            sync_bridge_ack_to_send_state(
                self.data_dir,
                bridge_id,
                status=status,
                reason=reason,
                external_message_id=external_message_id,
            )
            synced = True
        except Exception as exc:  # pragma: no cover - best effort
            self.stats.last_error = f"sync_failed:{type(exc).__name__}:{exc}"
            logger.error("bridge %s", self.stats.last_error)
        if status in _TERMINAL_ACK_STATUSES and synced:
            self._mark_synced(bridge_id)
        self.stats.record(status, reason)

    def _synced_marker_path(self) -> Path:
        return self.data_dir / "send_bridge" / "synced_acks.json"

    def _load_synced(self) -> set[str]:
        path = self._synced_marker_path()
        if not path.exists():
            return set()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        ids = payload.get("synced") if isinstance(payload, dict) else None
        return {str(item) for item in ids} if isinstance(ids, list) else set()

    def _mark_synced(self, bridge_id: str) -> None:
        synced = self._load_synced()
        if bridge_id in synced:
            return
        synced.add(bridge_id)
        path = self._synced_marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps({"synced": sorted(synced)}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:  # pragma: no cover - best effort
            logger.error("bridge synced-marker write failed: %s", exc)

    def _reconcile_unsynced_acks(self) -> None:
        """Re-run ledger/confirm-queue sync for terminal acks that never synced.

        A terminal ack is durable in acks.jsonl even if its downstream sync
        failed (Windows file lock, disk error). Because the record then leaves
        the pending set, _deliver never runs again — without this pass the ledger
        and confirm queue would stay unflipped forever. sync is idempotent, so
        re-running for an already-synced record is harmless; the marker just
        avoids redundant work.
        """
        synced = self._load_synced()
        terminal: dict[str, dict[str, Any]] = {}
        for ack in self.store._read_all(self.store.ack_path):
            bridge_id = str(ack.get("bridge_id", ""))
            if bridge_id and str(ack.get("status", "")) in _TERMINAL_ACK_STATUSES:
                terminal[bridge_id] = ack  # keep the latest terminal ack
        for bridge_id, ack in terminal.items():
            if bridge_id in synced:
                continue
            try:
                sync_bridge_ack_to_send_state(
                    self.data_dir,
                    bridge_id,
                    status=str(ack.get("status", "")),
                    reason=str(ack.get("reason", "")),
                    external_message_id=str(ack.get("external_message_id", "")),
                )
                self._mark_synced(bridge_id)
            except Exception as exc:  # pragma: no cover - retried next tick
                self.stats.last_error = f"reconcile_sync_failed:{type(exc).__name__}:{exc}"
                logger.error("bridge %s", self.stats.last_error)


def run_bridge_worker(
    data_dir: str | Path,
    *,
    poll_interval_seconds: float = 2.0,
    once: bool = False,
    lock_enabled: bool = True,
    max_iterations: int | None = None,
) -> BridgeWorkerStats:
    """Run the outbox bridge, holding a single-instance lock for its lifetime."""
    data_dir = Path(data_dir)
    config = load_config(data_dir)
    backend = build_send_backend(config)
    worker = BridgeWorker(data_dir, backend)

    lock_path = data_dir / "send_bridge" / ".bridge_worker.lock"
    lock: ProcessLock | None = None
    if lock_enabled:
        lock = ProcessLock(lock_path, label="send_bridge_worker", stale_after_seconds=60.0)
        try:
            lock.acquire()
        except ProcessLockError as exc:
            logger.error("send bridge worker already running: %s", exc)
            raise

    try:
        iterations = 0
        while True:
            worker.run_once()
            if lock is not None:
                lock.heartbeat()
            iterations += 1
            if once or (max_iterations is not None and iterations >= max_iterations):
                break
            time.sleep(max(0.1, poll_interval_seconds))
    finally:
        if lock is not None:
            lock.release()
        try:
            backend.close()
        except Exception:  # pragma: no cover - best effort
            pass
    return worker.stats

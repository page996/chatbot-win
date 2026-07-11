"""Outbox bridge worker: delivers queued messages to WeChat without foreground.

The reply gate / send drivers only *queue* outgoing messages into
``<data_dir>/send_bridge/outbox.jsonl``. This worker is the consumer half: it
reads the outbox, delivers each not-yet-acked record through a
:class:`SendBackend` (native HTTP, WeFlow HTTP, or dry-run), writes an ack
to ``acks.jsonl``, and syncs the confirm queue + conversation ledger so the UI
and history reflect real delivery.

Design constraints (see the WeFlow concurrency model memory note):

* **Single instance.** Guarded by a :class:`ProcessLock`, so two bridges never
  double-send the same outbox lines.
* **Serialized sends.** Native send ports are treated as single-lane resources,
  so this worker delivers strictly one record at a time, in outbox (FIFO) order.
  That also preserves per-conversation ordering and interleaves fairly across
  conversations.
* **Restart-safe.** The "already resolved" cursor is derived from terminal acks
  in ``acks.jsonl`` (``sent``/``accepted``/``failed``/``blocked``), not from
  in-memory state, so a restart never re-sends an already-acked record.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.personal_wechat_bot.config.loader import load_config
from app.personal_wechat_bot.control.send_commands import sync_bridge_ack_to_send_state
from app.personal_wechat_bot.runtime.process_lock import (
    ProcessLock,
    ProcessLockError,
    process_pid_alive,
    process_start_marker,
)
from app.personal_wechat_bot.wechat_driver.bridge_send import (
    BridgeAckStatus,
    BridgeAckState,
    BridgeOutboxStore,
    _receiver_authorization_blocker,
    bridge_sync_fingerprint,
    effective_bridge_ack_states,
    is_terminal_bridge_ack_status,
)
from app.personal_wechat_bot.wechat_driver.send_backends import SendBackend, build_send_backend

logger = logging.getLogger(__name__)

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

# Outbox/acks compaction cadence and retention. Compaction drops old terminally-
# resolved records so the files (re-read in full every tick) stay bounded.
_COMPACT_EVERY_TICKS = 50
_KEEP_RESOLVED_RECORDS = 500
BRIDGE_WORKER_LOCK_STALE_SECONDS = 60.0
_ACCEPTED_REVERIFY_EVERY_SECONDS = 10.0
_ACCEPTED_REVERIFY_MAX_PER_TICK = 5
_ACCEPTED_REVERIFY_MAX_ATTEMPTS = 120
_REAL_SEND_BACKENDS = frozenset({"weflow_http", "wechat_native_http"})


# Reason markers for a send whose delivery outcome is genuinely unknown: the
# wire send may already have landed but the client never confirmed. Such a
# record must NOT be re-sent, because a re-send would duplicate an
# already-delivered message.
_UNKNOWN_DELIVERY_MARKERS = (
    "unknown_delivery_state",
    "connectionreseterror",
    "connectionabortederror",
    "brokenpipeerror",
    "connection reset by peer",
    "connection aborted",
    "broken pipe",
    "econnreset",
    "econnaborted",
    "epipe",
    "winerror 10053",
    "winerror 10054",
    "remotedisconnected",
    "remote end closed connection without response",
    "badstatusline",
    "incompleteread",
    "connectionerror",
    "ssleoferror",
    "ssl eof",
    "response ended",
    "remote protocol error",
    "timeouterror",
    "timed out",
    "weflow_http_send_text_error:timeouterror",
    "weflow_http_send_file_error:timeouterror",
    "wechat_native_http_send_text_error:timeouterror",
    "wechat_native_http_send_image_error:timeouterror",
    "wechat_native_http_send_file_error:timeouterror",
)

_PRE_CONNECT_FAILURE_MARKERS = (
    "connectionrefusederror",
    "econnrefused",
    "winerror 10061",
    "connect_failed",
    "connection refused",
    "errno 111",
    "actively refused",
)

_PERMANENT_FAILURE_MARKERS = (
    "unsupported",
    "http_404",
    "not_found",
    "file_not_found",
    "empty_text",
    "empty_file_path",
    "localhost",
    "non_local",
)


def _safe_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_nonnegative_float(value: Any) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed >= 0.0 else 0.0


def _is_unknown_delivery_state(reason: str) -> bool:
    lowered = str(reason or "").lower()
    if any(marker in lowered for marker in _PRE_CONNECT_FAILURE_MARKERS):
        return False
    if any(marker in lowered for marker in _UNKNOWN_DELIVERY_MARKERS):
        return True
    return re.search(r"(?:^|[^0-9])http_5[0-9]{2}(?:[^0-9]|$)", lowered) is not None


def _is_permanent_failure(reason: str) -> bool:
    lowered = str(reason or "").lower()
    return any(marker in lowered for marker in _PERMANENT_FAILURE_MARKERS)


def _is_retryable_failure(reason: str) -> bool:
    lowered = str(reason or "").lower()
    if _is_unknown_delivery_state(lowered):
        return False
    return any(marker in lowered for marker in _RETRYABLE_REASON_MARKERS)


def bridge_worker_lock_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "send_bridge" / ".bridge_worker.lock"


def _normalize_send_backend(value: str) -> str:
    return str(value or "dry_run").strip().lower()


def bridge_worker_lock_alive(
    data_dir: str | Path,
    *,
    stale_after_seconds: float = BRIDGE_WORKER_LOCK_STALE_SECONDS,
    now: float | None = None,
) -> bool:
    """True when the send-bridge worker lock has a fresh heartbeat."""

    lock_path = bridge_worker_lock_path(data_dir)
    if not lock_path.exists():
        return False
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    heartbeat = payload.get("heartbeat_at")
    if not isinstance(heartbeat, (int, float)):
        return False
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0 and not process_pid_alive(pid):
        return False
    recorded_start = str(payload.get("process_start") or "")
    current_start = process_start_marker(pid) if pid > 0 and recorded_start else ""
    if recorded_start and current_start and recorded_start != current_start:
        return False
    return ((time.time() if now is None else float(now)) - float(heartbeat)) <= max(
        1.0, float(stale_after_seconds)
    )


def bridge_worker_config_signature(config: Any) -> dict[str, Any]:
    """Backend config captured by a send-bridge worker at startup."""

    return {
        "send_enabled": bool(getattr(config, "send_enabled", False)),
        "send_driver": str(getattr(config, "send_driver", "") or ""),
        "send_backend": _normalize_send_backend(str(getattr(config, "send_backend", "dry_run") or "dry_run")),
        "weflow_base_url": str(getattr(config, "weflow_base_url", "http://127.0.0.1:5031") or "http://127.0.0.1:5031"),
        "weflow_token_env": str(getattr(config, "weflow_token_env", "WEFLOW_API_TOKEN") or "WEFLOW_API_TOKEN"),
        "weflow_send_text_path": str(getattr(config, "weflow_send_text_path", "/send/text") or "/send/text"),
        "weflow_send_file_path": str(getattr(config, "weflow_send_file_path", "/send/file") or "/send/file"),
        "weflow_send_timeout_seconds": float(getattr(config, "weflow_send_timeout_seconds", 35.0) or 35.0),
        "wechat_native_base_url": str(
            getattr(config, "wechat_native_base_url", "http://127.0.0.1:30001") or "http://127.0.0.1:30001"
        ),
        "wechat_native_send_text_path": str(getattr(config, "wechat_native_send_text_path", "/SendTextMsg") or "/SendTextMsg"),
        "wechat_native_send_image_path": str(getattr(config, "wechat_native_send_image_path", "/SendImgMsg") or "/SendImgMsg"),
        "wechat_native_send_file_path": str(getattr(config, "wechat_native_send_file_path", "/send_file_msg") or "/send_file_msg"),
        "wechat_native_status_path": str(getattr(config, "wechat_native_status_path", "/QueryDB/status") or "/QueryDB/status"),
        "wechat_native_timeout_seconds": float(getattr(config, "wechat_native_timeout_seconds", 15.0) or 15.0),
        "wechat_native_verify_timeout_seconds": float(getattr(config, "wechat_native_verify_timeout_seconds", 10.0) or 0.0),
        "wechat_native_file_verify_timeout_seconds": float(
            getattr(config, "wechat_native_file_verify_timeout_seconds", 45.0) or 0.0
        ),
    }


def _runtime_send_blocker(
    data_dir: str | Path,
    backend_name: str,
    *,
    expected_signature: dict[str, Any] | None = None,
) -> str:
    backend_name = _normalize_send_backend(backend_name)
    if expected_signature is None and backend_name not in _REAL_SEND_BACKENDS:
        return ""
    try:
        config = load_config(data_dir)
    except Exception as exc:
        return f"bridge_worker_runtime_config_unavailable:{type(exc).__name__}:{exc}"
    if str(getattr(config, "send_driver", "") or "") != "bridge_outbox":
        return f"send_driver_not_bridge_outbox:{getattr(config, 'send_driver', '') or 'missing'}"
    if not bool(getattr(config, "send_enabled", False)):
        return "send_enabled_false"
    config_backend = _normalize_send_backend(str(getattr(config, "send_backend", "") or "dry_run"))
    if config_backend != backend_name:
        return f"bridge_worker_runtime_backend_mismatch:worker_backend={backend_name}:config_backend={config_backend}"
    if expected_signature is not None:
        current_signature = bridge_worker_config_signature(config)
        if current_signature != expected_signature:
            changed = sorted(
                key
                for key in set(expected_signature) | set(current_signature)
                if expected_signature.get(key) != current_signature.get(key)
            )
            return "bridge_worker_runtime_config_changed:" + ",".join(changed[:8])
    return ""


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
        heartbeat: Callable[[], None] | None = None,
        config_signature: dict[str, Any] | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.backend = backend
        self.max_send_attempts = max(1, int(max_send_attempts))
        self.store = BridgeOutboxStore(self.data_dir)
        self.stats = BridgeWorkerStats()
        self.config_signature = dict(config_signature) if isinstance(config_signature, dict) else None
        # Called before each wire send so a slow drain keeps the single-instance
        # lock fresh (else a second worker could see it stale and double-send).
        self._heartbeat = heartbeat

    def _beat(self) -> None:
        if self._heartbeat is None:
            return
        try:
            self._heartbeat()
        except Exception:  # pragma: no cover - heartbeat must never break delivery
            logger.exception("bridge heartbeat callback failed")

    def _acked_bridge_ids(self) -> set[str]:
        return {
            bridge_id
            for bridge_id, ack_state in effective_bridge_ack_states(self.store._read_all(self.store.ack_path)).items()
            if ack_state.terminal
        }

    def pending_records(self) -> list[dict[str, Any]]:
        """Outbox records with no terminal ack yet, in FIFO order."""
        acked = self._acked_bridge_ids()
        pending: list[dict[str, Any]] = []
        for record in self.store._read_all(self.store.outbox_path):
            bridge_id = str(record.get("bridge_id", ""))
            if not bridge_id or bridge_id in acked:
                continue
            # Manual retries are published in two phases so queue/ledger/task
            # projections become visible before a fast worker can deliver them.
            if record.get("ready_for_delivery") is False:
                continue
            pending.append(record)
        return pending

    def _effective_ack_status(self) -> dict[str, str]:
        """Map each bridge_id to its monotonic effective ack status."""
        return {
            bridge_id: ack_state.status
            for bridge_id, ack_state in effective_bridge_ack_states(self.store._read_all(self.store.ack_path)).items()
        }

    def _quarantine_interrupted_sends(self) -> None:
        """Fail records whose latest ack is 'inflight' (crash mid-send).

        _deliver writes an 'inflight' ack immediately before the wire send. If
        the process crashes between the send and the terminal ack, the record is
        left with 'inflight' as its latest status. It may already have been
        delivered, and the endpoint may return no message id to dedup against,
        so re-sending risks a duplicate. The safe choice is to stop retrying
        and surface it for operator review rather than silently re-send.
        """
        for bridge_id, status in self._effective_ack_status().items():
            if status == BridgeAckStatus.INFLIGHT:
                logger.warning(
                    "bridge %s was interrupted mid-send; quarantining as possible duplicate", bridge_id
                )
                self._ack(bridge_id, BridgeAckStatus.FAILED, "possible_duplicate_send_after_crash")

    def run_once(self) -> int:
        """Deliver all currently-pending records. Returns the count processed."""
        self.stats.ticks += 1
        # A producer can crash after durably writing a staged outbox record but
        # before publishing its projections and activation bit. Such a record
        # was never eligible for delivery; terminate it once its owner is known
        # dead so it cannot remain an invisible pending item forever.
        try:
            abandoned_staged = self.store.quarantine_abandoned_staged_records()
        except Exception as exc:
            abandoned_staged = []
            self.stats.last_error = f"staged_quarantine_failed:{type(exc).__name__}:{exc}"
            logger.error("bridge %s", self.stats.last_error)
        for ack in abandoned_staged:
            bridge_id = str(ack.get("bridge_id") or "")
            reason = str(ack.get("reason") or "staged_record_owner_exited_before_activation")
            try:
                sync_result = sync_bridge_ack_to_send_state(
                    self.data_dir,
                    bridge_id,
                    status=BridgeAckStatus.FAILED,
                    reason=reason,
                )
                if isinstance(sync_result, dict) and bool(sync_result.get("sync_complete")):
                    self._mark_synced(bridge_id, self._sync_fingerprint(bridge_id, ack))
            except Exception as exc:
                self.stats.last_error = f"staged_quarantine_sync_failed:{type(exc).__name__}:{exc}"
                logger.error("bridge %s", self.stats.last_error)
            self.stats.record(BridgeAckStatus.FAILED, reason)
        # Re-sync any terminal acks whose ledger/confirm-queue sync previously
        # failed, so a transient state-write error becomes eventually consistent.
        self._reconcile_unsynced_acks()
        # Some native file sends finish asynchronously after the initial HTTP
        # ack. Re-check accepted/unverified records by readback only; never
        # re-send them as part of verification.
        self._reconcile_accepted_unverified_delivery()
        # Quarantine records whose delivery was interrupted mid-send by a crash
        # (an inflight marker with no terminal ack). These may already have been
        # delivered on the wire, so they must NOT be blindly re-sent.
        self._quarantine_interrupted_sends()
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
                    self._ack(bridge_id, BridgeAckStatus.FAILED, reason)
                except Exception:  # pragma: no cover - last-resort survival
                    logger.exception("bridge quarantine ack failed for %s", bridge_id)
            processed += 1
        self._maybe_compact()
        return processed

    def _maybe_compact(self) -> None:
        """Periodically compact the outbox/acks so they don't grow unbounded.

        Both files are re-read in full every tick, so compaction keeps steady-
        state read cost bounded. Runs every _COMPACT_EVERY_TICKS ticks; the marker
        of synced bridge_ids is pruned to whatever compaction retained.
        """
        if self.stats.ticks % _COMPACT_EVERY_TICKS != 0:
            return
        try:
            result = self.store.compact(
                keep_resolved=_KEEP_RESOLVED_RECORDS,
                synced_ack_fingerprints=self._load_synced(),
            )
        except Exception as exc:  # pragma: no cover - best effort
            self.stats.last_error = f"compact_failed:{type(exc).__name__}:{exc}"
            logger.error("bridge %s", self.stats.last_error)
            return
        if result.get("removed_outbox") or result.get("removed_acks"):
            self._prune_synced_marker()

    def _prune_synced_marker(self) -> None:
        """Drop synced-marker ids no longer present in the (compacted) acks."""
        try:
            ack_states = effective_bridge_ack_states(self.store._read_all(self.store.ack_path))
            outbox_by_id = self._outbox_records_by_id()
            synced = {
                bridge_id: fingerprint
                for bridge_id, fingerprint in self._load_synced().items()
                if bridge_id in ack_states
                and ack_states[bridge_id].terminal
                and fingerprint
                == bridge_sync_fingerprint(ack_states[bridge_id].ack, outbox_by_id.get(bridge_id))
            }
            path = self._synced_marker_path()
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"version": 3, "synced": synced}, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:  # pragma: no cover - best effort
            logger.error("bridge synced-marker prune failed: %s", exc)

    def _deliver(self, record: dict[str, Any]) -> None:
        bridge_id = str(record.get("bridge_id", ""))
        conversation_id = str(record.get("conversation_id", ""))
        kind = str(record.get("kind", "text"))
        runtime_blocker = _runtime_send_blocker(
            self.data_dir,
            str(getattr(self.backend, "name", "")),
            expected_signature=self.config_signature,
        )
        if runtime_blocker:
            self.stats.last_error = runtime_blocker
            self.stats.record("skipped", runtime_blocker)
            logger.warning("bridge %s skipped before wire send: %s", bridge_id, runtime_blocker)
            return
        receiver = self._receiver_for(conversation_id, record)

        if not receiver:
            # The receiver may be registered by a later channel update, so treat
            # a missing receiver as retryable rather than dropping the reply.
            self._fail_or_retry(bridge_id, "missing_receiver")
            return
        receiver_blocker = _receiver_authorization_blocker(
            self.data_dir,
            conversation_id,
            receiver,
            backend_name=str(getattr(self.backend, "name", "")),
        )
        if receiver_blocker:
            self._ack(bridge_id, BridgeAckStatus.BLOCKED, receiver_blocker)
            return

        outcome = None
        last_reason = ""
        last_payload: dict[str, Any] = {}
        # Mark inflight right before the wire send. If we crash between the send
        # and the terminal ack, the next run sees 'inflight' as the latest ack and
        # quarantines the record instead of risking a duplicate re-send.
        if not self._ack(bridge_id, BridgeAckStatus.INFLIGHT, "delivering"):
            # Do not touch the wire unless the pre-send marker is durable. If the
            # ack file is temporarily locked/unwritable, leaving the record
            # pending is safer than sending without a restart-safe marker.
            return
        for attempt in range(self.max_send_attempts):
            # Keep the single-instance lock fresh right before each blocking send
            # so a slow drain or a slow individual send can't let the lock go
            # stale and invite a second worker to take over and double-deliver.
            self._beat()
            # Config and channel authorization are mutable while a backend is
            # unavailable. Re-check them before *every* wire attempt so turning
            # sending off or revoking a channel after attempt 1 prevents attempt 2.
            runtime_blocker = _runtime_send_blocker(
                self.data_dir,
                str(getattr(self.backend, "name", "")),
                expected_signature=self.config_signature,
            )
            if runtime_blocker:
                self.stats.last_error = runtime_blocker
                self._ack(
                    bridge_id,
                    BridgeAckStatus.RETRY,
                    runtime_blocker,
                    payload={"phase": "pre_wire_recheck", "attempt": attempt + 1},
                )
                logger.warning("bridge %s paused before wire retry: %s", bridge_id, runtime_blocker)
                return
            receiver = self._receiver_for(conversation_id, record)
            if not receiver:
                self._fail_or_retry(bridge_id, "missing_receiver")
                return
            receiver_blocker = _receiver_authorization_blocker(
                self.data_dir,
                conversation_id,
                receiver,
                backend_name=str(getattr(self.backend, "name", "")),
            )
            if receiver_blocker:
                self._ack(bridge_id, BridgeAckStatus.BLOCKED, receiver_blocker)
                return
            effective_ack = self._effective_ack_state(bridge_id)
            if effective_ack is not None and effective_ack.terminal:
                self.stats.record("skipped", f"terminal_before_wire:{effective_ack.status}")
                logger.warning(
                    "bridge %s stopped before wire send by terminal ack: %s",
                    bridge_id,
                    effective_ack.status,
                )
                return
            if kind == "file":
                path = str(record.get("path", ""))
                if not path or not Path(path).exists():
                    # A vanished/never-present file cannot be re-delivered: terminal.
                    self._ack(bridge_id, BridgeAckStatus.FAILED, f"file_not_found:{path}")
                    return
                outcome = self.backend.send_file(receiver, path, str(record.get("caption", "")))
            else:
                outcome = self.backend.send_text(receiver, str(record.get("text", "")))
            if outcome.ok:
                break
            last_reason = outcome.reason
            last_payload = outcome.payload
            logger.warning(
                "bridge delivery attempt %d/%d failed for %s: %s",
                attempt + 1,
                self.max_send_attempts,
                bridge_id,
                last_reason,
            )
            # An unknown-delivery-state failure may already have landed on the
            # wire. Never re-send it in the same tick for the remaining attempts.
            if _is_unknown_delivery_state(last_reason):
                logger.warning(
                    "bridge %s: unknown delivery state, not re-sending this tick", bridge_id
                )
                break
            if _is_permanent_failure(last_reason):
                logger.warning(
                    "bridge %s: permanent delivery failure, not re-sending this tick", bridge_id
                )
                break
            if attempt < self.max_send_attempts - 1:
                time.sleep(min(2.0, 0.5 * (attempt + 1)))

        if outcome is not None and outcome.ok:
            ack_status = BridgeAckStatus.SENT if getattr(outcome, "delivery_verified", True) else BridgeAckStatus.ACCEPTED
            self._ack(
                bridge_id,
                ack_status,
                outcome.reason,
                external_message_id=outcome.external_message_id,
                payload=outcome.payload,
            )
            return
        self._fail_or_retry(bridge_id, last_reason or "send_failed", payload=last_payload)

    def _fail_or_retry(self, bridge_id: str, reason: str, *, payload: dict[str, Any] | None = None) -> None:
        """Ack a delivery failure as retryable (stays pending) or terminal.

        A transient reason (backend down, receiver momentarily unavailable) is
        left pending so a later tick retries it, up to a cross-tick cap. A
        permanent reason — or an exhausted retry budget — becomes terminal.
        """
        if _is_unknown_delivery_state(reason):
            quarantined_reason = str(reason or "unknown_delivery_state")
            if "unknown_delivery_state" not in quarantined_reason.lower():
                quarantined_reason = f"unknown_delivery_state:{quarantined_reason}"
            self._ack(bridge_id, BridgeAckStatus.FAILED, quarantined_reason, payload=payload)
            return
        if _is_retryable_failure(reason):
            prior_retries = self._retry_count(bridge_id)
            if prior_retries < _MAX_CROSS_TICK_RETRIES:
                self._ack(
                    bridge_id,
                    BridgeAckStatus.RETRY,
                    reason,
                    payload={
                        **(payload or {}),
                        "retry_attempt": prior_retries + 1,
                        "max_retries": _MAX_CROSS_TICK_RETRIES,
                    },
                )
                return
            reason = f"retries_exhausted:{reason}"
        self._ack(bridge_id, BridgeAckStatus.FAILED, reason, payload=payload)

    def _retry_count(self, bridge_id: str) -> int:
        """Number of non-terminal retry acks already recorded for this record."""
        count = 0
        for ack in self.store._read_all(self.store.ack_path):
            if str(ack.get("bridge_id", "")) == bridge_id and str(ack.get("status", "")) == BridgeAckStatus.RETRY:
                count += 1
        return count

    def _receiver_for(self, conversation_id: str, record: dict[str, Any]) -> str:
        receiver = str(record.get("receiver") or "").strip()
        if receiver:
            return receiver
        # Legacy outbox records did not carry a receiver. Recover it from the
        # channel registry (roomid for groups, wxid for private) rather than
        # blindly using the hashed conversation_id, which is never a valid
        # WeChat receiver and would misroute group replies.
        from app.personal_wechat_bot.wechat_driver.bridge_send import (
            _channel_receiver,
            _looks_like_wechat_receiver,
        )

        resolved = _channel_receiver(self.data_dir, conversation_id)
        if resolved:
            return resolved
        # Only fall back to the conversation_id when it is itself a valid WeChat
        # receiver (a raw wxid/roomid). A hashed conversation_id is not, so
        # returning it would misroute or fail on the wire; yield "" instead so
        # _deliver treats it as missing_receiver (retryable until the channel is
        # registered), mirroring the driver-side guard.
        candidate = conversation_id.strip()
        return candidate if _looks_like_wechat_receiver(candidate) else ""

    def _ack(
        self,
        bridge_id: str,
        status: str,
        reason: str,
        *,
        external_message_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not bridge_id:
            return False
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
            return False
        effective_ack = self._effective_ack_state(bridge_id)
        if effective_ack is None:
            self.stats.last_error = f"ack_not_observable:{bridge_id}:{status}"
            logger.error("bridge %s", self.stats.last_error)
            return False
        if effective_ack is not None and effective_ack.terminal and not is_terminal_bridge_ack_status(status):
            self.stats.record(status, "stale_nonterminal_after_terminal")
            return False
        # A non-terminal ack must not trigger ledger/queue sync (the send has not
        # resolved yet) and must not be marked synced.
        if not is_terminal_bridge_ack_status(status):
            self.stats.record(status, reason)
            return True
        if effective_ack is not None and effective_ack.terminal:
            status = effective_ack.status
            reason = str(effective_ack.ack.get("reason", reason))
            external_message_id = str(effective_ack.ack.get("external_message_id", external_message_id))
        # Sync confirm queue + ledger. This is best-effort: a sync failure must
        # not prevent the ack (delivery already happened) from being recorded.
        # For terminal acks we record whether the sync succeeded so a later tick
        # can reconcile any that failed (otherwise the ledger/queue would stay
        # unflipped forever once the record leaves the pending set).
        synced = False
        try:
            sync_result = sync_bridge_ack_to_send_state(
                self.data_dir,
                bridge_id,
                status=status,
                reason=reason,
                external_message_id=external_message_id,
            )
            queue_error = str(sync_result.get("queue_error", "")) if isinstance(sync_result, dict) else ""
            synced = isinstance(sync_result, dict) and bool(sync_result.get("sync_complete"))
            if queue_error:
                self.stats.last_error = f"sync_queue_error:{queue_error}"
        except Exception as exc:  # pragma: no cover - best effort
            self.stats.last_error = f"sync_failed:{type(exc).__name__}:{exc}"
            logger.error("bridge %s", self.stats.last_error)
        if is_terminal_bridge_ack_status(status) and synced:
            self._mark_synced(bridge_id, self._sync_fingerprint(bridge_id, effective_ack.ack))
        self.stats.record(status, reason)
        return True

    def _effective_ack_state(self, bridge_id: str) -> BridgeAckState | None:
        return effective_bridge_ack_states(self.store._read_all(self.store.ack_path)).get(bridge_id)

    def _outbox_records_by_id(self) -> dict[str, dict[str, Any]]:
        return {
            str(record.get("bridge_id", "")): record
            for record in self.store._read_all(self.store.outbox_path)
            if isinstance(record, dict) and str(record.get("bridge_id", ""))
        }

    def _sync_fingerprint(self, bridge_id: str, ack: dict[str, Any]) -> str:
        return bridge_sync_fingerprint(ack, self._outbox_records_by_id().get(str(bridge_id or "")))

    def _synced_marker_path(self) -> Path:
        return self.data_dir / "send_bridge" / "synced_acks.json"

    def _accepted_reverify_marker_path(self) -> Path:
        return self.data_dir / "send_bridge" / "accepted_reverify.json"

    def _load_synced(self) -> dict[str, str]:
        path = self._synced_marker_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            return {}
        except (UnicodeError, json.JSONDecodeError) as exc:
            self._quarantine_corrupt_marker(path, exc)
            return {}
        values = payload.get("synced") if isinstance(payload, dict) else None
        version = payload.get("version") if isinstance(payload, dict) else None
        # Versions 1 and 2 did not bind proof to the current projection
        # contract. They cannot prove that newly-published queue/ledger/task
        # projections were updated, so conservatively re-sync into v3.
        if version != 3 or not isinstance(values, dict):
            return {}
        return {
            str(bridge_id): str(fingerprint)
            for bridge_id, fingerprint in values.items()
            if str(bridge_id) and str(fingerprint)
        }

    def _mark_synced(self, bridge_id: str, ack_fingerprint: str) -> None:
        bridge_id = str(bridge_id or "")
        ack_fingerprint = str(ack_fingerprint or "")
        if not bridge_id or not ack_fingerprint:
            return
        synced = self._load_synced()
        if synced.get(bridge_id) == ack_fingerprint:
            return
        synced[bridge_id] = ack_fingerprint
        path = self._synced_marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps({"version": 3, "synced": synced}, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:  # pragma: no cover - best effort
            logger.error("bridge synced-marker write failed: %s", exc)

    def _load_accepted_reverify_marker(self) -> dict[str, dict[str, Any]]:
        path = self._accepted_reverify_marker_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            return {}
        except (UnicodeError, json.JSONDecodeError) as exc:
            self._quarantine_corrupt_marker(path, exc)
            return {}
        items = payload.get("items") if isinstance(payload, dict) else {}
        if not isinstance(items, dict):
            return {}
        return {str(key): value for key, value in items.items() if isinstance(value, dict)}

    @staticmethod
    def _quarantine_corrupt_marker(path: Path, error: Exception) -> None:
        target = path.with_name(f"{path.name}.corrupt.{time.time_ns()}")
        try:
            path.replace(target)
        except OSError as exc:
            logger.error("bridge marker quarantine failed for %s: %s", path, exc)
            return
        logger.error("bridge marker quarantined at %s: %s", target, error)

    def _save_accepted_reverify_marker(self, marker: dict[str, dict[str, Any]]) -> None:
        path = self._accepted_reverify_marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps({"items": marker}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:  # pragma: no cover - best effort
            logger.error("bridge accepted-reverify marker write failed: %s", exc)

    def _reconcile_accepted_unverified_delivery(self) -> None:
        verifier = getattr(self.backend, "verify_accepted_bridge_record", None)
        if not callable(verifier):
            return
        outbox = self.store._read_all(self.store.outbox_path)
        ack_states = effective_bridge_ack_states(self.store._read_all(self.store.ack_path))
        marker = self._load_accepted_reverify_marker()
        now = time.time()
        changed = False
        checked = 0
        for record in outbox:
            if checked >= _ACCEPTED_REVERIFY_MAX_PER_TICK:
                break
            bridge_id = str(record.get("bridge_id", ""))
            if not bridge_id:
                continue
            ack_state = ack_states.get(bridge_id)
            if ack_state is None or ack_state.status != BridgeAckStatus.ACCEPTED:
                continue
            ack = ack_state.ack
            payload = ack.get("payload") if isinstance(ack.get("payload"), dict) else {}
            if payload.get("delivery_verified") is True:
                continue
            item_marker = marker.get(bridge_id, {})
            attempts = _safe_nonnegative_int(item_marker.get("attempts"))
            if attempts >= _ACCEPTED_REVERIFY_MAX_ATTEMPTS:
                continue
            last_checked = _safe_nonnegative_float(item_marker.get("last_checked_at"))
            if now - last_checked < _ACCEPTED_REVERIFY_EVERY_SECONDS:
                continue
            marker[bridge_id] = {
                **item_marker,
                "attempts": attempts + 1,
                "last_checked_at": now,
            }
            checked += 1
            changed = True
            runtime_blocker = _runtime_send_blocker(
                self.data_dir,
                str(getattr(self.backend, "name", "")),
                expected_signature=self.config_signature,
            )
            if runtime_blocker:
                self.stats.last_error = runtime_blocker
                marker[bridge_id]["last_error"] = runtime_blocker
                break
            try:
                outcome = verifier(record, ack)
            except Exception as exc:  # pragma: no cover - backend recheck must not stop worker
                reason = f"accepted_reverify_error:{type(exc).__name__}:{exc}"
                self.stats.last_error = reason
                marker[bridge_id]["last_error"] = reason
                logger.warning("bridge %s accepted reverify failed: %s", bridge_id, reason)
                continue
            if outcome is None or not getattr(outcome, "ok", False) or not getattr(outcome, "delivery_verified", False):
                continue
            marker[bridge_id]["verified_at"] = now
            self._ack(
                bridge_id,
                BridgeAckStatus.SENT,
                str(outcome.reason or "accepted_reverified_sent"),
                external_message_id=str(outcome.external_message_id or ""),
                payload=outcome.payload,
            )
        if changed:
            live_ids = {str(item.get("bridge_id", "")) for item in outbox if str(item.get("bridge_id", ""))}
            self._save_accepted_reverify_marker({key: value for key, value in marker.items() if key in live_ids})

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
        outbox_by_id = self._outbox_records_by_id()
        terminal = {
            bridge_id: ack_state.ack
            for bridge_id, ack_state in effective_bridge_ack_states(self.store._read_all(self.store.ack_path)).items()
            if ack_state.terminal
        }
        for bridge_id, ack in terminal.items():
            fingerprint = bridge_sync_fingerprint(ack, outbox_by_id.get(bridge_id))
            if synced.get(bridge_id) == fingerprint:
                continue
            try:
                sync_result = sync_bridge_ack_to_send_state(
                    self.data_dir,
                    bridge_id,
                    status=str(ack.get("status", "")),
                    reason=str(ack.get("reason", "")),
                    external_message_id=str(ack.get("external_message_id", "")),
                )
                queue_error = str(sync_result.get("queue_error", "")) if isinstance(sync_result, dict) else ""
                if isinstance(sync_result, dict) and bool(sync_result.get("sync_complete")):
                    self._mark_synced(bridge_id, fingerprint)
                elif queue_error:
                    self.stats.last_error = f"reconcile_queue_error:{queue_error}"
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
    stop_event: Any = None,
) -> BridgeWorkerStats:
    """Run the outbox bridge, holding a single-instance lock for its lifetime.

    ``stop_event`` (any object with ``is_set()``) lets a supervisor request a
    clean stop between ticks — the loop checks it after each drain and before
    sleeping, so a stop takes effect within one poll interval.
    """
    data_dir = Path(data_dir)
    config = load_config(data_dir)
    backend = build_send_backend(config)
    config_signature = bridge_worker_config_signature(config)
    worker = BridgeWorker(data_dir, backend, config_signature=config_signature)

    lock_path = bridge_worker_lock_path(data_dir)
    lock: ProcessLock | None = None
    if lock_enabled:
        lock = ProcessLock(
            lock_path,
            label="send_bridge_worker",
            stale_after_seconds=BRIDGE_WORKER_LOCK_STALE_SECONDS,
            metadata={
                "data_dir": str(data_dir.resolve()),
                "backend_name": getattr(backend, "name", ""),
                "config_signature": config_signature,
            },
        )
        try:
            lock.acquire()
        except ProcessLockError as exc:
            logger.error("send bridge worker already running: %s", exc)
            raise
        # Heartbeat before each send too (not just per-drain), so a large backlog
        # or a slow individual send can't let the 60s lock go stale mid-drain and
        # invite a second worker to take over and double-deliver.
        worker._heartbeat = lock.heartbeat

    try:
        iterations = 0
        while True:
            runtime_blocker = _runtime_send_blocker(
                data_dir,
                str(getattr(backend, "name", "")),
                expected_signature=config_signature,
            )
            if runtime_blocker:
                worker.stats.last_error = runtime_blocker
                worker.stats.record("skipped", runtime_blocker)
                logger.warning("send bridge worker stopped by runtime config: %s", runtime_blocker)
                break
            worker.run_once()
            if lock is not None:
                lock.heartbeat()
            iterations += 1
            if once or (max_iterations is not None and iterations >= max_iterations):
                break
            if stop_event is not None and stop_event.is_set():
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

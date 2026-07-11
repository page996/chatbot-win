from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.personal_wechat_bot.conversation.channel_control import normalize_control_mode, parse_bool, snooze_is_active
from app.personal_wechat_bot.conversation.channel_state_store import ChannelStateStore
from app.personal_wechat_bot.runtime.process_lock import scoped_process_lock_path, short_process_lock
from app.personal_wechat_bot.tasks.scheduler_store import SchedulerStore


ACTIVE_STATUSES = {"queued", "running", "waiting", "paused", "blocked"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
VALID_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES
_BRIDGE_ID_RE = re.compile(r"(?:^|[^A-Za-z0-9_])(?P<id>bridge:[^\s,;，；。)）\]】]+)")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class TaskRecord:
    task_id: str
    title: str
    conversation_id: str = ""
    session_id: str = "session_default"
    kind: str = "operation"
    status: str = "queued"
    priority: int = 50
    progress: int = 0
    phase: str = ""
    detail: str = ""
    blocker: str = ""
    assigned_worker: str = ""
    parent_task_id: str = ""
    dependencies: list[str] = field(default_factory=list)
    concurrency_key: str = "global"
    topic_id: str = ""
    topic_title: str = ""
    resource_class: str = "cpu_io"
    estimated_cost: int = 1
    actual_cost: int = 0
    stop_and_wait: bool = False
    external_id: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    started_at: str = ""
    finished_at: str = ""
    last_error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["priority_score"] = task_priority_score(payload)
        return payload


class TaskStatusStore:
    """Persistent task state for sidebar/agent operations.

    This is intentionally a status manager, not a worker pool. Runtime code can
    create/update records from multiple conversations; schedulers can sort by the
    priority score and honor paused/waiting states before dispatching work.
    """

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.root = Path(data_dir) / "task_manager"
        self.path = self.root / "tasks.json"
        self.scheduler_store = SchedulerStore(data_dir)
        self._lock = threading.RLock()

    def state(self, *, limit: int = 100) -> dict[str, Any]:
        with self._lock:
            tasks = self._read_tasks()
            ordered = sorted(tasks, key=task_priority_score, reverse=True)
            active = [item for item in ordered if item.get("status") in ACTIVE_STATUSES]
            terminal = [item for item in ordered if item.get("status") in TERMINAL_STATUSES]
            resource_pools = _resource_pools(tasks)
            channel_controls = _load_channel_controls(self.data_dir)
            return {
                "status": "ok",
                "storage": str(self.path),
                "scheduler": {
                    "schema": "multi_channel_task_scheduler_v1",
                    "policy": "priority_score_then_channel_concurrency_then_resource_budget",
                    "priority_algorithm": "status_weight + user_priority*2 + channel_priority_bonus + conversation_bonus + age_bonus - pause_penalty - dependency_penalty",
                    "dispatch_policy": "queued_only + channel_control_allows + dependencies_completed + resource_slot_available + per_channel_limit + concurrency_key_mutex",
                    "active_statuses": sorted(ACTIVE_STATUSES),
                    "terminal_statuses": sorted(TERMINAL_STATUSES),
                    "supports_stop_and_wait": True,
                    "supports_multi_conversation": True,
                    "supports_atomic_claim": True,
                    "supports_channel_controls": True,
                    "channel_control_count": len(channel_controls),
                    "resource_pools": resource_pools,
                    "dispatch_preview": _dispatch_preview(
                        tasks,
                        resource_limits=_pool_limits(resource_pools),
                        channel_controls=channel_controls,
                    ),
                },
                "counts": _counts(tasks),
                "channels": _channel_lanes(tasks),
                "active": active[:limit],
                "history": terminal[:limit],
                "tasks": ordered[:limit],
            }

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            task_id = _clean_id(payload.get("task_id") or payload.get("id") or "")
            if not task_id:
                task_id = f"task-{uuid4().hex[:12]}"

            def mutate(tasks: list[dict[str, Any]]):
                tasks, _, _ = self._repair_tasks_atomically(tasks)
                existing = next((item for item in tasks if item.get("task_id") == task_id), None)
                if existing is not None:
                    result = _merge_task(existing, payload)
                    _replace(tasks, task_id, result)
                    event = "updated"
                else:
                    result = TaskRecord(
                        task_id=task_id,
                        title=str(payload.get("title") or payload.get("label") or "operation").strip()
                        or "operation",
                        conversation_id=str(payload.get("conversation_id") or payload.get("conversationId") or ""),
                        session_id=str(payload.get("session_id") or payload.get("sessionId") or "session_default"),
                        kind=str(payload.get("kind") or payload.get("category") or "operation"),
                        status=_status(payload.get("status"), "queued"),
                        priority=_int(payload.get("priority"), 50),
                        progress=_percent(payload.get("progress"), 0),
                        phase=str(payload.get("phase") or ""),
                        detail=str(payload.get("detail") or ""),
                        blocker=str(payload.get("blocker") or ""),
                        assigned_worker=str(payload.get("assigned_worker") or payload.get("assignedWorker") or ""),
                        parent_task_id=str(payload.get("parent_task_id") or payload.get("parentTaskId") or ""),
                        dependencies=_string_list(payload.get("dependencies")),
                        concurrency_key=str(payload.get("concurrency_key") or payload.get("scope") or "global"),
                        topic_id=str(payload.get("topic_id") or payload.get("topicId") or ""),
                        topic_title=str(payload.get("topic_title") or payload.get("topicTitle") or ""),
                        resource_class=str(payload.get("resource_class") or payload.get("resourceClass") or "cpu_io"),
                        estimated_cost=max(0, _int(payload.get("estimated_cost") or payload.get("estimatedCost"), 1)),
                        actual_cost=max(0, _int(payload.get("actual_cost") or payload.get("actualCost"), 0)),
                        stop_and_wait=bool(payload.get("stop_and_wait") or payload.get("stopAndWait")),
                        external_id=str(payload.get("external_id") or payload.get("externalId") or ""),
                        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
                    ).to_dict()
                    tasks.append(result)
                    event = "created"
                return _bounded_task_records(tasks), result, [(task_id, event, result)]

            result = self.scheduler_store.update_tasks_atomically(mutate)
            self._write_projection_from_sqlite()
            return result

    def update(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            def mutate(tasks: list[dict[str, Any]]):
                tasks, _, _ = self._repair_tasks_atomically(tasks)
                current = next((item for item in tasks if item.get("task_id") == task_id), None)
                if current is None:
                    raise KeyError(f"task not found: {task_id}")
                updated = _merge_task(current, patch)
                _replace(tasks, task_id, updated)
                return _bounded_task_records(tasks), updated, [(task_id, "updated", updated)]

            updated = self.scheduler_store.update_tasks_atomically(mutate)
            self._write_projection_from_sqlite()
            return updated

    def transition(self, task_id: str, action: str, patch: dict[str, Any] | None = None) -> dict[str, Any]:
        action_status = {
            "start": "running",
            "pause": "paused",
            "resume": "queued",
            "wait": "waiting",
            "block": "blocked",
            "complete": "completed",
            "fail": "failed",
            "cancel": "cancelled",
        }
        status = action_status.get(str(action or "").strip().lower())
        if not status:
            raise ValueError(f"unsupported task action: {action}")
        task = self.update(task_id, {"status": status, **(patch or {})})
        self._write_event(task_id, f"transition:{status}", task)
        return task

    def finish_external(self, external_id: str, patch: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Terminalize all active records tied to a backend job id."""

        external_id = str(external_id or "").strip()
        if not external_id:
            return []
        with self._lock:
            def mutate(tasks: list[dict[str, Any]]):
                tasks, _, _ = self._repair_tasks_atomically(tasks)
                updated_tasks: list[dict[str, Any]] = []
                events: list[tuple[str, str, dict[str, Any]]] = []
                for index, task in enumerate(tasks):
                    if str(task.get("external_id") or "") != external_id:
                        continue
                    if str(task.get("status") or "") not in ACTIVE_STATUSES:
                        continue
                    updated = _merge_task(task, patch or {})
                    tasks[index] = updated
                    updated_tasks.append(updated)
                    events.append((str(updated.get("task_id") or ""), "finish_external", updated))
                return _bounded_task_records(tasks), updated_tasks, events

            updated_tasks = self.scheduler_store.update_tasks_atomically(mutate)
            self._write_projection_from_sqlite()
            return updated_tasks

    def claim_next(
        self,
        *,
        worker_id: str,
        resource_limits: dict[str, int] | None = None,
        channel_limit: int = 1,
        allowed_resources: list[str] | None = None,
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        """Atomically claim runnable queued tasks for a scheduler worker.

        This is the first executable piece of the command center. It does not
        run the work; it gives workers a safe way to reserve tasks without
        racing other workers or stealing resources from an already-busy channel.
        """

        worker = str(worker_id or "").strip() or f"worker-{uuid4().hex[:8]}"
        resource_limits = {str(key): max(1, _int(value, 1)) for key, value in (resource_limits or {}).items()}
        allowed = {str(item) for item in (allowed_resources or []) if str(item).strip()}
        max_claims = max(1, min(100, _int(limit, 1)))
        per_channel = max(1, _int(channel_limit, 1))
        channel_controls = _load_channel_controls(self.data_dir)

        def mutate(tasks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[str, str, dict[str, Any]]]]:
            filtered = [dict(item) for item in tasks if isinstance(item, dict) and not _is_ephemeral_ui_task(item)]
            updated_tasks = _repair_stale_external_tasks(filtered, data_dir=self.data_dir)
            claimed: list[dict[str, Any]] = []
            events: list[tuple[str, str, dict[str, Any]]] = []
            resource_active = _active_by_resource(updated_tasks)
            conversation_active = _active_by_conversation(updated_tasks)
            active_scopes = _active_concurrency_keys(updated_tasks)
            completed_ids = _completed_task_ids(updated_tasks)
            ordered = sorted(
                updated_tasks,
                key=lambda item: _dispatch_priority_score(item, completed_ids, channel_controls=channel_controls),
                reverse=True,
            )
            for task in ordered:
                if len(claimed) >= max_claims:
                    break
                if str(task.get("status") or "") != "queued":
                    continue
                conversation_id = str(task.get("conversation_id") or "")
                if _channel_control_block_reason(conversation_id, channel_controls):
                    continue
                resource = str(task.get("resource_class") or "cpu_io")
                if allowed and resource not in allowed:
                    continue
                if not _dependencies_satisfied(task, completed_ids):
                    continue
                resource_limit = max(1, resource_limits.get(resource, _default_resource_limit(resource)))
                if resource_active.get(resource, 0) >= resource_limit:
                    continue
                if conversation_id and conversation_active.get(conversation_id, 0) >= per_channel:
                    continue
                concurrency_key = str(task.get("concurrency_key") or "")
                if _mutex_scope(concurrency_key) and concurrency_key in active_scopes:
                    continue
                next_progress = max(1, _percent(task.get("progress"), 0))
                updated = _merge_task(
                    task,
                    {
                        "status": "running",
                        "assigned_worker": worker,
                        "progress": next_progress,
                        "phase": task.get("phase") or "已由总台领取",
                    },
                )
                _replace(updated_tasks, str(updated.get("task_id") or ""), updated)
                claimed.append(updated)
                events.append((str(updated.get("task_id") or ""), "claimed", updated))
                resource_active[resource] = resource_active.get(resource, 0) + 1
                if conversation_id:
                    conversation_active[conversation_id] = conversation_active.get(conversation_id, 0) + 1
                if _mutex_scope(concurrency_key):
                    active_scopes.add(concurrency_key)
            return updated_tasks, claimed, events

        with self._lock:
            claimed = self.scheduler_store.update_tasks_atomically(mutate)
            if claimed:
                self._write_projection_from_sqlite()
            return claimed

    def dispatch_preview(
        self,
        *,
        resource_limits: dict[str, int] | None = None,
        channel_limit: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        with self._lock:
            tasks = self._read_tasks()
            pools = _resource_pools(tasks)
            limits = resource_limits or _pool_limits(pools)
            return _dispatch_preview(
                tasks,
                resource_limits=limits,
                channel_limit=channel_limit,
                limit=limit,
                channel_controls=_load_channel_controls(self.data_dir),
            )

    def events(self, *, task_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        return self.scheduler_store.list_events(task_id=task_id, limit=limit)

    def _read_tasks(self) -> list[dict[str, Any]]:
        tasks = self.scheduler_store.list_tasks()
        if tasks:
            filtered = [dict(item) for item in tasks if isinstance(item, dict) and not _is_ephemeral_ui_task(item)]
            repaired = _repair_stale_external_tasks(filtered, data_dir=self.data_dir)
            if filtered != tasks or repaired != filtered:
                repaired = self.scheduler_store.update_tasks_atomically(self._repair_tasks_atomically)
                self._write_projection_from_sqlite()
            return repaired
        return []

    def _repair_tasks_atomically(
        self, tasks: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[str, str, dict[str, Any]]]]:
        filtered = [dict(item) for item in tasks if isinstance(item, dict) and not _is_ephemeral_ui_task(item)]
        repaired = _repair_stale_external_tasks(filtered, data_dir=self.data_dir)
        return repaired, repaired, []

    def _write_projection_from_sqlite(self) -> None:
        lock_path = scoped_process_lock_path(
            self.data_dir,
            "task-manager-projection",
            "global",
        )
        with short_process_lock(
            lock_path,
            timeout_seconds=30.0,
            stale_after_seconds=60.0,
            timeout_label="task manager projection lock",
        ):
            # Read after acquiring the projection lock. A caller that committed
            # earlier must not overwrite a newer caller with its older snapshot.
            self._write_projection(self.scheduler_store.list_tasks())

    def _write_projection(self, tasks: list[dict[str, Any]]) -> None:
        tasks = sorted(tasks, key=lambda item: str(item.get("updated_at", "")), reverse=True)[:500]
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "task_manager_v1",
            "updated_at": _now_iso(),
            "authority": "scheduler.sqlite",
            "sqlite": str(self.scheduler_store.path),
            "tasks": tasks,
        }
        tmp = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _write_event(self, task_id: str, event: str, payload: dict[str, Any]) -> None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return
        try:
            self.scheduler_store.append_event(task_id, event, payload)
        except Exception:
            pass

def task_priority_score(task: dict[str, Any]) -> float:
    status = str(task.get("status", "queued"))
    status_weight = {
        "running": 500,
        "queued": 420,
        "waiting": 360,
        "blocked": 300,
        "paused": 260,
        "failed": 120,
        "cancelled": 80,
        "completed": 40,
    }.get(status, 0)
    priority = _int(task.get("priority"), 50)
    updated = _epoch(task.get("updated_at")) or _epoch(task.get("created_at")) or 0.0
    age_bonus = min(100.0, max(0.0, time.time() - updated) / 60.0)
    conversation_bonus = 25 if task.get("conversation_id") else 0
    wait_penalty = -180 if status == "paused" else 0
    dependency_penalty = 120 if task.get("dependencies") and status == "queued" else 0
    return status_weight + priority * 2 + conversation_bonus + age_bonus + wait_penalty - dependency_penalty


def _merge_task(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    updated = dict(current)
    mapping = {
        "id": "task_id",
        "label": "title",
        "conversationId": "conversation_id",
        "sessionId": "session_id",
        "assignedWorker": "assigned_worker",
        "parentTaskId": "parent_task_id",
        "externalId": "external_id",
        "scope": "concurrency_key",
        "stopAndWait": "stop_and_wait",
        "topicId": "topic_id",
        "topicTitle": "topic_title",
        "resourceClass": "resource_class",
        "estimatedCost": "estimated_cost",
        "actualCost": "actual_cost",
    }
    for key, value in patch.items():
        target = mapping.get(key, key)
        if target == "status":
            updated[target] = _status(value, str(updated.get(target, "queued")))
        elif target == "progress":
            updated[target] = _percent(value, int(updated.get(target, 0) or 0))
        elif target == "priority":
            updated[target] = _int(value, int(updated.get(target, 50) or 50))
        elif target == "dependencies":
            updated[target] = _string_list(value)
        elif target in {"estimated_cost", "actual_cost"}:
            updated[target] = max(0, _int(value, int(updated.get(target, 0) or 0)))
        elif target == "metadata":
            updated[target] = value if isinstance(value, dict) else {}
        elif target not in {"task_id", "created_at", "priority_score"}:
            updated[target] = value
    now = _now_iso()
    status = str(updated.get("status", "queued"))
    if status == "running" and not updated.get("started_at"):
        updated["started_at"] = now
    if status in TERMINAL_STATUSES and not updated.get("finished_at"):
        updated["finished_at"] = now
    updated["updated_at"] = now
    updated["priority_score"] = task_priority_score(updated)
    return updated


def _replace(tasks: list[dict[str, Any]], task_id: str, updated: dict[str, Any]) -> None:
    for index, item in enumerate(tasks):
        if item.get("task_id") == task_id:
            tasks[index] = updated
            return
    tasks.append(updated)


def _bounded_task_records(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        tasks,
        key=lambda item: str(item.get("updated_at", "")),
        reverse=True,
    )[:500]


def _counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in sorted(VALID_STATUSES)}
    for task in tasks:
        status = str(task.get("status", "queued"))
        counts[status] = counts.get(status, 0) + 1
    counts["active"] = sum(counts.get(status, 0) for status in ACTIVE_STATUSES)
    counts["total"] = len(tasks)
    return counts


def _repair_stale_external_tasks(tasks: list[dict[str, Any]], *, data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    terminal_by_external: dict[str, dict[str, Any]] = {}
    for task in tasks:
        external_id = str(task.get("external_id") or "")
        if not external_id or str(task.get("status") or "") not in TERMINAL_STATUSES:
            continue
        current = terminal_by_external.get(external_id)
        if current is None or str(task.get("updated_at") or "") >= str(current.get("updated_at") or ""):
            terminal_by_external[external_id] = task
    if not terminal_by_external:
        repaired = _repair_bridge_send_tasks(tasks, data_dir=data_dir)
        repaired = _repair_reply_tasks_from_ledger(repaired, data_dir=data_dir)
        repaired = _repair_obsolete_send_backend_blockers(repaired, data_dir=data_dir)
        repaired = _repair_stale_worker_tasks(repaired)
        return _repair_dry_run_send_task_phases(repaired)
    repaired: list[dict[str, Any]] = []
    for task in tasks:
        external_id = str(task.get("external_id") or "")
        terminal = terminal_by_external.get(external_id)
        scope = str(task.get("concurrency_key") or task.get("scope") or "")
        if (
            terminal is not None
            and scope.startswith("weflow:")
            and str(task.get("status") or "") in ACTIVE_STATUSES
        ):
            patched = dict(task)
            patched["status"] = str(terminal.get("status") or "completed")
            patched["progress"] = 100
            patched["phase"] = str(terminal.get("phase") or "后台任务已结束")
            patched["finished_at"] = str(terminal.get("finished_at") or terminal.get("updated_at") or _now_iso())
            patched["updated_at"] = max(str(task.get("updated_at") or ""), str(terminal.get("updated_at") or ""))
            patched["priority_score"] = task_priority_score(patched)
            repaired.append(patched)
        else:
            repaired.append(task)
    repaired = _repair_bridge_send_tasks(repaired, data_dir=data_dir)
    repaired = _repair_reply_tasks_from_ledger(repaired, data_dir=data_dir)
    repaired = _repair_obsolete_send_backend_blockers(repaired, data_dir=data_dir)
    repaired = _repair_stale_worker_tasks(repaired)
    return _repair_dry_run_send_task_phases(repaired)


def _repair_bridge_send_tasks(tasks: list[dict[str, Any]], *, data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    if data_dir is None:
        return tasks
    states = _bridge_ack_states(data_dir)
    if not states:
        return tasks
    repaired: list[dict[str, Any]] = []
    for task in tasks:
        if str(task.get("kind") or "") != "send" or str(task.get("status") or "") not in ACTIVE_STATUSES:
            repaired.append(task)
            continue
        bridge_ids = _task_bridge_ids(task)
        if not bridge_ids:
            repaired.append(task)
            continue
        primary_bridge_id = bridge_ids[0]
        ack_states = {bridge_id: states.get(bridge_id) for bridge_id in bridge_ids}
        pending_bridge_ids = [
            bridge_id
            for bridge_id, ack_state in ack_states.items()
            if ack_state is None or not bool(getattr(ack_state, "terminal", False))
        ]
        if pending_bridge_ids:
            patched = dict(task)
            if not str(patched.get("external_id") or ""):
                patched["external_id"] = primary_bridge_id
                metadata = patched.get("metadata") if isinstance(patched.get("metadata"), dict) else {}
                patched["metadata"] = {**metadata, "bridge_id": primary_bridge_id, "bridge_ids": bridge_ids}
                patched["priority_score"] = task_priority_score(patched)
            repaired.append(patched)
            continue
        aggregate_status = _aggregate_bridge_task_ack_status(
            [str(getattr(ack_state, "status", "") or "") for ack_state in ack_states.values() if ack_state is not None]
        )
        reason = _bridge_task_ack_summary(bridge_ids, ack_states)
        final_status = "completed" if aggregate_status in {"sent", "accepted"} else "failed"
        finished_at = max(
            [
                str((getattr(ack_state, "ack", {}) if isinstance(getattr(ack_state, "ack", {}), dict) else {}).get("created_at") or "")
                for ack_state in ack_states.values()
                if ack_state is not None
            ]
            or [_now_iso()]
        )
        if not finished_at:
            finished_at = _now_iso()
        bridge_acks = _bridge_task_ack_metadata(bridge_ids, ack_states)
        patched = dict(task)
        patched["status"] = final_status
        patched["progress"] = 100
        patched["external_id"] = str(task.get("external_id") or primary_bridge_id)
        patched["phase"] = _bridge_task_phase(final_status, reason, ack_status=aggregate_status)
        patched["detail"] = reason
        patched["last_error"] = "" if final_status == "completed" else reason
        patched["finished_at"] = str(patched.get("finished_at") or finished_at)
        patched["updated_at"] = max(str(task.get("updated_at") or ""), finished_at)
        metadata = patched.get("metadata") if isinstance(patched.get("metadata"), dict) else {}
        patched["metadata"] = {
            **metadata,
            "bridge_id": str(metadata.get("bridge_id") or primary_bridge_id),
            "bridge_ids": bridge_ids,
            "bridge_acks": bridge_acks,
            "aggregate_bridge_status": aggregate_status,
        }
        patched["priority_score"] = task_priority_score(patched)
        repaired.append(patched)
    return repaired


def _repair_reply_tasks_from_ledger(tasks: list[dict[str, Any]], *, data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    if data_dir is None:
        return tasks
    assistant_replies = _assistant_reply_messages(data_dir)
    if not assistant_replies:
        return tasks
    repaired: list[dict[str, Any]] = []
    for task in tasks:
        if str(task.get("kind") or "") != "reply" or str(task.get("status") or "") not in ACTIVE_STATUSES:
            repaired.append(task)
            continue
        message_id = _task_message_id(task)
        reply = assistant_replies.get(message_id)
        if not reply:
            repaired.append(task)
            continue
        send = reply.get("send") if isinstance(reply.get("send"), dict) else {}
        finished_at = str(reply.get("updated_at") or reply.get("created_at") or _now_iso())
        patched = dict(task)
        patched["status"] = "completed"
        patched["progress"] = 100
        patched["phase"] = "reply candidate recorded"
        patched["detail"] = str(send.get("reason") or _entry_text(reply))[:500]
        patched["finished_at"] = finished_at
        patched["updated_at"] = max(str(task.get("updated_at") or ""), finished_at)
        patched["actual_cost"] = max(1, _int(task.get("actual_cost"), 0))
        patched["priority_score"] = task_priority_score(patched)
        repaired.append(patched)
    return repaired


def _repair_stale_worker_tasks(tasks: list[dict[str, Any]], *, now: float | None = None) -> list[dict[str, Any]]:
    """Terminalize explicit worker records whose heartbeat/PID proves they died."""

    current_time = time.time() if now is None else float(now)
    repaired: list[dict[str, Any]] = []
    for task in tasks:
        if str(task.get("status") or "") != "running" or not _is_worker_task(task):
            repaired.append(task)
            continue
        reason = _worker_stale_reason(task, now=current_time)
        if not reason:
            repaired.append(task)
            continue
        final_status = _worker_terminal_status(task, reason)
        patched = dict(task)
        finished_at = _now_iso()
        patched["status"] = final_status
        patched["progress"] = 100
        patched["phase"] = _worker_terminal_phase(task, final_status)
        patched["detail"] = reason
        patched["last_error"] = "" if final_status == "completed" else reason
        patched["finished_at"] = str(patched.get("finished_at") or finished_at)
        patched["updated_at"] = max(str(task.get("updated_at") or ""), str(patched["finished_at"]))
        patched["actual_cost"] = max(1, _int(task.get("actual_cost"), 0))
        metadata = patched.get("metadata") if isinstance(patched.get("metadata"), dict) else {}
        patched["metadata"] = {**metadata, "worker_reconciled": True, "worker_terminal_reason": reason}
        patched["priority_score"] = task_priority_score(patched)
        repaired.append(patched)
    return repaired


def _is_worker_task(task: dict[str, Any]) -> bool:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    if metadata.get("worker") is True:
        return True
    scope = str(task.get("concurrency_key") or task.get("scope") or "")
    external_id = str(task.get("external_id") or "")
    return scope in {"agent:worker", "weflow:pull:worker", "send_bridge:worker"} or external_id in {
        "agent-worker",
        "worker",
        "send-bridge-worker",
    }


def _worker_stale_reason(task: dict[str, Any], *, now: float) -> str:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    pid = _int(metadata.get("worker_pid", metadata.get("pid")), 0)
    if pid > 0:
        try:
            from app.personal_wechat_bot.runtime.process_lock import process_pid_alive

            if not process_pid_alive(pid):
                return f"worker_pid_not_alive:{pid}"
        except Exception:
            return ""
    heartbeat = _float_first(
        metadata.get("worker_heartbeat_at"),
        metadata.get("heartbeat_at"),
        metadata.get("last_heartbeat_at"),
        metadata.get("last_tick_at"),
    )
    if heartbeat <= 0:
        return ""
    stale_after = max(1.0, _float_first(metadata.get("worker_stale_after_seconds"), metadata.get("stale_after_seconds"), 120.0))
    if now - heartbeat > stale_after:
        return f"worker_heartbeat_stale:age={now - heartbeat:.1f}s:limit={stale_after:.1f}s"
    return ""


def _worker_terminal_status(task: dict[str, Any], reason: str) -> str:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    last_status = str(metadata.get("last_status") or task.get("phase") or task.get("detail") or "").lower()
    if "crash" in last_status or "error" in last_status or "failed" in last_status or "heartbeat_stale" in reason:
        return "failed"
    return "completed"


def _worker_terminal_phase(task: dict[str, Any], final_status: str) -> str:
    kind = str(task.get("kind") or "").lower()
    if "weflow" in kind:
        return "WeFlow worker stopped" if final_status == "completed" else "WeFlow worker stale"
    if "bridge" in kind:
        return "send bridge worker stopped" if final_status == "completed" else "send bridge worker stale"
    if "agent" in kind:
        return "agent worker stopped" if final_status == "completed" else "agent worker stale"
    return "worker stopped" if final_status == "completed" else "worker stale"


def _repair_obsolete_send_backend_blockers(
    tasks: list[dict[str, Any]],
    *,
    data_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    if data_dir is None:
        return tasks
    try:
        from app.personal_wechat_bot.config.loader import load_config

        config = load_config(data_dir)
        backend = str(getattr(config, "send_backend", "") or "").strip().lower()
        mode = str(getattr(config, "mode", "") or "").strip().lower()
        confirm_required = bool(getattr(config, "send_confirm_required", True))
    except Exception:
        return tasks
    if backend == "weflow_http":
        return tasks
    worker_config_matched = _bridge_worker_config_is_matched(data_dir)
    approved_queue_message_ids = _approved_queue_message_ids(data_dir)
    markers = (
        "weflow_backend_unavailable",
        "weflow_text_send_not_supported",
        "native-not-implemented",
    )
    repaired: list[dict[str, Any]] = []
    for task in tasks:
        status = str(task.get("status") or "")
        message_id = _task_message_id(task)
        queue_still_approved = bool(message_id and message_id in approved_queue_message_ids)
        evidence = " ".join(str(task.get(key) or "") for key in ("detail", "last_error", "phase"))
        can_repair_terminal_queue_mismatch = (
            queue_still_approved
            and status in TERMINAL_STATUSES
            and (
                "obsolete_send_backend_blocker" in evidence
                or "obsolete_bridge_worker_stale_config" in evidence
                or "bridge_worker_stale_config" in evidence
            )
        )
        if (
            str(task.get("kind") or "") != "send"
            or (status not in ACTIVE_STATUSES and not can_repair_terminal_queue_mismatch)
        ):
            repaired.append(task)
            continue
        reason = ""
        if (
            mode == "auto"
            and not confirm_required
            and status == "queued"
            and not str(task.get("external_id") or "").strip()
            and message_id.startswith("sidebar_channel_test_")
        ):
            reason = "obsolete_sidebar_confirm_test_task:auto_mode_active"
        elif any(marker in evidence for marker in markers):
            reason = f"obsolete_send_backend_blocker:current_backend={backend or 'unknown'}"
        elif "bridge_worker_stale_config" in evidence and worker_config_matched:
            reason = f"obsolete_bridge_worker_stale_config:current_backend={backend or 'unknown'}"
        if not reason:
            repaired.append(task)
            continue
        patched = dict(task)
        now = _now_iso()
        if queue_still_approved and not reason.startswith("obsolete_sidebar_confirm_test_task"):
            patched["status"] = "queued"
            patched["progress"] = max(55, _int(patched.get("progress"), 0))
            patched["phase"] = "发送阻断已解除，等待重新投递"
            patched["detail"] = reason
            patched["blocker"] = ""
            patched["last_error"] = ""
            patched["finished_at"] = ""
            patched["updated_at"] = max(str(task.get("updated_at") or ""), now)
        else:
            patched["status"] = "cancelled" if reason.startswith("obsolete_sidebar_confirm_test_task") else "failed"
            patched["progress"] = 100
            patched["phase"] = "旧发送后端阻断已失效"
            patched["detail"] = reason
            patched["last_error"] = reason
            patched["finished_at"] = str(patched.get("finished_at") or now)
            patched["updated_at"] = max(str(task.get("updated_at") or ""), str(patched["finished_at"]))
        if not str(patched.get("updated_at") or ""):
            patched["updated_at"] = now
        patched["priority_score"] = task_priority_score(patched)
        repaired.append(patched)
    return repaired


def _approved_queue_message_ids(data_dir: str | Path) -> set[str]:
    try:
        from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue

        items = ConfirmQueue(Path(data_dir) / "confirm_queue.jsonl").list_by_status("approved")
    except Exception:
        return set()
    ids: set[str] = set()
    for item in items:
        reply = item.get("reply") if isinstance(item, dict) and isinstance(item.get("reply"), dict) else {}
        message_id = str(reply.get("message_id") or "").strip()
        if message_id:
            ids.add(message_id)
    return ids


def _bridge_worker_config_is_matched(data_dir: str | Path) -> bool:
    try:
        from app.personal_wechat_bot.config.loader import load_config
        from app.personal_wechat_bot.runtime.send_bridge_worker import (
            bridge_worker_config_signature,
            bridge_worker_lock_alive,
            bridge_worker_lock_path,
        )

        root = Path(data_dir)
        if not bridge_worker_lock_alive(root):
            return False
        payload = json.loads(bridge_worker_lock_path(root).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        actual = payload.get("config_signature")
        if not isinstance(actual, dict) or not actual:
            return False
        return actual == bridge_worker_config_signature(load_config(root))
    except Exception:
        return False


def _bridge_ack_states(data_dir: str | Path) -> dict[str, Any]:
    ack_path = Path(data_dir) / "send_bridge" / "acks.jsonl"
    if not ack_path.exists():
        return {}
    try:
        from app.personal_wechat_bot.wechat_driver.bridge_send import effective_bridge_ack_states

        records = []
        for line in ack_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
        return effective_bridge_ack_states(records)
    except Exception:
        return {}


def _assistant_reply_messages(data_dir: str | Path) -> dict[str, dict[str, Any]]:
    try:
        from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore

        store = ConversationLedgerStore(data_dir)
        conversation_ids = store.list_conversation_ids()
    except Exception:
        return {}
    replies: dict[str, dict[str, Any]] = {}
    for conversation_id in conversation_ids:
        try:
            entries = store.read_entries(conversation_id, include_removed=True)
        except Exception:
            continue
        for entry in entries:
            item = asdict(entry)
            if str(item.get("role") or "") != "assistant":
                continue
            message_id = str(item.get("message_id") or "").strip()
            if message_id:
                replies[message_id] = item
    return replies


def _task_bridge_id(task: dict[str, Any]) -> str:
    ids = _task_bridge_ids(task)
    return ids[0] if ids else ""


def _task_bridge_ids(task: dict[str, Any]) -> list[str]:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    ids: list[str] = []
    raw_bridge_ids = metadata.get("bridge_ids") if isinstance(metadata.get("bridge_ids"), list) else []
    ids.extend(
        str(item).strip()
        for item in raw_bridge_ids
        if str(item).strip().startswith("bridge:")
    )
    for value in (
        task.get("external_id"),
        metadata.get("bridge_id"),
        metadata.get("send_reason"),
        task.get("detail"),
        task.get("last_error"),
    ):
        text = str(value or "").strip()
        if text.startswith("bridge:"):
            ids.append(text.strip("，,.;；"))
        ids.extend(match.group("id").strip("，,.;；") for match in _BRIDGE_ID_RE.finditer(text))
    return _dedupe_nonempty([item for item in ids if str(item).startswith("bridge:")])


def _dedupe_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _aggregate_bridge_task_ack_status(statuses: list[str]) -> str:
    normalized = [str(item or "").strip() for item in statuses if str(item or "").strip()]
    if any(item in {"failed", "blocked"} for item in normalized):
        return "failed"
    if any(item == "accepted" for item in normalized):
        return "accepted"
    if normalized and all(item == "sent" for item in normalized):
        return "sent"
    return normalized[-1] if normalized else "failed"


def _bridge_task_ack_summary(bridge_ids: list[str], ack_states: dict[str, Any]) -> str:
    parts: list[str] = []
    for bridge_id in bridge_ids:
        ack_state = ack_states.get(bridge_id)
        status = str(getattr(ack_state, "status", "") or "queued")
        ack = getattr(ack_state, "ack", {}) if ack_state is not None else {}
        ack = ack if isinstance(ack, dict) else {}
        reason = str(ack.get("reason") or f"bridge_ack:{status}")
        parts.append(f"{bridge_id}:{status}:{reason}")
    return "bridge_ack_parts:" + ";".join(parts) if len(parts) > 1 else (parts[0] if parts else "bridge_ack_parts:empty")


def _bridge_task_ack_metadata(bridge_ids: list[str], ack_states: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for bridge_id in bridge_ids:
        ack_state = ack_states.get(bridge_id)
        ack = getattr(ack_state, "ack", {}) if ack_state is not None else {}
        ack = ack if isinstance(ack, dict) else {}
        payload[bridge_id] = {
            "bridge_id": bridge_id,
            "status": str(getattr(ack_state, "status", "") or ""),
            "reason": str(ack.get("reason") or ""),
            "created_at": str(ack.get("created_at") or ""),
        }
    return payload


def _task_message_id(task: dict[str, Any]) -> str:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    return str(task.get("external_id") or metadata.get("message_id") or "").strip()


def _bridge_task_phase(final_status: str, reason: str, *, ack_status: str = "") -> str:
    if final_status == "completed":
        if "dry_run_not_delivered" in str(reason or ""):
            return "send bridge dry-run completed"
        if ack_status == "accepted" or "accepted" in str(reason or ""):
            return "send bridge accepted, unverified"
        return "send bridge delivered"
    return "send bridge failed"


def _entry_text(entry: dict[str, Any]) -> str:
    blocks = entry.get("text_blocks") if isinstance(entry.get("text_blocks"), list) else []
    return "\n".join(str(block.get("text", "")) for block in blocks if isinstance(block, dict) and block.get("text"))


def _repair_dry_run_send_task_phases(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    repaired: list[dict[str, Any]] = []
    for task in tasks:
        if (
            str(task.get("kind") or "") == "send"
            and str(task.get("status") or "") == "completed"
            and "dry_run_not_delivered" in str(task.get("detail") or "")
            and str(task.get("phase") or "") in {"发送完成", "非前台桥发送完成"}
        ):
            patched = dict(task)
            patched["phase"] = "非前台桥演练完成，未投递微信"
            patched["priority_score"] = task_priority_score(patched)
            repaired.append(patched)
        else:
            repaired.append(task)
    return repaired


def _resource_pools(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    pools = {
        "gpu": {"max_parallel": 1, "active": 0, "queued": 0},
        "cpu_io": {"max_parallel": 2, "active": 0, "queued": 0},
        "llm": {"max_parallel": 2, "active": 0, "queued": 0},
        "wechat_io": {"max_parallel": 1, "active": 0, "queued": 0},
    }
    for task in tasks:
        resource = str(task.get("resource_class") or "cpu_io")
        pool = pools.setdefault(resource, {"max_parallel": 1, "active": 0, "queued": 0})
        status = str(task.get("status", ""))
        if status == "running":
            pool["active"] += 1
        elif status == "queued":
            pool["queued"] += 1
    return pools


def _pool_limits(pools: dict[str, Any]) -> dict[str, int]:
    limits: dict[str, int] = {}
    for name, pool in (pools or {}).items():
        payload = pool if isinstance(pool, dict) else {}
        limits[str(name)] = max(1, _int(payload.get("max_parallel"), _default_resource_limit(str(name))))
    return limits


def _dispatch_preview(
    tasks: list[dict[str, Any]],
    *,
    resource_limits: dict[str, int],
    channel_limit: int = 1,
    limit: int = 50,
    channel_controls: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    channel_controls = channel_controls or {}
    tasks = [dict(item) for item in tasks if isinstance(item, dict) and not _is_ephemeral_ui_task(item)]
    active_by_resource = _active_by_resource(tasks)
    active_by_conversation = _active_by_conversation(tasks)
    active_scopes = _active_concurrency_keys(tasks)
    completed_ids = _completed_task_ids(tasks)
    runnable: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for task in sorted(
        tasks,
        key=lambda item: _dispatch_priority_score(item, completed_ids, channel_controls=channel_controls),
        reverse=True,
    ):
        if str(task.get("status") or "") != "queued":
            continue
        reason = _not_runnable_reason(
            task,
            resource_limits=resource_limits,
            active_by_resource=active_by_resource,
            active_by_conversation=active_by_conversation,
            active_scopes=active_scopes,
            completed_ids=completed_ids,
            channel_limit=channel_limit,
            channel_controls=channel_controls,
        )
        conversation_id = str(task.get("conversation_id") or "")
        control = channel_controls.get(conversation_id, {}) if conversation_id else {}
        projection = {
            "task_id": str(task.get("task_id") or ""),
            "title": str(task.get("title") or ""),
            "conversation_id": conversation_id,
            "resource_class": str(task.get("resource_class") or "cpu_io"),
            "concurrency_key": str(task.get("concurrency_key") or ""),
            "priority_score": task_priority_score(task),
            "dispatch_score": _dispatch_priority_score(task, completed_ids, channel_controls=channel_controls),
            "channel_control_mode": str(control.get("mode") or ""),
            "channel_priority": _int(control.get("priority"), 50) if control else 50,
            "channel_pinned": parse_bool(control.get("pinned"), False) if control else False,
            "reason": reason,
        }
        if reason:
            blocked.append(projection)
        else:
            runnable.append(projection)
            resource = str(task.get("resource_class") or "cpu_io")
            active_by_resource[resource] = active_by_resource.get(resource, 0) + 1
            if conversation_id:
                active_by_conversation[conversation_id] = active_by_conversation.get(conversation_id, 0) + 1
            concurrency_key = str(task.get("concurrency_key") or "")
            if _mutex_scope(concurrency_key):
                active_scopes.add(concurrency_key)
    return {
        "schema": "task_dispatch_preview_v1",
        "policy": "queued tasks are runnable only when dependencies, resource slots, channel limit, and concurrency key allow it",
        "channel_limit": max(1, _int(channel_limit, 1)),
        "resource_limits": dict(resource_limits or {}),
        "runnable_count": len(runnable),
        "blocked_count": len(blocked),
        "runnable": runnable[:limit],
        "blocked": blocked[:limit],
    }


def _not_runnable_reason(
    task: dict[str, Any],
    *,
    resource_limits: dict[str, int],
    active_by_resource: dict[str, int],
    active_by_conversation: dict[str, int],
    active_scopes: set[str],
    completed_ids: set[str],
    channel_limit: int,
    channel_controls: dict[str, dict[str, Any]] | None = None,
) -> str:
    conversation_id = str(task.get("conversation_id") or "")
    control_reason = _channel_control_block_reason(conversation_id, channel_controls or {})
    if control_reason:
        return control_reason
    if not _dependencies_satisfied(task, completed_ids):
        missing = [item for item in _string_list(task.get("dependencies")) if item not in completed_ids]
        return "waiting_for_dependencies:" + ",".join(missing)
    resource = str(task.get("resource_class") or "cpu_io")
    resource_limit = max(1, _int(resource_limits.get(resource), _default_resource_limit(resource)))
    if active_by_resource.get(resource, 0) >= resource_limit:
        return f"resource_busy:{resource}"
    if conversation_id and active_by_conversation.get(conversation_id, 0) >= max(1, _int(channel_limit, 1)):
        return f"channel_busy:{conversation_id}"
    concurrency_key = str(task.get("concurrency_key") or "")
    if _mutex_scope(concurrency_key) and concurrency_key in active_scopes:
        return f"concurrency_key_busy:{concurrency_key}"
    return ""


def _dispatch_priority_score(
    task: dict[str, Any],
    completed_ids: set[str],
    *,
    channel_controls: dict[str, dict[str, Any]] | None = None,
) -> float:
    score = task_priority_score(task)
    score += _channel_priority_bonus(task, channel_controls or {})
    if _string_list(task.get("dependencies")) and _dependencies_satisfied(task, completed_ids):
        score += 120
    return score


def _load_channel_controls(data_dir: str | Path) -> dict[str, dict[str, Any]]:
    state_path = Path(data_dir) / "channel_state.sqlite"
    if not state_path.exists():
        return {}
    try:
        states = ChannelStateStore(data_dir).list_states(limit=10000)
    except Exception:
        return {}
    controls: dict[str, dict[str, Any]] = {}
    for state in states:
        if not isinstance(state, dict):
            continue
        conversation_id = str(state.get("conversation_id") or "").strip()
        control = state.get("control") if isinstance(state.get("control"), dict) else {}
        if not conversation_id:
            continue
        controls[conversation_id] = {
            "mode": normalize_control_mode(control.get("mode")),
            "priority": max(0, min(100, _int(control.get("priority"), 50))),
            "pinned": parse_bool(control.get("pinned"), False),
            "snoozed_until": str(control.get("snoozed_until") or ""),
            "wait_reason": str(control.get("wait_reason") or ""),
            "operator_note": str(control.get("operator_note") or ""),
        }
    return controls


def _channel_priority_bonus(task: dict[str, Any], channel_controls: dict[str, dict[str, Any]]) -> int:
    conversation_id = str(task.get("conversation_id") or "").strip()
    if not conversation_id:
        return 0
    control = channel_controls.get(conversation_id)
    if not control:
        return 0
    pinned_bonus = 180 if parse_bool(control.get("pinned"), False) else 0
    return pinned_bonus + (_int(control.get("priority"), 50) - 50) * 2


def _channel_control_block_reason(conversation_id: str, channel_controls: dict[str, dict[str, Any]]) -> str:
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        return ""
    control = channel_controls.get(conversation_id)
    if not control:
        return ""
    mode = normalize_control_mode(control.get("mode"))
    if mode == "paused":
        return f"channel_paused:{conversation_id}"
    if mode == "snoozed" and snooze_is_active(control.get("snoozed_until")):
        until = str(control.get("snoozed_until") or "").strip()
        return f"channel_snoozed:{conversation_id}:{until}" if until else f"channel_snoozed:{conversation_id}"
    return ""


def _active_by_resource(tasks: list[dict[str, Any]]) -> dict[str, int]:
    active: dict[str, int] = {}
    for task in tasks:
        if str(task.get("status") or "") != "running":
            continue
        resource = str(task.get("resource_class") or "cpu_io")
        active[resource] = active.get(resource, 0) + 1
    return active


def _active_by_conversation(tasks: list[dict[str, Any]]) -> dict[str, int]:
    active: dict[str, int] = {}
    for task in tasks:
        if str(task.get("status") or "") != "running":
            continue
        conversation_id = str(task.get("conversation_id") or "")
        if not conversation_id:
            continue
        active[conversation_id] = active.get(conversation_id, 0) + 1
    return active


def _active_concurrency_keys(tasks: list[dict[str, Any]]) -> set[str]:
    scopes: set[str] = set()
    for task in tasks:
        if str(task.get("status") or "") != "running":
            continue
        key = str(task.get("concurrency_key") or "")
        if _mutex_scope(key):
            scopes.add(key)
    return scopes


def _completed_task_ids(tasks: list[dict[str, Any]]) -> set[str]:
    return {
        str(task.get("task_id") or "")
        for task in tasks
        if str(task.get("status") or "") == "completed" and str(task.get("task_id") or "")
    }


def _dependencies_satisfied(task: dict[str, Any], completed_ids: set[str]) -> bool:
    dependencies = _string_list(task.get("dependencies"))
    return all(item in completed_ids for item in dependencies)


def _mutex_scope(scope: str) -> bool:
    value = str(scope or "").strip()
    if not value:
        return False
    return not value.startswith(("global", "diagnostic:", "ui:", "audit:", "history:"))


def _default_resource_limit(resource: str) -> int:
    return {
        "gpu": 1,
        "wechat_io": 1,
        "send_bridge": 1,
        "file_io": 1,
        "media_cpu": 2,
        "cpu_io": 2,
        "llm": 1,
        "llm_interactive": 1,
        "llm_background": 1,
    }.get(str(resource or ""), 1)


def _channel_lanes(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        if _is_local_ui_or_non_lane_task(task):
            continue
        conversation_id = str(task.get("conversation_id") or "")
        if not conversation_id:
            continue
        grouped.setdefault(conversation_id, []).append(task)
    lanes: list[dict[str, Any]] = []
    for conversation_id, items in grouped.items():
        ordered = sorted(items, key=task_priority_score, reverse=True)
        active = [item for item in ordered if item.get("status") in ACTIVE_STATUSES]
        terminal = [item for item in ordered if item.get("status") in TERMINAL_STATUSES]
        current = active[0] if active else {}
        topics = _topic_history(items)
        lanes.append(
            {
                "conversation_id": conversation_id,
                "current_topic": {
                    "topic_id": str(current.get("topic_id") or ""),
                    "title": str(current.get("topic_title") or current.get("title") or ""),
                    "priority": _int(current.get("priority"), 0),
                    "status": str(current.get("status") or "idle"),
                },
                "topic_history": topics[:12],
                "counts": _counts(items),
                "active": active[:8],
                "waiting": [item for item in active if item.get("status") in {"waiting", "blocked"}][:8],
                "paused": [item for item in active if item.get("status") == "paused"][:8],
                "history": terminal[:8],
                "resource_audit": {
                    "estimated_cost": sum(_int(item.get("estimated_cost"), 0) for item in items),
                    "actual_cost": sum(_int(item.get("actual_cost"), 0) for item in items),
                    "resources": _resource_pools(items),
                },
            }
        )
    return sorted(lanes, key=lambda item: task_priority_score((item.get("active") or [{}])[0] if item.get("active") else {}), reverse=True)


def _topic_history(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    topics: dict[str, dict[str, Any]] = {}
    for task in tasks:
        topic_id = str(task.get("topic_id") or task.get("concurrency_key") or task.get("task_id") or "")
        if not topic_id:
            continue
        topic = topics.setdefault(
            topic_id,
            {
                "topic_id": topic_id,
                "title": str(task.get("topic_title") or task.get("title") or ""),
                "task_count": 0,
                "active_count": 0,
                "terminal_count": 0,
                "max_priority": 0,
                "updated_at": "",
            },
        )
        topic["task_count"] += 1
        status = str(task.get("status") or "")
        if status in ACTIVE_STATUSES:
            topic["active_count"] += 1
        if status in TERMINAL_STATUSES:
            topic["terminal_count"] += 1
        topic["max_priority"] = max(int(topic["max_priority"]), _int(task.get("priority"), 0))
        topic["updated_at"] = max(str(topic.get("updated_at") or ""), str(task.get("updated_at") or ""))
    return sorted(topics.values(), key=lambda item: (int(item.get("active_count", 0)), int(item.get("max_priority", 0)), str(item.get("updated_at", ""))), reverse=True)


def _is_ephemeral_ui_task(task: dict[str, Any]) -> bool:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    scope = str(task.get("concurrency_key") or task.get("scope") or "")
    if metadata.get("ephemeral") is True:
        return True
    if metadata.get("local_ui") is True:
        return True
    return False


def _is_local_ui_or_non_lane_task(task: dict[str, Any]) -> bool:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    scope = str(task.get("concurrency_key") or task.get("scope") or "")
    if metadata.get("local_ui") is True:
        return True
    return _is_non_lane_scope(scope)


def _is_non_lane_scope(scope: str) -> bool:
    value = str(scope or "")
    return value.startswith(
        (
            "diagnostic:",
            "agent:",
            "ui:",
            "weflow:",
            "queue:",
            "send-review:",
            "settings:",
            "audit:",
            "history:",
            "channels:",
        )
    )


def _status(value: Any, default: str) -> str:
    status = str(value or default).strip().lower()
    return status if status in VALID_STATUSES else default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_first(*values: Any) -> float:
    for value in values:
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _percent(value: Any, default: int) -> int:
    return max(0, min(100, _int(value, default)))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _clean_id(value: Any) -> str:
    return "".join(ch for ch in str(value or "").strip() if ch.isalnum() or ch in {"-", "_", "."})[:80]


def _epoch(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0

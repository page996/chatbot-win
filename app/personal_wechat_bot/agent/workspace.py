from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


TERMINAL_STATUSES = {"completed", "failed", "canceled", "archived"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}"


def read_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


class PlanBookStore:
    def __init__(self, data_dir: str | Path):
        self.workspace_root = Path(data_dir) / "agent_workspace"
        self.plans_root = self.workspace_root / "plans"

    def create_plan(
        self,
        conversation_id: str,
        thread_id: str,
        goal: str,
        concurrency_policy: dict[str, Any] | None = None,
    ) -> str:
        plan_id = new_id("plan")
        plan_dir = self.plan_dir(plan_id)
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan = {
            "plan_id": plan_id,
            "conversation_id": conversation_id,
            "thread_id": thread_id,
            "status": "active",
            "goal": goal,
            "current_revision": 0,
            "concurrency_policy": concurrency_policy or {"max_parallel_workers": 2, "overflow": "queue"},
            "revisions": [],
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        write_json(plan_dir / "plan.json", plan)
        self.append_revision(plan_id, "initial_plan", f"Create plan: {goal}")
        return plan_id

    def append_revision(
        self,
        plan_id: str,
        kind: str,
        decision: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        plan = self.read_plan(plan_id)
        revision_number = int(plan.get("current_revision", 0)) + 1
        revision = {
            "revision": revision_number,
            "kind": kind,
            "decision": decision,
            "metadata": metadata or {},
            "created_at": now_iso(),
        }
        plan["current_revision"] = revision_number
        plan.setdefault("revisions", []).append(revision)
        plan["updated_at"] = now_iso()
        plan_dir = self.plan_dir(plan_id)
        write_json(plan_dir / "plan.json", plan)
        append_jsonl(plan_dir / "revisions.jsonl", revision)
        append_jsonl(plan_dir / "events.jsonl", {"type": "plan_revision", **revision})
        return revision_number

    def read_plan(self, plan_id: str) -> dict[str, Any]:
        plan = read_json(self.plan_dir(plan_id) / "plan.json")
        if not isinstance(plan, dict):
            raise FileNotFoundError(f"missing plan: {plan_id}")
        return plan

    def plan_dir(self, plan_id: str) -> Path:
        return self.plans_root / plan_id


class TaskWorkspaceStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.workspace_root = self.data_dir / "agent_workspace"
        self.tasks_root = self.workspace_root / "tasks"

    def create_task(
        self,
        plan_id: str,
        plan_revision: int,
        agent_type: str,
        conversation_id: str,
        thread_id: str,
        user_goal: str,
        instructions: dict[str, Any] | None = None,
        input_refs: list[dict[str, Any]] | None = None,
        context_refs: list[dict[str, Any]] | None = None,
    ) -> str:
        task_id = new_id("task")
        task_dir = self.task_dir(task_id)
        for child in ["input_files", "working", "output", "debug"]:
            (task_dir / child).mkdir(parents=True, exist_ok=True)
        request = {
            "task_id": task_id,
            "plan_id": plan_id,
            "plan_revision": plan_revision,
            "parent_task_id": None,
            "conversation_id": conversation_id,
            "thread_id": thread_id,
            "agent_type": agent_type,
            "user_goal": user_goal,
            "instructions": instructions or {},
            "input_refs": input_refs or [],
            "context_refs": context_refs or [],
            "created_at": now_iso(),
        }
        write_json(task_dir / "request.json", request)
        self.record_status(task_id, "created", detail="Task workspace created.")
        return task_id

    def record_status(
        self,
        task_id: str,
        status: str,
        detail: str = "",
        progress: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        task_dir = self.task_dir(task_id)
        previous = read_json(task_dir / "status.json", {})
        payload = {
            "task_id": task_id,
            "status": status,
            "detail": detail,
            "progress": progress or {},
            "metadata": metadata or {},
            "previous_status": previous.get("status") if isinstance(previous, dict) else None,
            "updated_at": now_iso(),
        }
        write_json(task_dir / "status.json", payload)
        append_jsonl(task_dir / "events.jsonl", {"type": "status", **payload})

    def read_status(self, task_id: str) -> dict[str, Any]:
        status = read_json(self.task_dir(task_id) / "status.json")
        if not isinstance(status, dict):
            raise FileNotFoundError(f"missing status: {task_id}")
        return status

    def read_request(self, task_id: str) -> dict[str, Any]:
        request = read_json(self.task_dir(task_id) / "request.json")
        if not isinstance(request, dict):
            raise FileNotFoundError(f"missing request: {task_id}")
        return request

    def write_report(self, task_id: str, report: dict[str, Any]) -> None:
        payload = {"task_id": task_id, "created_at": now_iso(), **report}
        write_json(self.task_dir(task_id) / "report.json", payload)
        append_jsonl(self.task_dir(task_id) / "events.jsonl", {"type": "report", **payload})

    def read_report(self, task_id: str) -> dict[str, Any]:
        report = read_json(self.task_dir(task_id) / "report.json")
        if not isinstance(report, dict):
            raise FileNotFoundError(f"missing report: {task_id}")
        return report

    def read_events(self, task_id: str) -> list[dict[str, Any]]:
        events_path = self.task_dir(task_id) / "events.jsonl"
        if not events_path.exists():
            return []
        events: list[dict[str, Any]] = []
        with events_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    events.append(json.loads(line))
        return events

    def task_dir(self, task_id: str) -> Path:
        return self.tasks_root / task_id

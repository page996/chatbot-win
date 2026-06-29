from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.agent.monitor import TaskMonitor
from app.personal_wechat_bot.agent.worker_queue import LocalWorkerQueue
from app.personal_wechat_bot.agent.workspace import PlanBookStore, TaskWorkspaceStore
from app.personal_wechat_bot.domain.models import ToolCallRequest, ToolCallResult


class ToolTaskOrchestrator:
    def __init__(
        self,
        data_dir: str | Path,
        max_parallel: int = 2,
        timeout_seconds: float = 120.0,
        max_retries: int = 1,
        worker_module: str = "app.personal_wechat_bot.agent.tool_worker",
    ):
        self.data_dir = Path(data_dir)
        self.plan_store = PlanBookStore(self.data_dir)
        self.task_store = TaskWorkspaceStore(self.data_dir)
        self.monitor = TaskMonitor(self.task_store)
        self.max_parallel = max_parallel
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.worker_module = worker_module

    def execute(self, request: ToolCallRequest) -> ToolCallResult:
        plan_id = self.plan_store.create_plan(
            conversation_id=request.conversation_id,
            thread_id=f"tool:{request.tool_name}",
            goal=f"Execute tool task: {request.tool_name}",
        )
        last_task_id = ""
        last_summary = "工具 worker 执行失败"
        last_report: dict[str, Any] | None = None
        for attempt in range(self.max_retries + 1):
            kind = "dispatch" if attempt == 0 else "repair"
            decision = (
                f"Dispatch {request.tool_name} to short-process worker."
                if attempt == 0
                else f"Retry {request.tool_name} after abnormal worker state."
            )
            revision = self.plan_store.append_revision(
                plan_id,
                kind,
                decision,
                metadata={"call_id": request.call_id, "attempt": attempt + 1, "retry_of": last_task_id or None},
            )
            task_id = self.task_store.create_task(
                plan_id=plan_id,
                plan_revision=revision,
                agent_type="tool_worker",
                conversation_id=request.conversation_id,
                thread_id=f"tool:{request.tool_name}",
                user_goal=f"Execute {request.tool_name}",
                instructions={
                    "tool_request": asdict(request),
                    "attempt": attempt + 1,
                    "retry_of": last_task_id or None,
                },
            )
            last_task_id = task_id
            queue = LocalWorkerQueue(self.task_store, max_parallel=self.max_parallel)
            queue.enqueue(task_id, worker_module=self.worker_module)
            result = queue.run_until_idle(timeout_seconds=self.timeout_seconds)
            snapshot = self.monitor.snapshot(task_id)
            try:
                last_report = self.task_store.read_report(task_id)
            except FileNotFoundError:
                last_report = None

            if result.completed > 0 and last_report is not None:
                self.plan_store.append_revision(
                    plan_id,
                    "completed",
                    f"Tool worker completed for {request.tool_name}.",
                    metadata={"task_id": task_id, "call_id": request.call_id, "attempt": attempt + 1},
                )
                return self._result_from_report(request, task_id, last_report)

            last_summary = "工具 worker 未返回报告" if last_report is None else "工具 worker 执行失败"
            if attempt < self.max_retries:
                self.plan_store.append_revision(
                    plan_id,
                    "repair_prepare",
                    f"Prepare retry for {request.tool_name}.",
                    metadata={
                        "task_id": task_id,
                        "call_id": request.call_id,
                        "attempt": attempt + 1,
                        "status": snapshot.status,
                        "event_count": snapshot.event_count,
                    },
                )
                continue

        self.plan_store.append_revision(
            plan_id,
            "failed",
            f"Tool worker failed for {request.tool_name}.",
            metadata={"task_id": last_task_id, "call_id": request.call_id},
        )
        if last_report is not None:
            return self._result_from_report(request, last_task_id, last_report)
        return self._failed_result(request, last_task_id, last_summary)

    def _result_from_report(self, request: ToolCallRequest, task_id: str, report: dict[str, Any]) -> ToolCallResult:
        raw = report.get("payload", {}).get("tool_result")
        if isinstance(raw, dict):
            return ToolCallResult(
                call_id=str(raw.get("call_id", request.call_id)),
                tool_name=str(raw.get("tool_name", request.tool_name)),
                status=raw.get("status", "failed"),
                summary=str(raw.get("summary", report.get("summary", ""))),
                output_refs=list(raw.get("output_refs", report.get("output_refs", []))),
                error=raw.get("error"),
                completed_at=str(raw.get("completed_at", report.get("created_at", ""))),
                payload={**dict(raw.get("payload", {})), "agent_task_id": task_id},
            )
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status=report.get("status", "failed"),
            summary=str(report.get("summary", "")),
            output_refs=list(report.get("output_refs", [])),
            payload={"agent_task_id": task_id},
        )

    def _failed_result(self, request: ToolCallRequest, task_id: str, summary: str) -> ToolCallResult:
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status="failed",
            summary=summary,
            error="tool_worker_failed",
            payload={"agent_task_id": task_id},
        )

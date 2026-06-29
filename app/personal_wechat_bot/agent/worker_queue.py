from __future__ import annotations

import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass

from app.personal_wechat_bot.agent.workspace import TERMINAL_STATUSES, TaskWorkspaceStore


@dataclass(frozen=True)
class WorkerQueueResult:
    started: int
    completed: int
    failed: int
    max_running_seen: int


@dataclass
class _QueuedTask:
    task_id: str
    worker_module: str


@dataclass
class _RunningTask:
    task_id: str
    process: subprocess.Popen


class LocalWorkerQueue:
    def __init__(
        self,
        workspace: TaskWorkspaceStore,
        max_parallel: int = 2,
        python_executable: str | None = None,
    ):
        if max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")
        self.workspace = workspace
        self.max_parallel = max_parallel
        self.python_executable = python_executable or sys.executable
        self._pending: deque[_QueuedTask] = deque()

    def enqueue(self, task_id: str, worker_module: str = "app.personal_wechat_bot.agent.fake_worker") -> None:
        self.workspace.record_status(task_id, "queued", detail="Task queued for local worker.")
        self._pending.append(_QueuedTask(task_id=task_id, worker_module=worker_module))

    def run_until_idle(self, timeout_seconds: float = 30.0, poll_interval_seconds: float = 0.05) -> WorkerQueueResult:
        running: list[_RunningTask] = []
        started = 0
        completed = 0
        failed = 0
        max_running_seen = 0
        deadline = time.monotonic() + timeout_seconds

        while self._pending or running:
            if time.monotonic() > deadline:
                failed += self._fail_running_tasks(running, "Worker queue timeout.")
                break

            while self._pending and len(running) < self.max_parallel:
                queued = self._pending.popleft()
                self.workspace.record_status(
                    queued.task_id,
                    "assigned",
                    detail="Task assigned to short process worker.",
                    metadata={"worker_module": queued.worker_module},
                )
                process = subprocess.Popen(
                    [
                        self.python_executable,
                        "-m",
                        queued.worker_module,
                        "--data-dir",
                        str(self.workspace.data_dir),
                        "--task-id",
                        queued.task_id,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                running.append(_RunningTask(task_id=queued.task_id, process=process))
                started += 1
                max_running_seen = max(max_running_seen, len(running))

            for item in list(running):
                exit_code = item.process.poll()
                if exit_code is None:
                    continue
                running.remove(item)
                task_status = self.workspace.read_status(item.task_id).get("status")
                if exit_code == 0 and task_status == "completed":
                    completed += 1
                    continue
                if task_status not in TERMINAL_STATUSES:
                    self.workspace.record_status(
                        item.task_id,
                        "failed",
                        detail=f"Worker exited without completed status. exit_code={exit_code}",
                    )
                failed += 1

            if self._pending or running:
                time.sleep(poll_interval_seconds)

        return WorkerQueueResult(
            started=started,
            completed=completed,
            failed=failed,
            max_running_seen=max_running_seen,
        )

    def _fail_running_tasks(self, running: list[_RunningTask], detail: str) -> int:
        failed = 0
        for item in list(running):
            if item.process.poll() is None:
                item.process.kill()
            task_status = self.workspace.read_status(item.task_id).get("status")
            if task_status not in TERMINAL_STATUSES:
                self.workspace.record_status(item.task_id, "failed", detail=detail)
            running.remove(item)
            failed += 1
        while self._pending:
            queued = self._pending.popleft()
            self.workspace.record_status(queued.task_id, "failed", detail=detail)
            failed += 1
        return failed

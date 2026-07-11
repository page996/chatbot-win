from __future__ import annotations

import os
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass

from app.personal_wechat_bot.agent.workspace import TERMINAL_STATUSES, TaskWorkspaceStore
from app.personal_wechat_bot.runtime.history_fence import (
    HistoryWriterStartupHandoff,
    history_writer_fence_if_owned,
    history_writer_lease_if_owned,
    register_history_writer_startup_handoff_if_owned,
)
from app.personal_wechat_bot.runtime.process_lock import process_start_marker


_WORKER_STARTUP_TIMEOUT_SECONDS = 5.0
_WORKER_STARTUP_HANDOFF_TTL_SECONDS = 15.0


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
        with history_writer_fence_if_owned(
            self.workspace.data_dir,
            label="local_worker_queue_enqueue",
        ):
            self.workspace.record_status(task_id, "queued", detail="Task queued for local worker.")
            self._pending.append(_QueuedTask(task_id=task_id, worker_module=worker_module))

    def run_until_idle(self, timeout_seconds: float = 30.0, poll_interval_seconds: float = 0.05) -> WorkerQueueResult:
        with history_writer_lease_if_owned(
            self.workspace.data_dir,
            label="local_worker_queue",
        ):
            return self._run_until_idle_leased(
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )

    def _run_until_idle_leased(
        self,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> WorkerQueueResult:
        running: list[_RunningTask] = []
        started = 0
        completed = 0
        failed = 0
        max_running_seen = 0
        deadline = time.monotonic() + timeout_seconds

        try:
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
                    handoff: HistoryWriterStartupHandoff | None = None
                    process: subprocess.Popen | None = None
                    worker_process_start = ""
                    startup_approved = False
                    try:
                        handoff = register_history_writer_startup_handoff_if_owned(
                            self.workspace.data_dir,
                            label="local_worker_startup",
                            metadata={
                                "task_id": queued.task_id,
                                "worker_module": queued.worker_module,
                            },
                            ttl_seconds=_WORKER_STARTUP_HANDOFF_TTL_SECONDS,
                        )
                        popen_options = _worker_process_options()
                        if handoff is not None:
                            child_environment = os.environ.copy()
                            child_environment.update(handoff.child_environment())
                            popen_options["env"] = child_environment
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
                            **popen_options,
                        )
                        started += 1
                        worker_process_start = process_start_marker(
                            int(getattr(process, "pid", 0) or 0)
                        )
                        if not worker_process_start:
                            raise RuntimeError("worker process identity is unavailable")
                        if handoff is not None:
                            _wait_for_worker_startup(
                                process,
                                handoff,
                                expected_process_start=worker_process_start,
                                timeout_seconds=min(
                                    _WORKER_STARTUP_TIMEOUT_SECONDS,
                                    max(0.0, deadline - time.monotonic()),
                                ),
                                poll_interval_seconds=poll_interval_seconds,
                            )
                            startup_approved = True
                    except Exception as exc:
                        termination_error: Exception | None = None
                        if process is not None:
                            try:
                                _terminate_worker_process(process)
                            except Exception as cleanup_exc:
                                termination_error = cleanup_exc
                        self.workspace.record_status(
                            queued.task_id,
                            "failed",
                            detail=(
                                f"Worker failed to start: {type(exc).__name__}: {exc}"
                                + (
                                    f"; cleanup failed: {type(termination_error).__name__}: {termination_error}"
                                    if termination_error is not None
                                    else ""
                                )
                            ),
                        )
                        failed += 1
                        if termination_error is not None:
                            raise termination_error
                        continue
                    except BaseException:
                        if process is not None:
                            _terminate_worker_process(process)
                        raise
                    finally:
                        if handoff is not None:
                            if startup_approved:
                                handoff.release()
                            else:
                                handoff.cancel()
                    if process is None:  # pragma: no cover - Popen returns or raises
                        raise RuntimeError("worker process was not created")
                    running.append(
                        _RunningTask(
                            task_id=queued.task_id,
                            process=process,
                        )
                    )
                    max_running_seen = max(max_running_seen, len(running))

                for item in list(running):
                    exit_code = item.process.poll()
                    if exit_code is None:
                        continue
                    _wait_finished_process(item.process)
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
        finally:
            if self._pending or running:
                self._fail_running_tasks(running, "Worker queue interrupted.")

        return WorkerQueueResult(
            started=started,
            completed=completed,
            failed=failed,
            max_running_seen=max_running_seen,
        )

    def _fail_running_tasks(self, running: list[_RunningTask], detail: str) -> int:
        failed = 0
        for item in list(running):
            _terminate_worker_process(item.process)
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


def _worker_process_options() -> dict[str, object]:
    if os.name == "nt":
        return {
            "creationflags": int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        }
    return {}


def _wait_finished_process(process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=0.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _wait_for_worker_startup(
    process: subprocess.Popen,
    handoff: HistoryWriterStartupHandoff,
    *,
    expected_process_start: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> None:
    timeout = max(0.0, float(timeout_seconds))
    deadline = time.monotonic() + timeout
    while True:
        if handoff.ready_for_process(
            int(getattr(process, "pid", 0) or 0),
            expected_process_start=expected_process_start,
        ):
            return
        exit_code = process.poll()
        if exit_code is not None:
            _wait_finished_process(process)
            raise RuntimeError(f"worker exited before startup handoff; exit_code={exit_code}")
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise TimeoutError("worker startup handoff timed out")
        time.sleep(min(max(0.005, float(poll_interval_seconds)), remaining))


def _terminate_worker_process(
    process: subprocess.Popen,
    *,
    grace_seconds: float = 2.0,
) -> None:
    if process.poll() is not None:
        _wait_finished_process(process)
        return
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=max(0.1, grace_seconds))
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=max(0.1, grace_seconds))
    except (OSError, subprocess.TimeoutExpired):
        # The worker's Windows Job Object closes with the direct child and
        # terminates descendants. Make one final handle-based reap attempt.
        try:
            process.kill()
            process.wait(timeout=max(0.1, grace_seconds))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("worker process could not be reaped") from exc

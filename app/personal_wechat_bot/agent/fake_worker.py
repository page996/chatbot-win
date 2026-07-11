from __future__ import annotations

import argparse
import time

from app.personal_wechat_bot.agent.workspace import TaskWorkspaceStore
from app.personal_wechat_bot.runtime.history_fence import (
    history_writer_lease_after_startup_handoff_if_owned,
)
from app.personal_wechat_bot.runtime.worker_job import ensure_worker_descendant_job


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fake short-process worker for agent orchestration tests.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--task-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_worker_descendant_job()
    with history_writer_lease_after_startup_handoff_if_owned(
        args.data_dir,
        label="fake_agent_worker",
        metadata={"task_id": args.task_id},
    ):
        return _run_worker(args)


def _run_worker(args: argparse.Namespace) -> int:
    workspace = TaskWorkspaceStore(args.data_dir)
    request = workspace.read_request(args.task_id)
    instructions = request.get("instructions", {})
    workspace.record_status(args.task_id, "running", detail="Fake worker started.")

    sleep_seconds = float(instructions.get("sleep_seconds", 0))
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)

    if instructions.get("fail"):
        workspace.write_report(
            args.task_id,
            {
                "status": "failed",
                "summary": "Fake worker failed as requested.",
                "output_refs": [],
                "source_refs": [],
                "main_agent_requests": [],
                "debug": {"reason": "requested_failure"},
            },
        )
        workspace.record_status(args.task_id, "failed", detail="Fake worker failed as requested.")
        return 1

    workspace.write_report(
        args.task_id,
        {
            "status": "completed",
            "summary": f"Fake worker completed {request['agent_type']} task.",
            "output_refs": [],
            "source_refs": [],
            "main_agent_requests": [],
            "debug": {"worker": "fake_worker"},
        },
    )
    workspace.record_status(args.task_id, "completed", detail="Fake worker completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

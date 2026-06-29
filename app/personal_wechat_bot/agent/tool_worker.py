from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from app.personal_wechat_bot.config.loader import load_config
from app.personal_wechat_bot.agent.workspace import TaskWorkspaceStore
from app.personal_wechat_bot.domain.models import ToolCallRequest
from app.personal_wechat_bot.logging.event_log import EventLogger
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.defaults import register_default_tools
from app.personal_wechat_bot.tools.registry import ToolRegistry
from app.personal_wechat_bot.tools.runtime import ToolRuntime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Short-process worker for tool execution tasks.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--task-id", required=True)
    return parser.parse_args()


def build_tool_runtime(data_dir: Path) -> ToolRuntime:
    config = load_config(data_dir)
    file_index = FileIndex(data_dir / "file_index.sqlite")
    logger = EventLogger(data_dir / "logs.jsonl")
    registry = ToolRegistry()
    register_default_tools(registry, data_root=data_dir, config=config, file_index=file_index)
    return ToolRuntime(registry, logger)


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir)
    workspace = TaskWorkspaceStore(data_dir)
    request = workspace.read_request(args.task_id)
    tool_payload = request.get("instructions", {}).get("tool_request")
    if not isinstance(tool_payload, dict):
        workspace.record_status(args.task_id, "failed", detail="Missing tool_request payload.")
        return 1

    workspace.record_status(args.task_id, "running", detail="Tool worker started.")
    tool_request = ToolCallRequest(**tool_payload)
    result = build_tool_runtime(data_dir).execute(tool_request)
    status = "completed" if result.status == "completed" else "failed"
    workspace.write_report(
        args.task_id,
        {
            "status": status,
            "summary": result.summary,
            "output_refs": list(result.output_refs),
            "source_refs": [],
            "main_agent_requests": [],
            "debug": {"tool_name": result.tool_name, "tool_status": result.status},
            "payload": {"tool_result": asdict(result)},
        },
    )
    workspace.record_status(args.task_id, status, detail=f"Tool worker finished with {result.status}.")
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

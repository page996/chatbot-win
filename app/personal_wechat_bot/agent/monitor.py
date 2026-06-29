from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.personal_wechat_bot.agent.workspace import TERMINAL_STATUSES, TaskWorkspaceStore


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    status: str
    is_terminal: bool
    event_count: int
    updated_at: str
    detail: str
    status_payload: dict[str, Any]


class TaskMonitor:
    def __init__(self, workspace: TaskWorkspaceStore):
        self.workspace = workspace

    def snapshot(self, task_id: str) -> TaskSnapshot:
        status_payload = self.workspace.read_status(task_id)
        status = str(status_payload.get("status", "unknown"))
        events = self.workspace.read_events(task_id)
        return TaskSnapshot(
            task_id=task_id,
            status=status,
            is_terminal=status in TERMINAL_STATUSES,
            event_count=len(events),
            updated_at=str(status_payload.get("updated_at", "")),
            detail=str(status_payload.get("detail", "")),
            status_payload=status_payload,
        )

    def status_history(self, task_id: str) -> list[str]:
        return [
            str(event["status"])
            for event in self.workspace.read_events(task_id)
            if event.get("type") == "status" and "status" in event
        ]

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.personal_wechat_bot.runtime.history_fence import (
    HistoryWriterLease,
    HistoryWriterLeaseGroup,
    history_writer_fence_if_owned,
    register_history_writer_lease_if_owned,
)


class LoopRunner(Protocol):
    def run_once(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RunnerTick:
    name: str
    status: str
    processed_count: int
    detail: dict[str, Any] = field(default_factory=dict)


class AgentRunner:
    def __init__(
        self,
        runners: list[tuple[str, LoopRunner]],
        *,
        poll_interval_seconds: float = 1.0,
        data_dir: str | Path | None = None,
    ):
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        self.runners = runners
        self.poll_interval_seconds = poll_interval_seconds
        self.data_dir = Path(data_dir) if data_dir is not None else None

    def run_once(self) -> dict[str, Any]:
        if self.data_dir is None:
            return self._run_once_fenced()
        with history_writer_fence_if_owned(
            self.data_dir,
            label="agent_runner",
        ):
            return self._run_once_fenced()

    def _run_once_fenced(self) -> dict[str, Any]:
        ticks: list[RunnerTick] = []
        total_processed = 0
        for name, runner in self.runners:
            try:
                result = runner.run_once()
            except Exception as exc:
                ticks.append(
                    RunnerTick(
                        name=name,
                        status="error",
                        processed_count=0,
                        detail={"type": type(exc).__name__, "message": str(exc)},
                    )
                )
                continue
            processed_count = _processed_count(result)
            total_processed += processed_count
            ticks.append(
                RunnerTick(
                    name=name,
                    status=str(result.get("status", "unknown")),
                    processed_count=processed_count,
                    detail=_runner_detail(result),
                )
            )
        return {
            "status": "ok",
            "processed_count": total_processed,
            "runners": [tick.__dict__ for tick in ticks],
        }

    def run_forever(
        self,
        max_loops: int | None = None,
        *,
        history_lease: HistoryWriterLease | HistoryWriterLeaseGroup | None = None,
    ) -> dict[str, Any]:
        lease = history_lease
        if lease is None and self.data_dir is not None:
            lease = register_history_writer_lease_if_owned(
                self.data_dir,
                label="agent_runner_loop",
            )
        try:
            return self._run_forever_leased(max_loops=max_loops)
        finally:
            if lease is not None:
                lease.release()

    def _run_forever_leased(self, max_loops: int | None = None) -> dict[str, Any]:
        loops = 0
        processed_count = 0
        last_ticks: list[dict[str, Any]] = []
        while max_loops is None or loops < max_loops:
            result = self.run_once()
            loops += 1
            processed_count += int(result.get("processed_count", 0) or 0)
            last_ticks = list(result.get("runners", []))
            if max_loops is None or loops < max_loops:
                time.sleep(self.poll_interval_seconds)
        return {
            "status": "stopped",
            "loops": loops,
            "processed_count": processed_count,
            "runners": last_ticks,
        }


def _processed_count(result: dict[str, Any]) -> int:
    value = result.get("processed_count")
    if isinstance(value, int):
        return value
    processed = result.get("processed", [])
    if isinstance(processed, list):
        return len(processed)
    return 0


def _runner_detail(result: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "event_file",
        "snapshot",
        "parse",
        "foreground",
        "capture",
        "max_running_seen",
    ]
    return {key: result[key] for key in keys if key in result}

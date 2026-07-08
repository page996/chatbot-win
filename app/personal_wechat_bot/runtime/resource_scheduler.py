from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ResourceSchedule:
    workload: str
    max_parallel_conversations: int
    llm_total: int
    llm_interactive: int
    llm_background: int
    media_cpu: int
    file_io: int
    gpu_media: int
    audit_updated_at: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResourceScheduler:
    """Central resource-budget helper for runtime dispatch decisions.

    This class does not execute work by itself. It turns the latest explicit
    local resource audit plus the live LLM key pool into conservative budgets
    that worker loops can honor. Keeping it separate lets sidebar state, WeFlow
    pulls, file parsing, and future channel lanes share one policy.
    """

    def __init__(
        self,
        data_dir: str | Path,
        *,
        key_pool: Any | None = None,
        provider_max_concurrency: int = 1,
    ):
        self.data_dir = Path(data_dir)
        self.key_pool = key_pool
        self.provider_max_concurrency = max(1, _int(provider_max_concurrency, 1))

    def conversation_parallelism(self, workload: str = "interactive") -> ResourceSchedule:
        workload = _workload(workload)
        audit = self._last_audit()
        recommendation = audit.get("recommendation") if isinstance(audit.get("recommendation"), dict) else {}
        llm_total = self._llm_limit()
        interactive_ratio = _ratio(recommendation.get("llm_interactive_ratio"), 0.7)
        interactive = max(1, min(llm_total, int(llm_total * interactive_ratio)))
        if llm_total > 1 and interactive >= llm_total:
            interactive = llm_total - 1
        background = max(1, llm_total - interactive)
        media_cpu = max(1, _int(recommendation.get("media_cpu"), 2))
        file_io = max(1, _int(recommendation.get("file_io_parallel"), 1))
        gpu_media = max(1, _int(recommendation.get("gpu_media"), 1))
        max_parallel = interactive if workload == "interactive" else background
        return ResourceSchedule(
            workload=workload,
            max_parallel_conversations=max(1, max_parallel),
            llm_total=llm_total,
            llm_interactive=interactive,
            llm_background=background,
            media_cpu=media_cpu,
            file_io=file_io,
            gpu_media=gpu_media,
            audit_updated_at=str(audit.get("updated_at") or ""),
            reason=str(recommendation.get("reason") or _default_reason(llm_total, interactive, background)),
        )

    def policy_snapshot(self) -> dict[str, Any]:
        interactive = self.conversation_parallelism("interactive")
        background = self.conversation_parallelism("background")
        return {
            "schema": "resource_scheduler_v1",
            "status": "ok",
            "interactive": interactive.to_dict(),
            "background": background.to_dict(),
            "policy": {
                "llm_split": "interactive uses the 70% foreground budget; context-only/background batches use the 30% background budget.",
                "gpu_media": "GPU-heavy OCR/ASR remains guarded by the GPU gate, defaulting to one slot.",
                "source": "runtime/resource_audit.json plus live ApiKeyPool concurrency.",
            },
        }

    def _llm_limit(self) -> int:
        concurrency_limit = getattr(self.key_pool, "concurrency_limit", None)
        if callable(concurrency_limit):
            try:
                return max(1, int(concurrency_limit()))
            except Exception:
                pass
        return self.provider_max_concurrency

    def _last_audit(self) -> dict[str, Any]:
        path = self.data_dir / "runtime" / "resource_audit.json"
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}


def _workload(value: str) -> str:
    text = str(value or "").strip().lower()
    return "background" if text in {"background", "context_only", "backfill", "history"} else "interactive"


def _ratio(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.1, min(0.9, number))


def _int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _default_reason(total: int, interactive: int, background: int) -> str:
    return f"LLM total={total}; interactive={interactive}; background={background}."

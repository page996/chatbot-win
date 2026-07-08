from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.runtime.resource_scheduler import ResourceScheduler


class ResourceSchedulerTest(unittest.TestCase):
    def test_scheduler_splits_llm_budget_and_uses_audit_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_dir = data_dir / "runtime"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "resource_audit.json").write_text(
                json.dumps(
                    {
                        "updated_at": "2026-07-08T00:00:00Z",
                        "recommendation": {
                            "media_cpu": 6,
                            "file_io_parallel": 4,
                            "gpu_media": 1,
                            "llm_interactive_ratio": 0.7,
                            "llm_background_ratio": 0.3,
                            "reason": "test audit",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            scheduler = ResourceScheduler(data_dir, key_pool=_KeyPool(10), provider_max_concurrency=2)

            interactive = scheduler.conversation_parallelism("interactive")
            background = scheduler.conversation_parallelism("background")

            self.assertEqual(interactive.llm_total, 10)
            self.assertEqual(interactive.llm_interactive, 7)
            self.assertEqual(interactive.llm_background, 3)
            self.assertEqual(interactive.max_parallel_conversations, 7)
            self.assertEqual(background.max_parallel_conversations, 3)
            self.assertEqual(background.media_cpu, 6)
            self.assertEqual(background.file_io, 4)
            self.assertEqual(background.reason, "test audit")

    def test_scheduler_falls_back_to_provider_limit_without_key_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scheduler = ResourceScheduler(Path(tmp) / "data", provider_max_concurrency=6)

            snapshot = scheduler.policy_snapshot()

            self.assertEqual(snapshot["schema"], "resource_scheduler_v1")
            self.assertEqual(snapshot["interactive"]["max_parallel_conversations"], 4)
            self.assertEqual(snapshot["background"]["max_parallel_conversations"], 2)


class _KeyPool:
    def __init__(self, limit: int):
        self.limit = limit

    def concurrency_limit(self) -> int:
        return self.limit


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.personal_wechat_bot.runtime.resource_gate import acquire_gpu, acquire_llm, gpu_gate_snapshot, llm_gate_snapshot


class RuntimeResourceGateTests(unittest.TestCase):
    def test_gpu_gate_serializes_threads_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = 0
            max_active = 0
            slots: list[int] = []
            lock = threading.Lock()

            def worker(index: int) -> None:
                nonlocal active, max_active
                with acquire_gpu(root=root, max_parallel=1, timeout_seconds=5, reason=f"test:{index}") as lease:
                    with lock:
                        active += 1
                        max_active = max(max_active, active)
                        slots.append(lease.slot)
                    time.sleep(0.05)
                    with lock:
                        active -= 1

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(max_active, 1)
            self.assertEqual(slots, [0, 0, 0, 0])

    def test_gpu_gate_snapshot_reports_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = gpu_gate_snapshot(root=tmp, max_parallel=2)
        self.assertEqual(snapshot["resource"], "gpu")
        self.assertEqual(snapshot["max_parallel"], 2)
        self.assertEqual(snapshot["active_slots"], 0)
        self.assertEqual(len(snapshot["slots"]), 2)
        self.assertIn("显式选择 GPU", str(snapshot["policy"]))

    def test_gpu_gate_snapshot_reports_cross_process_slot_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with acquire_gpu(root=tmp, max_parallel=1, timeout_seconds=5, reason="snapshot-test"):
                snapshot = gpu_gate_snapshot(root=tmp, max_parallel=1)
        self.assertEqual(snapshot["active_slots"], 1)
        self.assertTrue(snapshot["slots"][0]["locked"])

    def test_llm_gate_splits_interactive_and_background_slots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with acquire_llm(root=tmp, workload="interactive", max_parallel=2, timeout_seconds=5, reason="interactive"):
                with acquire_llm(root=tmp, workload="background", max_parallel=1, timeout_seconds=5, reason="background"):
                    snapshot = llm_gate_snapshot(root=tmp, interactive_max=2, background_max=1)

        self.assertEqual(snapshot["interactive"]["active_slots"], 1)
        self.assertEqual(snapshot["background"]["active_slots"], 1)
        self.assertEqual(snapshot["interactive"]["max_parallel"], 2)
        self.assertEqual(snapshot["background"]["max_parallel"], 1)
        self.assertIn("foreground", str(snapshot["policy"]))

    def test_llm_gate_total_budget_serializes_interactive_and_background(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            active = 0
            max_active = 0
            lock = threading.Lock()

            def worker(workload: str) -> None:
                nonlocal active, max_active
                with acquire_llm(
                    root=tmp,
                    workload=workload,
                    max_parallel=2,
                    total_max_parallel=1,
                    timeout_seconds=5,
                    reason=workload,
                ):
                    with lock:
                        active += 1
                        max_active = max(max_active, active)
                    time.sleep(0.05)
                    with lock:
                        active -= 1

            threads = [
                threading.Thread(target=worker, args=("interactive",)),
                threading.Thread(target=worker, args=("background",)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            snapshot = llm_gate_snapshot(root=tmp, total_max=1, interactive_max=2, background_max=2)

        self.assertEqual(max_active, 1)
        self.assertIn("total", snapshot)
        self.assertEqual(snapshot["total"]["max_parallel"], 1)


if __name__ == "__main__":
    unittest.main()

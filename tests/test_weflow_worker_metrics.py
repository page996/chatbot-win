from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.runtime.weflow_state_summary import summarize_weflow_bridge_state
from app.personal_wechat_bot.runtime.weflow_worker_metrics import WeflowWorkerMetrics


def _tick_result(*, status="ok", source_status="ok", scanned=0, appended=0, imported=0, processed=0, errors=None):
    return {
        "status": status,
        "source": {
            "status": source_status,
            "session_count": 1,
            "scanned_count": scanned,
            "appended_count": appended,
            "errors": errors or [],
        },
        "pull": {
            "processed_count": processed,
            "import": {"appended_count": imported, "error_count": 0},
        },
    }


class WeflowWorkerMetricsTest(unittest.TestCase):
    def test_records_totals_and_progress(self) -> None:
        metrics = WeflowWorkerMetrics(slow_tick_seconds=10.0)
        metrics.record_tick(_tick_result(scanned=5, appended=3, imported=3, processed=2), 1.0, now=100.0)
        metrics.record_tick(_tick_result(scanned=4, appended=0, imported=0, processed=0), 0.5, now=101.0)

        self.assertEqual(metrics.loops, 2)
        self.assertEqual(metrics.totals["scanned"], 9)
        self.assertEqual(metrics.totals["appended"], 3)
        self.assertEqual(metrics.totals["processed"], 2)
        self.assertEqual(metrics.last_progress_at, 100.0)
        self.assertEqual(metrics.last_success_at, 101.0)

    def test_snapshot_flags_stall_when_no_success(self) -> None:
        metrics = WeflowWorkerMetrics()
        metrics.started_at = 100.0
        metrics.record_tick(_tick_result(status="partial_error", source_status="error"), 0.2, now=100.0)

        snapshot = metrics.snapshot(running=True, now=300.0, stall_threshold_seconds=120.0)

        self.assertTrue(snapshot["stalled"])
        self.assertEqual(snapshot["loops"], 1)
        self.assertEqual(snapshot["last_status"], "partial_error")

    def test_snapshot_not_stalled_after_recent_success(self) -> None:
        metrics = WeflowWorkerMetrics()
        metrics.record_tick(_tick_result(appended=1, processed=1), 0.2, now=290.0)

        snapshot = metrics.snapshot(running=True, now=300.0, stall_threshold_seconds=120.0)

        self.assertFalse(snapshot["stalled"])
        self.assertEqual(snapshot["seconds_since_success"], 10.0)

    def test_slow_tick_and_error_counters(self) -> None:
        metrics = WeflowWorkerMetrics(slow_tick_seconds=5.0)
        metrics.record_tick(_tick_result(processed=1), 30.0, now=100.0)
        metrics.record_error("ConnectionRefusedError: boom", 0.1, now=101.0)

        self.assertEqual(metrics.slow_ticks, 1)
        self.assertEqual(metrics.error_ticks, 1)
        self.assertGreaterEqual(metrics.max_tick_duration_seconds, 30.0)
        self.assertEqual(metrics.last_error, "ConnectionRefusedError: boom")

    def test_recent_ring_buffer_is_bounded(self) -> None:
        metrics = WeflowWorkerMetrics(max_recent_ticks=3)
        for index in range(6):
            metrics.record_tick(_tick_result(processed=1), 0.1, now=100.0 + index)

        snapshot = metrics.snapshot(running=True, now=200.0, recent_limit=10)

        self.assertEqual(len(snapshot["recent_ticks"]), 3)
        self.assertEqual([item["tick_index"] for item in snapshot["recent_ticks"]], [4, 5, 6])


class WeflowStateSummaryTest(unittest.TestCase):
    def test_absent_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = summarize_weflow_bridge_state(Path(tmp) / "missing.json")
            self.assertEqual(summary["status"], "absent")
            self.assertEqual(summary["session_count"], 0)

    def test_summarizes_sessions_and_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weflow_bridge_state.json"
            path.write_text(
                json.dumps(
                    {
                        "sessions": {
                            "wxid_alice": {"since": 1720000000},
                            "room1@chatroom": {"since": 1720000500},
                        },
                        "seen_raw_ids": ["a", "b", "c"],
                        "weflow_sse_seen": ["x"],
                        "weflow_sse_last_event_id": "42",
                    }
                ),
                encoding="utf-8",
            )

            summary = summarize_weflow_bridge_state(path)

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["session_count"], 2)
            self.assertEqual(summary["group_session_count"], 1)
            self.assertEqual(summary["private_session_count"], 1)
            self.assertEqual(summary["seen_raw_id_count"], 3)
            self.assertEqual(summary["sse_seen_count"], 1)
            self.assertEqual(summary["sse_last_event_id"], "42")
            talkers = {item["talker"]: item for item in summary["sessions"]}
            self.assertEqual(talkers["wxid_alice"]["since"], 1720000000)
            self.assertTrue(talkers["room1@chatroom"]["is_group"])

    def test_unreadable_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weflow_bridge_state.json"
            path.write_text("{not json", encoding="utf-8")
            summary = summarize_weflow_bridge_state(path)
            self.assertEqual(summary["status"], "unreadable")


if __name__ == "__main__":
    unittest.main()

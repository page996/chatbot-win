from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from app.personal_wechat_bot.control.sidebar_api import sidebar_task_action
from app.personal_wechat_bot.conversation.channel_state_store import ChannelStateStore
from app.personal_wechat_bot.tasks.manager import TaskStatusStore


class TaskStatusStoreTest(unittest.TestCase):
    def test_create_update_transition_and_state_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStatusStore(Path(tmp) / "data")

            created = store.create(
                {
                    "task_id": "task-1",
                    "title": "Read file",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "topic_id": "topic-file",
                    "topic_title": "File handling",
                    "resource_class": "gpu",
                    "estimated_cost": 5,
                    "priority": 80,
                    "stop_and_wait": True,
                }
            )
            running = store.transition("task-1", "start", {"progress": 40, "phase": "reading"})
            completed = store.transition("task-1", "complete", {"progress": 100})
            state = store.state()

            self.assertEqual(created["concurrency_key"], "conversation:conv-a")
            self.assertTrue(created["stop_and_wait"])
            self.assertEqual(running["status"], "running")
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(state["counts"]["completed"], 1)
            self.assertTrue(state["scheduler"]["supports_multi_conversation"])
            self.assertIn("gpu", state["scheduler"]["resource_pools"])
            self.assertEqual(state["channels"][0]["conversation_id"], "conv-a")
            self.assertEqual(state["channels"][0]["resource_audit"]["estimated_cost"], 5)

    def test_sidebar_task_action_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"

            created = sidebar_task_action(
                data_dir,
                {"action": "create", "task": {"task_id": "local-1", "title": "UI task", "kind": "sidebar"}},
            )
            updated = sidebar_task_action(
                data_dir,
                {"action": "update", "task_id": "local-1", "patch": {"status": "waiting", "blocker": "user_input"}},
            )

            self.assertEqual(created["status"], "ok")
            self.assertEqual(updated["task"]["status"], "waiting")
            self.assertEqual(updated["task_manager"]["counts"]["waiting"], 1)

    def test_sqlite_is_authority_and_json_projection_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = TaskStatusStore(data_dir)

            store.create({"task_id": "sqlite-task", "title": "SQLite task"})

            sqlite_path = data_dir / "scheduler.sqlite"
            projection_path = data_dir / "task_manager" / "tasks.json"
            projection = json.loads(projection_path.read_text(encoding="utf-8"))

            self.assertTrue(sqlite_path.exists())
            self.assertTrue(projection_path.exists())
            self.assertEqual(projection["authority"], "scheduler.sqlite")
            self.assertEqual(Path(projection["sqlite"]), sqlite_path)
            self.assertEqual(projection["tasks"][0]["task_id"], "sqlite-task")

    def test_legacy_json_projection_migrates_into_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            task_dir = data_dir / "task_manager"
            task_dir.mkdir(parents=True)
            (task_dir / "tasks.json").write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "task_id": "legacy-task",
                                "title": "Legacy task",
                                "status": "queued",
                                "updated_at": "2026-07-08T00:00:00Z",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            state = TaskStatusStore(data_dir).state()
            reloaded = TaskStatusStore(data_dir).state()

            self.assertTrue((data_dir / "scheduler.sqlite").exists())
            self.assertEqual(state["tasks"][0]["task_id"], "legacy-task")
            self.assertEqual(reloaded["tasks"][0]["task_id"], "legacy-task")

    def test_ephemeral_ui_diagnostics_are_hidden_from_persistent_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStatusStore(Path(tmp) / "data")

            store.create(
                {
                    "task_id": "probe-1",
                    "title": "GPU probe",
                    "kind": "environment",
                    "status": "completed",
                    "scope": "diagnostic:runtime-gpu",
                    "metadata": {"local_ui": True},
                }
            )
            store.create(
                {
                    "task_id": "real-1",
                    "title": "Parse file",
                    "kind": "file",
                    "status": "queued",
                    "scope": "conversation:conv-a",
                }
            )

            state = store.state()

            self.assertEqual(state["counts"]["total"], 1)
            self.assertEqual(state["tasks"][0]["task_id"], "real-1")

    def test_weflow_tasks_do_not_pollute_channel_lanes_and_finish_by_external_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStatusStore(Path(tmp) / "data")

            store.create(
                {
                    "task_id": "weflow-session",
                    "title": "WeFlow 拉取：wxid_user",
                    "status": "running",
                    "conversation_id": "conv-a",
                    "scope": "weflow:pull:wxid_user",
                    "external_id": "pull-1",
                }
            )
            store.create(
                {
                    "task_id": "conversation-task",
                    "title": "处理用户主题",
                    "status": "running",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "external_id": "topic-1",
                }
            )

            before = store.state()
            self.assertEqual([lane["conversation_id"] for lane in before["channels"]], ["conv-a"])
            self.assertEqual(before["channels"][0]["active"][0]["task_id"], "conversation-task")

            updated = store.finish_external("pull-1", {"status": "completed", "progress": 100, "phase": "完成"})
            after = store.state()

            self.assertEqual(updated[0]["status"], "completed")
            weflow = next(item for item in after["tasks"] if item["task_id"] == "weflow-session")
            self.assertEqual(weflow["status"], "completed")

    def test_stale_weflow_child_task_is_repaired_when_parent_finished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            task_dir = data_dir / "task_manager"
            task_dir.mkdir(parents=True)
            (task_dir / "tasks.json").write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "task_id": "weflow-parent",
                                "title": "WeFlow 拉取任务",
                                "status": "completed",
                                "progress": 100,
                                "external_id": "pull-1",
                                "concurrency_key": "weflow:pull:pull-1",
                                "updated_at": "2026-07-07T01:00:01Z",
                                "finished_at": "2026-07-07T01:00:01Z",
                            },
                            {
                                "task_id": "weflow-child",
                                "title": "WeFlow 拉取：wxid_user",
                                "status": "running",
                                "progress": 58,
                                "external_id": "pull-1",
                                "concurrency_key": "weflow:pull:wxid_user",
                                "updated_at": "2026-07-07T01:00:00Z",
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            state = TaskStatusStore(data_dir).state()
            child = next(item for item in state["tasks"] if item["task_id"] == "weflow-child")

            self.assertEqual(child["status"], "completed")
            self.assertEqual(child["progress"], 100)

    def test_global_backend_tasks_do_not_create_channel_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStatusStore(Path(tmp) / "data")

            store.create(
                {
                    "task_id": "global-maintenance",
                    "title": "全局维护",
                    "status": "running",
                    "scope": "global",
                }
            )

            state = store.state()

            self.assertEqual(state["counts"]["active"], 1)
            self.assertEqual(state["channels"], [])

    def test_claim_next_respects_resource_channel_dependencies_and_scope_mutex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStatusStore(Path(tmp) / "data")
            store.create({"task_id": "done", "title": "Done", "status": "completed"})
            store.create(
                {
                    "task_id": "a",
                    "title": "Reply A",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "resource_class": "llm_interactive",
                    "priority": 90,
                    "dependencies": ["done"],
                }
            )
            store.create(
                {
                    "task_id": "b",
                    "title": "Reply B",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "resource_class": "llm_interactive",
                    "priority": 80,
                }
            )
            store.create(
                {
                    "task_id": "c",
                    "title": "Reply C",
                    "conversation_id": "conv-b",
                    "scope": "conversation:conv-b",
                    "resource_class": "llm_interactive",
                    "priority": 70,
                    "dependencies": ["missing"],
                }
            )

            first = store.claim_next(
                worker_id="worker-1",
                resource_limits={"llm_interactive": 1},
                channel_limit=1,
                limit=3,
            )
            second = store.claim_next(
                worker_id="worker-2",
                resource_limits={"llm_interactive": 1},
                channel_limit=1,
                limit=3,
            )
            preview = store.dispatch_preview(resource_limits={"llm_interactive": 1}, channel_limit=1)
            events = store.events(task_id="a")

            self.assertEqual([item["task_id"] for item in first], ["a"])
            self.assertEqual(second, [])
            self.assertEqual(store.state()["tasks"][0]["assigned_worker"], "worker-1")
            self.assertEqual(preview["blocked_count"], 2)
            self.assertTrue(any(item["event"] == "claimed" for item in events))

    def test_dispatch_preview_simulates_claimed_slots_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStatusStore(Path(tmp) / "data")
            store.create(
                {
                    "task_id": "first",
                    "title": "First",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "resource_class": "llm_interactive",
                    "priority": 90,
                }
            )
            store.create(
                {
                    "task_id": "same-channel",
                    "title": "Same channel",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a:next",
                    "resource_class": "llm_interactive",
                    "priority": 80,
                }
            )
            store.create(
                {
                    "task_id": "same-scope",
                    "title": "Same scope",
                    "conversation_id": "conv-b",
                    "scope": "conversation:conv-a",
                    "resource_class": "cpu_io",
                    "priority": 70,
                }
            )

            preview = store.dispatch_preview(
                resource_limits={"llm_interactive": 2, "cpu_io": 2},
                channel_limit=1,
            )
            blocked = {item["task_id"]: item["reason"] for item in preview["blocked"]}

            self.assertEqual([item["task_id"] for item in preview["runnable"]], ["first"])
            self.assertEqual(blocked["same-channel"], "channel_busy:conv-a")
            self.assertEqual(blocked["same-scope"], "concurrency_key_busy:conversation:conv-a")

    def test_claim_next_respects_channel_controls_and_priority_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = TaskStatusStore(data_dir)
            controls = ChannelStateStore(data_dir)
            controls.patch_control("conv-paused", {"mode": "paused", "priority": 100, "wait_reason": "人工暂停"})
            controls.patch_control(
                "conv-snoozed",
                {"mode": "snoozed", "priority": 100, "snoozed_until": "2999-01-01T00:00:00Z"},
            )
            controls.patch_control("conv-priority", {"mode": "active", "priority": 100})
            controls.patch_control("conv-pinned", {"mode": "active", "priority": 50, "pinned": True})
            controls.patch_control(
                "conv-expired-snooze",
                {"mode": "snoozed", "priority": 90, "snoozed_until": "2000-01-01T00:00:00Z"},
            )
            store.create(
                {
                    "task_id": "paused",
                    "title": "Paused",
                    "conversation_id": "conv-paused",
                    "scope": "conversation:conv-paused",
                    "resource_class": "cpu_io",
                    "priority": 100,
                }
            )
            store.create(
                {
                    "task_id": "snoozed",
                    "title": "Snoozed",
                    "conversation_id": "conv-snoozed",
                    "scope": "conversation:conv-snoozed",
                    "resource_class": "cpu_io",
                    "priority": 100,
                }
            )
            store.create(
                {
                    "task_id": "priority",
                    "title": "Priority overlay",
                    "conversation_id": "conv-priority",
                    "scope": "conversation:conv-priority",
                    "resource_class": "cpu_io",
                    "priority": 40,
                }
            )
            store.create(
                {
                    "task_id": "pinned",
                    "title": "Pinned overlay",
                    "conversation_id": "conv-pinned",
                    "scope": "conversation:conv-pinned",
                    "resource_class": "cpu_io",
                    "priority": 10,
                }
            )
            store.create(
                {
                    "task_id": "normal",
                    "title": "Normal",
                    "conversation_id": "conv-normal",
                    "scope": "conversation:conv-normal",
                    "resource_class": "cpu_io",
                    "priority": 50,
                }
            )
            store.create(
                {
                    "task_id": "expired-snooze",
                    "title": "Expired snooze",
                    "conversation_id": "conv-expired-snooze",
                    "scope": "conversation:conv-expired-snooze",
                    "resource_class": "cpu_io",
                    "priority": 20,
                }
            )

            preview_before_claim = store.dispatch_preview(resource_limits={"cpu_io": 5}, channel_limit=1)
            claimed = store.claim_next(
                worker_id="worker-control",
                resource_limits={"cpu_io": 5},
                channel_limit=1,
                limit=5,
            )
            preview = store.dispatch_preview(resource_limits={"cpu_io": 5}, channel_limit=1)
            blocked = {item["task_id"]: item["reason"] for item in preview["blocked"]}

            self.assertEqual(preview_before_claim["runnable"][0]["task_id"], "pinned")
            self.assertTrue(preview_before_claim["runnable"][0]["channel_pinned"])
            self.assertEqual([item["task_id"] for item in claimed], ["pinned", "priority", "expired-snooze", "normal"])
            self.assertEqual(blocked["paused"], "channel_paused:conv-paused")
            self.assertTrue(blocked["snoozed"].startswith("channel_snoozed:conv-snoozed:"))
            self.assertEqual(preview["blocked_count"], 2)

    def test_sidebar_task_action_claim_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            sidebar_task_action(
                data_dir,
                {
                    "action": "create",
                    "task": {
                        "task_id": "claim-me",
                        "title": "Claim me",
                        "conversation_id": "conv-a",
                        "scope": "conversation:conv-a",
                    },
                },
            )

            claimed = sidebar_task_action(
                data_dir,
                {
                    "action": "claim",
                    "worker_id": "api-worker",
                    "resource_limits": {"cpu_io": 1},
                },
            )
            events = sidebar_task_action(data_dir, {"action": "events", "task_id": "claim-me"})

            self.assertEqual(claimed["status"], "ok")
            self.assertEqual(claimed["claimed"][0]["task_id"], "claim-me")
            self.assertEqual(claimed["claimed"][0]["status"], "running")
            self.assertTrue(any(item["event"] == "claimed" for item in events["events"]))


if __name__ == "__main__":
    unittest.main()

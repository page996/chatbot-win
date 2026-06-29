from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.agent.monitor import TaskMonitor
from app.personal_wechat_bot.agent.tool_orchestrator import ToolTaskOrchestrator
from app.personal_wechat_bot.agent.worker_queue import LocalWorkerQueue
from app.personal_wechat_bot.agent.workspace import PlanBookStore, TaskWorkspaceStore
from app.personal_wechat_bot.domain.models import ToolCallRequest


class AgentWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        self.plan_store = PlanBookStore(self.data_dir)
        self.task_store = TaskWorkspaceStore(self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_planbook_records_revisions(self) -> None:
        plan_id = self.plan_store.create_plan("private:wxid_xiaoming", "thread:default", "Translate a paper.")
        revision = self.plan_store.append_revision(plan_id, "dispatch", "Dispatch document task.")

        plan = self.plan_store.read_plan(plan_id)
        self.assertEqual(revision, 2)
        self.assertEqual(plan["current_revision"], 2)
        self.assertEqual([item["kind"] for item in plan["revisions"]], ["initial_plan", "dispatch"])

    def test_short_process_worker_queue_caps_parallelism_and_records_all_statuses(self) -> None:
        plan_id = self.plan_store.create_plan("group:Study Group", "topic:paper", "Run fake tasks.")
        revision = self.plan_store.append_revision(plan_id, "dispatch", "Dispatch three fake tasks.")
        task_ids = [
            self.task_store.create_task(
                plan_id=plan_id,
                plan_revision=revision,
                agent_type="fake_agent",
                conversation_id="group:Study Group",
                thread_id="topic:paper",
                user_goal=f"Fake task {index}",
                instructions={"sleep_seconds": 0.15},
            )
            for index in range(3)
        ]
        queue = LocalWorkerQueue(self.task_store, max_parallel=2)
        for task_id in task_ids:
            queue.enqueue(task_id)

        result = queue.run_until_idle(timeout_seconds=10)

        self.assertEqual(result.started, 3)
        self.assertEqual(result.completed, 3)
        self.assertEqual(result.failed, 0)
        self.assertLessEqual(result.max_running_seen, 2)
        self.assertEqual(result.max_running_seen, 2)
        for task_id in task_ids:
            statuses = [event["status"] for event in self.task_store.read_events(task_id) if event["type"] == "status"]
            self.assertEqual(statuses, ["created", "queued", "assigned", "running", "completed"])
            self.assertEqual(self.task_store.read_report(task_id)["status"], "completed")
            snapshot = TaskMonitor(self.task_store).snapshot(task_id)
            self.assertTrue(snapshot.is_terminal)
            self.assertEqual(snapshot.status, "completed")

    def test_failed_worker_records_failed_status_and_report(self) -> None:
        plan_id = self.plan_store.create_plan("private:wxid_xiaoming", "thread:default", "Run failing task.")
        revision = self.plan_store.append_revision(plan_id, "dispatch", "Dispatch failing fake task.")
        task_id = self.task_store.create_task(
            plan_id=plan_id,
            plan_revision=revision,
            agent_type="fake_agent",
            conversation_id="private:wxid_xiaoming",
            thread_id="thread:default",
            user_goal="Fail on purpose.",
            instructions={"fail": True},
        )
        queue = LocalWorkerQueue(self.task_store, max_parallel=2)
        queue.enqueue(task_id)

        result = queue.run_until_idle(timeout_seconds=10)

        self.assertEqual(result.started, 1)
        self.assertEqual(result.completed, 0)
        self.assertEqual(result.failed, 1)
        statuses = [event["status"] for event in self.task_store.read_events(task_id) if event["type"] == "status"]
        self.assertEqual(statuses, ["created", "queued", "assigned", "running", "failed"])
        self.assertEqual(self.task_store.read_report(task_id)["status"], "failed")

    def test_tool_orchestrator_writes_repair_revision_before_retry(self) -> None:
        orchestrator = ToolTaskOrchestrator(
            self.data_dir,
            timeout_seconds=5,
            max_retries=1,
            worker_module="app.personal_wechat_bot.agent.missing_worker",
        )
        request = ToolCallRequest(
            tool_name="document.translate",
            call_id="call_missing_worker",
            conversation_id="private:wxid_xiaoming",
            requested_by="chatbot",
            arguments={"input_text": "hello"},
        )

        result = orchestrator.execute(request)

        self.assertEqual(result.status, "failed")
        plan_dirs = list((self.data_dir / "agent_workspace" / "plans").iterdir())
        self.assertEqual(len(plan_dirs), 1)
        plan = self.plan_store.read_plan(plan_dirs[0].name)
        kinds = [revision["kind"] for revision in plan["revisions"]]
        self.assertEqual(kinds, ["initial_plan", "dispatch", "repair_prepare", "repair", "failed"])
        task_dirs = list((self.data_dir / "agent_workspace" / "tasks").iterdir())
        self.assertEqual(len(task_dirs), 2)


if __name__ == "__main__":
    unittest.main()

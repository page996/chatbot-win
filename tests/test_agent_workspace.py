from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.agent import fake_worker, worker_queue
from app.personal_wechat_bot.agent.monitor import TaskMonitor
from app.personal_wechat_bot.agent.tool_orchestrator import ToolTaskOrchestrator
from app.personal_wechat_bot.agent.worker_queue import LocalWorkerQueue
from app.personal_wechat_bot.agent.workspace import PlanBookStore, TaskWorkspaceStore
from app.personal_wechat_bot.config.loader import create_default_config
from app.personal_wechat_bot.domain.models import ToolCallRequest


class AgentWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        create_default_config(self.data_dir)
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

    def test_worker_startup_timeout_terminates_and_reaps_child(self) -> None:
        plan_id = self.plan_store.create_plan("private:test", "thread:test", "Run task.")
        revision = self.plan_store.append_revision(plan_id, "dispatch", "Dispatch task.")
        task_id = self.task_store.create_task(
            plan_id=plan_id,
            plan_revision=revision,
            agent_type="fake_agent",
            conversation_id="private:test",
            thread_id="thread:test",
            user_goal="Timeout during startup.",
            instructions={},
        )
        queue = LocalWorkerQueue(self.task_store)
        queue.enqueue(task_id)
        process = mock.Mock(pid=4321)
        handoff = mock.Mock()
        handoff.child_environment.return_value = {}

        with (
            mock.patch.object(
                worker_queue,
                "register_history_writer_startup_handoff_if_owned",
                return_value=handoff,
            ),
            mock.patch.object(worker_queue.subprocess, "Popen", return_value=process),
            mock.patch.object(worker_queue, "process_start_marker", return_value="start:4321"),
            mock.patch.object(
                worker_queue,
                "_wait_for_worker_startup",
                side_effect=TimeoutError("not ready"),
            ),
            mock.patch.object(worker_queue, "_terminate_worker_process") as terminate,
        ):
            result = queue.run_until_idle(timeout_seconds=1.0)

        self.assertEqual(result.started, 1)
        self.assertEqual(result.failed, 1)
        terminate.assert_called_once_with(process)
        handoff.cancel.assert_called_once_with()
        handoff.release.assert_not_called()
        self.assertEqual(self.task_store.read_status(task_id)["status"], "failed")

    def test_worker_startup_interrupt_terminates_and_reaps_child(self) -> None:
        plan_id = self.plan_store.create_plan("private:test", "thread:test", "Run task.")
        revision = self.plan_store.append_revision(plan_id, "dispatch", "Dispatch task.")
        task_id = self.task_store.create_task(
            plan_id=plan_id,
            plan_revision=revision,
            agent_type="fake_agent",
            conversation_id="private:test",
            thread_id="thread:test",
            user_goal="Interrupt during startup.",
            instructions={},
        )
        queue = LocalWorkerQueue(self.task_store)
        queue.enqueue(task_id)
        process = mock.Mock(pid=4321)
        handoff = mock.Mock()
        handoff.child_environment.return_value = {}

        with (
            mock.patch.object(
                worker_queue,
                "register_history_writer_startup_handoff_if_owned",
                return_value=handoff,
            ),
            mock.patch.object(worker_queue.subprocess, "Popen", return_value=process),
            mock.patch.object(worker_queue, "process_start_marker", return_value="start:4321"),
            mock.patch.object(
                worker_queue,
                "_wait_for_worker_startup",
                side_effect=KeyboardInterrupt(),
            ),
            mock.patch.object(worker_queue, "_terminate_worker_process") as terminate,
        ):
            with self.assertRaises(KeyboardInterrupt):
                queue.run_until_idle(timeout_seconds=1.0)

        terminate.assert_called_once_with(process)
        handoff.cancel.assert_called_once_with()
        handoff.release.assert_not_called()

    def test_worker_handle_termination_escalates_and_reaps(self) -> None:
        process = mock.Mock(pid=4321)
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="worker", timeout=0.1),
            0,
        ]
        worker_queue._terminate_worker_process(process, grace_seconds=0.1)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_missing_worker_process_marker_fails_before_ready_acceptance(self) -> None:
        plan_id = self.plan_store.create_plan("private:test", "thread:test", "Run task.")
        revision = self.plan_store.append_revision(plan_id, "dispatch", "Dispatch task.")
        task_id = self.task_store.create_task(
            plan_id=plan_id,
            plan_revision=revision,
            agent_type="fake_agent",
            conversation_id="private:test",
            thread_id="thread:test",
            user_goal="Reject unverifiable child.",
            instructions={},
        )
        queue = LocalWorkerQueue(self.task_store)
        queue.enqueue(task_id)
        process = mock.Mock(pid=4321)
        handoff = mock.Mock()
        handoff.child_environment.return_value = {}

        with (
            mock.patch.object(
                worker_queue,
                "register_history_writer_startup_handoff_if_owned",
                return_value=handoff,
            ),
            mock.patch.object(worker_queue.subprocess, "Popen", return_value=process),
            mock.patch.object(worker_queue, "process_start_marker", return_value=""),
            mock.patch.object(worker_queue, "_wait_for_worker_startup") as wait_ready,
            mock.patch.object(worker_queue, "_terminate_worker_process") as terminate,
        ):
            result = queue.run_until_idle(timeout_seconds=1.0)

        self.assertEqual(result.started, 1)
        self.assertEqual(result.failed, 1)
        wait_ready.assert_not_called()
        terminate.assert_called_once_with(process)
        handoff.cancel.assert_called_once_with()
        handoff.release.assert_not_called()

    def test_worker_job_failure_prevents_handoff_and_worker_body(self) -> None:
        with mock.patch.object(
            fake_worker,
            "parse_args",
            return_value=mock.Mock(data_dir=str(self.data_dir), task_id="task"),
        ), mock.patch.object(
            fake_worker,
            "ensure_worker_descendant_job",
            side_effect=RuntimeError("job unavailable"),
        ), mock.patch.object(
            fake_worker,
            "history_writer_lease_after_startup_handoff_if_owned",
        ) as lease, mock.patch.object(fake_worker, "_run_worker") as run_worker:
            with self.assertRaisesRegex(RuntimeError, "job unavailable"):
                fake_worker.main()

        lease.assert_not_called()
        run_worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()

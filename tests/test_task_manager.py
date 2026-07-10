from __future__ import annotations

import tempfile
import unittest
import json
import time
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.config.loader import create_default_config, save_config
from app.personal_wechat_bot.control.sidebar_api import build_sidebar_task_manager, sidebar_task_action
from app.personal_wechat_bot.conversation.channel_state_store import ChannelStateStore
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.domain.models import ReplyCandidate
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue
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

    def test_dry_run_send_task_phase_is_repaired_on_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStatusStore(Path(tmp) / "data")
            store.create(
                {
                    "task_id": "send-1",
                    "title": "发送回复",
                    "kind": "send",
                    "status": "completed",
                    "conversation_id": "conv-a",
                    "phase": "非前台桥发送完成",
                    "detail": "dry_run_not_delivered:text",
                }
            )

            task = TaskStatusStore(Path(tmp) / "data").state()["tasks"][0]

            self.assertEqual(task["phase"], "非前台桥演练完成，未投递微信")

    def test_terminal_lane_task_is_not_current_topic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStatusStore(Path(tmp) / "data")
            store.create(
                {
                    "task_id": "send-old",
                    "title": "Send old reply",
                    "kind": "send",
                    "status": "cancelled",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "topic_id": "reply-old",
                    "topic_title": "Old reply",
                    "priority": 90,
                }
            )

            lane = store.state()["channels"][0]

            self.assertEqual(lane["current_topic"]["status"], "idle")
            self.assertEqual(lane["current_topic"]["topic_id"], "")
            self.assertEqual(lane["history"][0]["task_id"], "send-old")

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

    def test_json_projection_does_not_repopulate_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            task_dir = data_dir / "task_manager"
            task_dir.mkdir(parents=True)
            (task_dir / "tasks.json").write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "task_id": "projection-task",
                                "title": "Projection task",
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
            TaskStatusStore(data_dir).create({"task_id": "current-task", "title": "Current task"})
            reloaded = TaskStatusStore(data_dir).state()

            self.assertTrue((data_dir / "scheduler.sqlite").exists())
            self.assertEqual(state["tasks"], [])
            self.assertEqual([item["task_id"] for item in reloaded["tasks"]], ["current-task"])

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

    def test_ephemeral_ui_tasks_are_purged_from_sqlite_on_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = TaskStatusStore(data_dir)
            store.scheduler_store.replace_tasks(
                [
                    {
                        "task_id": "probe-leftover",
                        "title": "Old UI probe",
                        "kind": "environment",
                        "status": "running",
                        "scope": "diagnostic:runtime-gpu",
                        "metadata": {"local_ui": True},
                    },
                    {
                        "task_id": "real-task",
                        "title": "Real queued work",
                        "kind": "file",
                        "status": "queued",
                        "scope": "conversation:conv-a",
                    },
                ]
            )

            state = store.state()
            persisted = TaskStatusStore(data_dir).scheduler_store.list_tasks()
            projection = json.loads((data_dir / "task_manager" / "tasks.json").read_text(encoding="utf-8"))

            self.assertEqual([item["task_id"] for item in state["tasks"]], ["real-task"])
            self.assertEqual([item["task_id"] for item in persisted], ["real-task"])
            self.assertEqual([item["task_id"] for item in projection["tasks"]], ["real-task"])

    def test_local_ui_conversation_probe_tasks_are_hidden_from_persistent_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStatusStore(Path(tmp) / "data")

            store.create(
                {
                    "task_id": "channel-probe",
                    "title": "Channel file probe",
                    "kind": "发送测试",
                    "status": "running",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "metadata": {"local_ui": True, "scope_label": "通道文件探针"},
                }
            )
            store.create(
                {
                    "task_id": "send-real",
                    "title": "Send reply",
                    "kind": "send",
                    "status": "queued",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                }
            )

            state = store.state()

            self.assertEqual([item["task_id"] for item in state["tasks"]], ["send-real"])
            self.assertEqual(state["counts"]["active"], 1)
            self.assertEqual(state["channels"][0]["active"][0]["task_id"], "send-real")

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
            store = TaskStatusStore(data_dir)
            store.create(
                {
                    "task_id": "weflow-parent",
                    "title": "WeFlow 拉取任务",
                    "status": "completed",
                    "progress": 100,
                    "external_id": "pull-1",
                    "concurrency_key": "weflow:pull:pull-1",
                    "updated_at": "2026-07-07T01:00:01Z",
                    "finished_at": "2026-07-07T01:00:01Z",
                }
            )
            store.create(
                {
                    "task_id": "weflow-child",
                    "title": "WeFlow 拉取：wxid_user",
                    "status": "running",
                    "progress": 58,
                    "external_id": "pull-1",
                    "concurrency_key": "weflow:pull:wxid_user",
                    "updated_at": "2026-07-07T01:00:00Z",
                }
            )

            state = store.state()
            child = next(item for item in state["tasks"] if item["task_id"] == "weflow-child")

            self.assertEqual(child["status"], "completed")
            self.assertEqual(child["progress"], 100)

    def test_bridge_send_task_is_repaired_from_terminal_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = TaskStatusStore(data_dir)
            bridge_id = "bridge:conv-a:abc123"
            store.create(
                {
                    "task_id": "send-bridge",
                    "title": "Send bridge",
                    "kind": "send",
                    "status": "queued",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "resource_class": "send_bridge",
                    "metadata": {"send_reason": f"queued_to_non_foreground_bridge:{bridge_id}"},
                }
            )
            ack_path = data_dir / "send_bridge" / "acks.jsonl"
            ack_path.parent.mkdir(parents=True, exist_ok=True)
            ack_path.write_text(
                json.dumps(
                    {
                        "bridge_id": bridge_id,
                        "status": "failed",
                        "reason": "wechat_native_http_send_text_error:missing",
                        "created_at": "2026-07-08T01:00:00Z",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            task = TaskStatusStore(data_dir).state()["tasks"][0]

            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["external_id"], bridge_id)
            self.assertEqual(task["progress"], 100)
            self.assertIn("wechat_native_http_send_text_error", task["last_error"])

    def test_bridge_send_task_accepted_ack_is_completed_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            bridge_id = "bridge:conv-a:accepted123"
            TaskStatusStore(data_dir).create(
                {
                    "task_id": "send-bridge-accepted",
                    "title": "Send bridge",
                    "kind": "send",
                    "status": "queued",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "resource_class": "send_bridge",
                    "metadata": {"send_reason": f"queued_to_non_foreground_bridge:{bridge_id}"},
                }
            )
            ack_path = data_dir / "send_bridge" / "acks.jsonl"
            ack_path.parent.mkdir(parents=True, exist_ok=True)
            ack_path.write_text(
                json.dumps(
                    {
                        "bridge_id": bridge_id,
                        "status": "accepted",
                        "reason": "wechat_native_http_send_file_accepted_unverified",
                        "created_at": "2026-07-08T01:00:00Z",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            task = TaskStatusStore(data_dir).state()["tasks"][0]

            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["external_id"], bridge_id)
            self.assertEqual(task["progress"], 100)
            self.assertEqual(task["last_error"], "")
            self.assertIn("accepted", task["phase"])
            self.assertEqual(task["metadata"]["aggregate_bridge_status"], "accepted")

    def test_multipart_bridge_send_task_waits_for_all_terminal_acks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            text_bridge_id = "bridge:conv-a:text123"
            file_bridge_id = "bridge:conv-a:file123"
            TaskStatusStore(data_dir).create(
                {
                    "task_id": "send-bridge-multipart",
                    "title": "Send bridge multipart",
                    "kind": "send",
                    "status": "queued",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "resource_class": "send_bridge",
                    "external_id": text_bridge_id,
                    "metadata": {
                        "bridge_id": text_bridge_id,
                        "bridge_ids": [text_bridge_id, file_bridge_id],
                        "send_reason": f"queued_to_non_foreground_bridge:{text_bridge_id};{file_bridge_id}",
                    },
                }
            )
            ack_path = data_dir / "send_bridge" / "acks.jsonl"
            ack_path.parent.mkdir(parents=True, exist_ok=True)
            ack_path.write_text(
                json.dumps(
                    {
                        "bridge_id": text_bridge_id,
                        "status": "sent",
                        "reason": "wechat_native_http_send_text_verified",
                        "created_at": "2026-07-08T01:00:00Z",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            waiting = TaskStatusStore(data_dir).state()["tasks"][0]

            self.assertEqual(waiting["status"], "queued")
            self.assertEqual(waiting["external_id"], text_bridge_id)

            with ack_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "bridge_id": file_bridge_id,
                            "status": "accepted",
                            "reason": "wechat_native_http_send_file_accepted_unverified",
                            "created_at": "2026-07-08T01:00:02Z",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

            completed = TaskStatusStore(data_dir).state()["tasks"][0]

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["progress"], 100)
            self.assertIn("accepted", completed["phase"])
            self.assertEqual(completed["metadata"]["aggregate_bridge_status"], "accepted")
            self.assertEqual(set(completed["metadata"]["bridge_acks"].keys()), {text_bridge_id, file_bridge_id})

    def test_obsolete_weflow_send_blocker_is_repaired_after_backend_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = create_default_config(data_dir)
            config.send_enabled = True
            config.send_driver = "bridge_outbox"
            config.send_backend = "wechat_native_http"
            save_config(config)
            store = TaskStatusStore(data_dir)
            store.create(
                {
                    "task_id": "send-old-weflow",
                    "title": "Send reply",
                    "kind": "send",
                    "status": "blocked",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "resource_class": "send_bridge",
                    "detail": "weflow_backend_unavailable:weflow_text_send_not_supported:native-not-implemented",
                    "last_error": "weflow_backend_unavailable:weflow_text_send_not_supported:native-not-implemented",
                }
            )

            task = TaskStatusStore(data_dir).state()["tasks"][0]

            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["progress"], 100)
            self.assertIn("obsolete_send_backend_blocker", task["last_error"])

    def test_obsolete_stale_worker_send_blocker_is_repaired_when_worker_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = create_default_config(data_dir)
            config.send_enabled = True
            config.send_driver = "bridge_outbox"
            config.send_backend = "wechat_native_http"
            save_config(config)
            store = TaskStatusStore(data_dir)
            store.create(
                {
                    "task_id": "send-old-stale-worker",
                    "title": "Send reply",
                    "kind": "send",
                    "status": "blocked",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "resource_class": "send_bridge",
                    "detail": "bridge_worker_stale_config:worker_backend=wechat_native_http:expected_backend=wechat_native_http",
                    "last_error": "bridge_worker_stale_config:worker_backend=wechat_native_http:expected_backend=wechat_native_http",
                }
            )

            with mock.patch(
                "app.personal_wechat_bot.tasks.manager._bridge_worker_config_is_matched",
                return_value=True,
            ):
                task = TaskStatusStore(data_dir).state()["tasks"][0]

            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["progress"], 100)
            self.assertIn("obsolete_bridge_worker_stale_config", task["last_error"])

    def test_obsolete_stale_worker_task_with_approved_queue_is_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = create_default_config(data_dir)
            config.send_enabled = True
            config.send_driver = "bridge_outbox"
            config.send_backend = "wechat_native_http"
            save_config(config)
            reply = ReplyCandidate(
                message_id="message-approved-after-stale",
                conversation_id="conv-a",
                text="hello after stale worker",
                send_mode="confirm",
                model="test",
            )
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")
            store = TaskStatusStore(data_dir)
            store.create(
                {
                    "task_id": "send-message-approved-after-stale",
                    "title": "Send reply",
                    "kind": "send",
                    "status": "failed",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "resource_class": "send_bridge",
                    "detail": "obsolete_bridge_worker_stale_config:current_backend=wechat_native_http",
                    "last_error": "obsolete_bridge_worker_stale_config:current_backend=wechat_native_http",
                    "metadata": {"message_id": reply.message_id},
                }
            )

            with mock.patch(
                "app.personal_wechat_bot.tasks.manager._bridge_worker_config_is_matched",
                return_value=True,
            ):
                task = TaskStatusStore(data_dir).state()["tasks"][0]

            self.assertEqual(task["status"], "queued")
            self.assertEqual(task["progress"], 55)
            self.assertEqual(task["last_error"], "")
            self.assertEqual(task["finished_at"], "")
            self.assertIn("obsolete_bridge_worker_stale_config", task["detail"])

    def test_auto_mode_retires_old_sidebar_confirm_test_send_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = create_default_config(data_dir)
            config.mode = "auto"
            config.send_confirm_required = False
            config.send_enabled = True
            config.send_driver = "bridge_outbox"
            config.send_backend = "wechat_native_http"
            save_config(config)
            store = TaskStatusStore(data_dir)
            store.create(
                {
                    "task_id": "send-sidebar_channel_test_replyold",
                    "title": "Send reply",
                    "kind": "send",
                    "status": "queued",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "resource_class": "send_bridge",
                    "detail": "confirm_approved",
                    "metadata": {"message_id": "sidebar_channel_test_reply:old"},
                }
            )

            task = TaskStatusStore(data_dir).state()["tasks"][0]

            self.assertEqual(task["status"], "cancelled")
            self.assertEqual(task["progress"], 100)
            self.assertIn("obsolete_sidebar_confirm_test_task", task["last_error"])

    def test_auto_mode_retires_sidebar_test_reopened_after_stale_worker_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = create_default_config(data_dir)
            config.mode = "auto"
            config.send_confirm_required = False
            config.send_enabled = True
            config.send_driver = "bridge_outbox"
            config.send_backend = "wechat_native_http"
            save_config(config)
            store = TaskStatusStore(data_dir)
            store.create(
                {
                    "task_id": "send-sidebar_channel_test_replyold",
                    "title": "Send reply",
                    "kind": "send",
                    "status": "queued",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "resource_class": "send_bridge",
                    "detail": "obsolete_bridge_worker_stale_config:current_backend=wechat_native_http",
                    "phase": "发送阻断已解除，等待重新投递",
                    "metadata": {"message_id": "sidebar_channel_test_reply:old"},
                }
            )

            task = TaskStatusStore(data_dir).state()["tasks"][0]

            self.assertEqual(task["status"], "cancelled")
            self.assertEqual(task["progress"], 100)
            self.assertIn("obsolete_sidebar_confirm_test_task", task["last_error"])

    def test_reply_task_is_repaired_when_assistant_ledger_entry_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = TaskStatusStore(data_dir)
            store.create(
                {
                    "task_id": "reply-msg-1",
                    "title": "Reply",
                    "kind": "reply",
                    "status": "running",
                    "conversation_id": "conv-a",
                    "scope": "conversation:conv-a",
                    "external_id": "msg-1",
                }
            )
            ConversationLedgerStore(data_dir).append_reply(
                ReplyCandidate(
                    message_id="msg-1",
                    conversation_id="conv-a",
                    text="hello",
                    send_mode="confirm",
                    model="test",
                ),
                chat_title="Alice",
            )

            task = TaskStatusStore(data_dir).state()["tasks"][0]

            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["progress"], 100)
            self.assertEqual(task["actual_cost"], 1)

    def test_reply_task_repair_reads_sqlite_when_ledger_projection_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_reply(
                ReplyCandidate(
                    message_id="msg-db-only",
                    conversation_id="conv-db-only",
                    text="database-backed reply",
                    send_mode="confirm",
                    model="test",
                ),
                chat_title="DB Only",
            )
            projection_dir = ledger.conversation_markdown_path("conv-db-only").parent
            (projection_dir / "messages.jsonl").unlink()
            (projection_dir / "conversation.md").unlink()
            store = TaskStatusStore(data_dir)
            store.create(
                {
                    "task_id": "reply-msg-db-only",
                    "title": "Reply",
                    "kind": "reply",
                    "status": "running",
                    "conversation_id": "conv-db-only",
                    "scope": "conversation:conv-db-only",
                    "external_id": "msg-db-only",
                }
            )

            task = TaskStatusStore(data_dir).state()["tasks"][0]

            self.assertEqual(task["status"], "completed")
            self.assertTrue((projection_dir / "messages.jsonl").exists())
            self.assertTrue((projection_dir / "conversation.md").exists())

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

    def test_sidebar_task_manager_finishes_stale_agent_worker_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            store = TaskStatusStore(data_dir)
            store.create(
                {
                    "task_id": "agent-worker",
                    "title": "连续对话 Agent",
                    "kind": "Agent",
                    "status": "running",
                    "external_id": "agent-worker",
                    "metadata": {"worker": True},
                }
            )

            state = build_sidebar_task_manager(data_dir)
            task = next(item for item in state["tasks"] if item["task_id"] == "agent-worker")

            self.assertEqual(task["status"], "completed")
            self.assertEqual(state["counts"]["active"], 0)

    def test_running_worker_task_with_stale_heartbeat_is_terminalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            stale_at = time.time() - 600
            TaskStatusStore(data_dir).create(
                {
                    "task_id": "weflow-worker",
                    "title": "WeFlow worker",
                    "kind": "WeFlow",
                    "status": "running",
                    "external_id": "worker",
                    "scope": "weflow:pull:worker",
                    "metadata": {
                        "worker": True,
                        "worker_kind": "weflow",
                        "worker_heartbeat_at": stale_at,
                        "worker_stale_after_seconds": 30,
                    },
                }
            )

            task = TaskStatusStore(data_dir).state()["tasks"][0]

            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["progress"], 100)
            self.assertIn("worker_heartbeat_stale", task["last_error"])
            self.assertTrue(task["metadata"]["worker_reconciled"])

    def test_fresh_worker_task_is_not_terminalized_on_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            TaskStatusStore(data_dir).create(
                {
                    "task_id": "fresh-worker",
                    "title": "Fresh worker",
                    "kind": "Agent",
                    "status": "running",
                    "external_id": "agent-worker",
                    "scope": "agent:worker",
                    "metadata": {
                        "worker": True,
                        "worker_kind": "agent",
                        "worker_heartbeat_at": time.time(),
                        "worker_stale_after_seconds": 300,
                    },
                }
            )

            task = TaskStatusStore(data_dir).state()["tasks"][0]

            self.assertEqual(task["status"], "running")
            self.assertEqual(task["progress"], 0)

    def test_sidebar_task_manager_finishes_stale_weflow_worker_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            TaskStatusStore(data_dir).create(
                {
                    "task_id": "weflow-worker",
                    "title": "WeFlow 拉取任务",
                    "kind": "WeFlow",
                    "status": "running",
                    "external_id": "worker",
                    "scope": "weflow:pull:worker",
                    "metadata": {"worker": True, "worker_kind": "weflow"},
                }
            )

            state = build_sidebar_task_manager(data_dir)
            task = next(item for item in state["tasks"] if item["task_id"] == "weflow-worker")

            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["detail"], "worker_not_running")

    def test_sidebar_task_manager_finishes_stale_send_bridge_worker_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            TaskStatusStore(data_dir).create(
                {
                    "task_id": "send-bridge-worker",
                    "title": "发送桥 worker",
                    "kind": "send_bridge_worker",
                    "status": "running",
                    "external_id": "send-bridge-worker",
                    "scope": "send_bridge:worker",
                    "resource_class": "send_bridge",
                    "metadata": {"worker": True, "worker_kind": "send_bridge"},
                }
            )

            state = build_sidebar_task_manager(data_dir)
            task = next(item for item in state["tasks"] if item["task_id"] == "send-bridge-worker")

            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["detail"], "worker_not_running")

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

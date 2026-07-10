from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.conversation.channel_state_store import (
    ChannelStateRecord,
    ChannelStateStore,
    build_channel_state_projection,
    merge_channel_state_projection,
)


class ChannelStateStoreTest(unittest.TestCase):
    def test_projection_collects_topics_files_reply_and_resources(self) -> None:
        record = build_channel_state_projection(
            channel={
                "conversation_id": "conv-a",
                "conversation_type": "group",
                "chat_title": "Project Group",
                "status": "active",
                "updated_at": "2026-07-08T01:00:00Z",
            },
            tasks=[
                {
                    "task_id": "task-1",
                    "title": "分析文件",
                    "status": "running",
                    "priority": 80,
                    "progress": 45,
                    "conversation_id": "conv-a",
                    "concurrency_key": "conversation:conv-a",
                    "topic_id": "topic-file",
                    "topic_title": "文件分析",
                    "resource_class": "llm_interactive",
                    "estimated_cost": 5,
                    "actual_cost": 2,
                    "updated_at": "2026-07-08T01:02:00Z",
                },
                {
                    "task_id": "task-2",
                    "title": "旧任务",
                    "status": "completed",
                    "priority": 20,
                    "conversation_id": "conv-a",
                    "topic_id": "topic-old",
                    "resource_class": "cpu_io",
                    "estimated_cost": 1,
                    "actual_cost": 1,
                    "updated_at": "2026-07-08T00:30:00Z",
                },
            ],
            ledger_entries=[
                {
                    "entry_id": "entry-user",
                    "message_id": "msg-user",
                    "role": "user",
                    "received_at": "2026-07-08T01:01:00Z",
                    "attachments": [
                        {
                            "file_id": "file-1",
                            "name": "report.pdf",
                            "kind": "file",
                            "status": "indexed",
                            "parse": {
                                "status": "parsed",
                                "kind": "pdf",
                                "ai_analysis_status": "analyzed",
                                "ai_summary": "这是一份项目报告。",
                                "ai_key_points": ["预算变化", "时间线风险"],
                                "chunk_count": 3,
                            },
                        }
                    ],
                },
                {
                    "entry_id": "entry-agent",
                    "message_id": "msg-user",
                    "role": "assistant",
                    "received_at": "2026-07-08T01:03:00Z",
                    "send": {"status": "queued_to_bridge"},
                },
            ],
        )
        payload = record.to_dict()

        self.assertEqual(payload["conversation_id"], "conv-a")
        self.assertEqual(payload["current_topic"]["title"], "文件分析")
        self.assertEqual(payload["active_tasks"][0]["task_id"], "task-1")
        self.assertEqual(payload["task_history"][0]["task_id"], "task-2")
        self.assertEqual(payload["reply_state"]["status"], "queued_to_bridge")
        self.assertEqual(payload["file_states"][0]["file_id"], "file-1")
        self.assertEqual(payload["file_states"][0]["summary"], "这是一份项目报告。")
        self.assertEqual(payload["file_states"][0]["key_points"], ["预算变化", "时间线风险"])
        self.assertEqual(payload["resource_audit"]["estimated_cost"], 5)
        self.assertEqual(payload["resource_audit"]["actual_cost"], 2)
        self.assertEqual(payload["resource_audit"]["resources"]["llm_interactive"]["active"], 1)

    def test_sqlite_replace_list_get_and_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ChannelStateStore(Path(tmp) / "data")
            first = ChannelStateRecord(conversation_id="conv-a", chat_title="A", status="active")
            second = {"conversation_id": "conv-b", "chat_title": "B", "status": "paused"}

            replaced = store.replace_all([first, second])
            listed = store.list_states()
            fetched = store.get("conv-b")
            updated = store.upsert({"conversation_id": "conv-a", "chat_title": "A2", "status": "active"})

            self.assertTrue(store.path.exists())
            self.assertEqual(len(replaced), 2)
            self.assertEqual({item["conversation_id"] for item in listed}, {"conv-a", "conv-b"})
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched["chat_title"], "B")
            self.assertEqual(updated["chat_title"], "A2")
            self.assertEqual(store.get("conv-a")["chat_title"], "A2")  # type: ignore[index]

    def test_control_patch_survives_projection_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ChannelStateStore(Path(tmp) / "data")
            store.upsert({"conversation_id": "conv-a", "chat_title": "A"})
            patched = store.patch_control(
                "conv-a",
                {
                    "mode": "paused",
                    "priority": 92,
                    "pinned": True,
                    "wait_reason": "等待人工确认",
                    "operator_note": "先别回复",
                },
                updated_by="tester",
            )
            projected = build_channel_state_projection(
                channel={"conversation_id": "conv-a", "chat_title": "A", "status": "active"},
                tasks=[{"task_id": "reply-1", "status": "running", "title": "回复", "priority": 80}],
                ledger_entries=[],
            )

            merged = merge_channel_state_projection(projected, patched)

            self.assertEqual(merged["control"]["mode"], "paused")
            self.assertEqual(merged["control"]["priority"], 92)
            self.assertTrue(merged["control"]["pinned"])
            self.assertEqual(merged["control"]["wait_reason"], "等待人工确认")
            self.assertEqual(merged["control"]["operator_note"], "先别回复")
            self.assertEqual(merged["control"]["updated_by"], "tester")
            self.assertEqual(merged["effective_status"], "paused")
            self.assertEqual(merged["current_topic"]["title"], "回复")

    def test_expired_snooze_does_not_override_effective_status(self) -> None:
        projected = build_channel_state_projection(
            channel={"conversation_id": "conv-a", "chat_title": "A", "status": "active"},
            tasks=[{"task_id": "reply-1", "status": "queued", "title": "回复", "priority": 80}],
            ledger_entries=[],
        )
        active_snooze = merge_channel_state_projection(
            projected,
            {
                "conversation_id": "conv-a",
                "control": {
                    "mode": "snoozed",
                    "snoozed_until": "2999-01-01T00:00:00Z",
                },
            },
        )
        expired_snooze = merge_channel_state_projection(
            projected,
            {
                "conversation_id": "conv-a",
                "control": {
                    "mode": "snoozed",
                    "snoozed_until": "2000-01-01T00:00:00Z",
                },
            },
        )

        self.assertEqual(active_snooze["effective_status"], "snoozed")
        self.assertEqual(expired_snooze["effective_status"], "queued")

    def test_terminal_task_is_not_projected_as_current_topic(self) -> None:
        record = build_channel_state_projection(
            channel={"conversation_id": "conv-a", "chat_title": "A", "status": "active"},
            tasks=[
                {
                    "task_id": "send-old",
                    "title": "Send old reply",
                    "status": "cancelled",
                    "priority": 90,
                    "topic_id": "reply-old",
                    "topic_title": "Old reply",
                    "updated_at": "2026-07-08T01:00:00Z",
                }
            ],
            ledger_entries=[],
        ).to_dict()

        self.assertEqual(record["current_topic"]["status"], "idle")
        self.assertEqual(record["current_topic"]["topic_id"], "")
        self.assertEqual(record["effective_status"], "idle")
        self.assertEqual(record["task_history"][0]["task_id"], "send-old")

    def test_terminal_reply_status_does_not_make_idle_channel_failed(self) -> None:
        record = build_channel_state_projection(
            channel={"conversation_id": "conv-a", "chat_title": "A", "status": "active"},
            tasks=[],
            ledger_entries=[
                {
                    "entry_id": "entry-agent",
                    "message_id": "reply-1",
                    "role": "assistant",
                    "received_at": "2026-07-08T01:03:00Z",
                    "send": {"status": "failed", "reason": "manual_sidebar_failed:old_failure"},
                }
            ],
        ).to_dict()

        self.assertEqual(record["reply_state"]["status"], "failed")
        self.assertEqual(record["reply_state"]["last_send_reason"], "manual_sidebar_failed:old_failure")
        self.assertTrue(record["reply_state"]["historical"])
        self.assertFalse(record["reply_state"]["problem"])
        self.assertEqual(record["current_topic"]["status"], "idle")
        self.assertEqual(record["effective_status"], "idle")


if __name__ == "__main__":
    unittest.main()

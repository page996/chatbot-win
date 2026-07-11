from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.conversation.channel_registry_store import ChannelRegistryStore
from app.personal_wechat_bot.conversation.segment import conversation_segment
from app.personal_wechat_bot.conversation.session_database import ConversationSessionDatabase
from app.personal_wechat_bot.conversation.session_store import (
    CLEAR_CONTEXT_PHRASES,
    DEFAULT_SESSION_ID,
    ConversationSessionStore,
    is_reset_command,
)
from app.personal_wechat_bot.domain.models import NormalizedMessage
from app.personal_wechat_bot.runtime.process_lock import scoped_process_lock_path


class ConversationSessionStoreTest(unittest.TestCase):
    def test_tampered_registry_segment_cannot_escape_session_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            conversation_id = "registry-escape"
            ChannelRegistryStore(data_dir).upsert(
                {
                    "conversation_id": conversation_id,
                    "conversation_type": "private",
                    "chat_title": "Unsafe",
                    "segment": "../../escaped",
                    "status": "active",
                }
            )

            with self.assertRaises(ValueError):
                ConversationSessionStore(data_dir).current_session_id_for_conversation(
                    conversation_id,
                    "Unsafe",
                )

            self.assertFalse((root / "escaped").exists())

    def test_tampered_segment_cache_cannot_escape_session_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            store = ConversationSessionStore(data_dir)
            store._segment_cache["conv1"] = "../../escaped"

            with self.assertRaises(ValueError):
                store.current_session_id("conv1")

            self.assertFalse((root / "escaped").exists())

    def test_conversation_lock_lives_outside_deletable_session_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = ConversationSessionStore(data_dir)

            with store._conversation_lock("conv1"):
                lock_path = scoped_process_lock_path(
                    data_dir,
                    "conversation-lifecycle",
                    "conv1",
                )
                self.assertTrue(lock_path.exists())

            self.assertFalse(lock_path.exists())
            self.assertTrue(Path(f"{lock_path}.guard").exists())

    def test_current_session_defaults_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationSessionStore(Path(tmp))

            first = store.current_session_id("conv1")
            second = ConversationSessionStore(Path(tmp)).current_session_id("conv1")
            state = json.loads((Path(tmp) / "conversation_sessions" / "conv1" / "state.json").read_text(encoding="utf-8"))

            self.assertEqual(first, DEFAULT_SESSION_ID)
            self.assertEqual(second, DEFAULT_SESSION_ID)
            self.assertEqual(store.state_for_conversation("conv1")["current_session_id"], DEFAULT_SESSION_ID)
            self.assertTrue((Path(tmp) / "conversation_sessions" / "conv1" / "state.json").exists())
            self.assertEqual(state["session_started_at"], state["created_at"])
            self.assertEqual(state["reset_count"], 0)

    def test_current_session_keeps_its_sqlite_segment_after_channel_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ConversationSessionDatabase(root).upsert_state(
                "conv1",
                "conv1",
                {"conversation_id": "conv1", "current_session_id": "session_old"},
            )
            segment = conversation_segment("conv1", "PAGE")
            ChannelRegistryStore(root).upsert(
                {"conversation_id": "conv1", "chat_title": "PAGE", "segment": segment}
            )

            session_id = ConversationSessionStore(root).current_session_id("conv1")

            self.assertEqual(session_id, "session_old")
            self.assertTrue((root / "conversation_sessions" / "conv1" / "state.json").exists())

    def test_current_session_for_message_uses_stable_channel_segment_after_title_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conversation_id = "conv-title"
            old_segment = conversation_segment(conversation_id, "PAGE")
            new_segment = conversation_segment(conversation_id, "PAGE renamed")
            ChannelRegistryStore(root).upsert(
                {
                    "conversation_id": conversation_id,
                    "chat_title": "PAGE renamed",
                    "segment": old_segment,
                }
            )
            message = NormalizedMessage(
                message_id="m-title",
                conversation_id=conversation_id,
                conversation_type="private",
                chat_title="PAGE renamed",
                sender_name="PAGE",
                sender_wechat_id="wxid_page",
                text="hello",
                is_self=False,
                received_at="2026-06-29T00:00:00+08:00",
            )

            session_id = ConversationSessionStore(root).current_session_id_for_message(message)

            self.assertEqual(session_id, DEFAULT_SESSION_ID)
            self.assertTrue((root / "conversation_sessions" / old_segment / "state.json").exists())
            self.assertFalse((root / "conversation_sessions" / new_segment).exists())

    def test_clear_context_command_switches_session_without_context_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationSessionStore(Path(tmp))

            new_session = store.maybe_reset_for_message(
                _message("m1", f"@bot {CLEAR_CONTEXT_PHRASES[0]}")
            )
            # Session dirs use the human-readable segment (chat_title_hashPrefix),
            # matching what channel_store cleanup targets. chat_title="PAGE".
            segment = conversation_segment("conv1", "PAGE")
            state_path = Path(tmp) / "conversation_sessions" / segment / "state.json"
            events_path = Path(tmp) / "conversation_sessions" / segment / "events.jsonl"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]

            self.assertIsNotNone(new_session)
            self.assertNotEqual(new_session, DEFAULT_SESSION_ID)
            self.assertEqual(store.current_session_id("conv1"), new_session)
            self.assertEqual(state["current_session_id"], new_session)
            self.assertEqual(state["previous_session_id"], DEFAULT_SESSION_ID)
            self.assertEqual(state["reset_count"], 1)
            self.assertTrue(state["session_started_at"])
            self.assertEqual(events[-1]["type"], "session.reset")
            self.assertEqual(events[-1]["session_id"], new_session)
            self.assertEqual(events[-1]["previous_session_id"], DEFAULT_SESSION_ID)
            self.assertFalse(hasattr(store, "record_message"))
            self.assertFalse(hasattr(store, "build_snapshot"))

    def test_sqlite_session_state_survives_missing_file_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ConversationSessionStore(root)
            new_session = store.maybe_reset_for_message(_message("m1", "@bot 清空上下文"))
            segment = conversation_segment("conv1", "PAGE")
            session_dir = root / "conversation_sessions" / segment
            (session_dir / "state.json").unlink()
            (session_dir / "events.jsonl").unlink()

            reopened = ConversationSessionStore(root)
            state = reopened.state_for_conversation("conv1")
            events = reopened.database.list_events("conv1")

            self.assertTrue((root / "conversation_sessions.sqlite").exists())
            self.assertEqual(state["current_session_id"], new_session)
            self.assertEqual(events[-1]["type"], "session.reset")
            self.assertEqual(events[-1]["session_id"], new_session)
            self.assertTrue((session_dir / "state.json").exists())
            self.assertTrue((session_dir / "events.jsonl").exists())

    def test_file_projection_does_not_repopulate_session_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = root / "conversation_sessions" / "conv1"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "state.json").write_text(
                json.dumps({"conversation_id": "conv1", "current_session_id": "session_projection"}),
                encoding="utf-8",
            )
            projection_event = {
                "type": "session.reset",
                "conversation_id": "conv1",
                "session_id": "session_projection",
                "previous_session_id": DEFAULT_SESSION_ID,
                "created_at": "2026-06-29T00:00:00+00:00",
            }
            (session_dir / "events.jsonl").write_text(json.dumps(projection_event) + "\n", encoding="utf-8")

            store = ConversationSessionStore(root)

            self.assertEqual(store.current_session_id("conv1"), DEFAULT_SESSION_ID)
            self.assertEqual(store.database.list_events("conv1"), [])
            self.assertTrue((root / "conversation_sessions.sqlite").exists())

    def test_reset_detector_accepts_chinese_and_english_variants(self) -> None:
        self.assertTrue(is_reset_command("@bot 清空当前对话上下文"))
        self.assertTrue(is_reset_command("please @agent reset context now"))
        self.assertTrue(is_reset_command("清空上下文", metadata={"mentioned_self": True}))
        self.assertTrue(is_reset_command("清空上下文", metadata={"mentions": [{"name": "bot"}]}))
        self.assertFalse(is_reset_command("清空当前对话上下文"))
        self.assertFalse(is_reset_command("please reset context now"))
        self.assertFalse(is_reset_command("清空上下文 @小王"))
        self.assertFalse(is_reset_command("清空上下文", metadata={"mentions": [{"name": "wxid_other"}]}))
        self.assertFalse(is_reset_command("继续分析这个文件"))


def _message(message_id: str, text: str) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=message_id,
        conversation_id="conv1",
        conversation_type="private",
        chat_title="PAGE",
        sender_name="PAGE",
        sender_wechat_id="wxid_page",
        text=text,
        is_self=False,
        received_at="2026-06-29T00:00:00+08:00",
    )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.personal_wechat_bot.conversation.channel_store import ConversationChannelStore
from app.personal_wechat_bot.config.schema import ProviderConfig
from app.personal_wechat_bot.domain.models import NormalizedMessage
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool


class ConversationChannelStoreTest(unittest.TestCase):
    def test_private_channel_gets_one_sticky_key_and_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = ProviderConfig(api_key_env="", api_key_env_pool=["KEY_A", "KEY_B", "KEY_C"])
            store = ConversationChannelStore(
                root,
                ApiKeyPool(provider),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            )
            message = _message(conversation_id="private-1", conversation_type="private", chat_title="Alice")

            first = store.ensure_channel(message)
            second = ConversationChannelStore(
                root,
                ApiKeyPool(provider),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            ).get_channel("private-1")

            self.assertEqual(first.key_slots, 1)
            self.assertEqual(len(first.api_key_refs), 1)
            self.assertIsNotNone(second)
            self.assertEqual(second.api_key_refs, first.api_key_refs)
            self.assertIn("private-1", first.context_dir)
            self.assertIn("private-1", first.file_workspace_dir)

    def test_group_channel_gets_two_key_slots_when_pool_allows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = ProviderConfig(api_key_env="", api_key_env_pool=["KEY_A", "KEY_B", "KEY_C"])
            store = ConversationChannelStore(
                root,
                ApiKeyPool(provider),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            )

            channel = store.ensure_channel(
                _message(conversation_id="group-1", conversation_type="group", chat_title="Study Group")
            )

            self.assertEqual(channel.key_slots, 2)
            self.assertEqual(len(channel.api_key_refs), 2)
            self.assertTrue((root / "conversation_channels" / "group-1" / "channel.json").exists())

    def test_channel_key_selection_rotates_across_assigned_available_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key_file = root / "keys.md"
            key_file.write_text("KEY_A=secret-a\nKEY_B=secret-b\n", encoding="utf-8")
            provider = ProviderConfig(api_key_env="", api_key_file="keys.md")
            store = ConversationChannelStore(
                root,
                ApiKeyPool(provider, root),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            )
            store.ensure_channel(_message(conversation_id="group-rotate", conversation_type="group"))

            first = store.api_key_for_request("group-rotate")
            second = store.api_key_for_request("group-rotate")
            third = store.api_key_for_request("group-rotate")

            self.assertIn(first, {"secret-a", "secret-b"})
            self.assertIn(second, {"secret-a", "secret-b"})
            self.assertNotEqual(first, second)
            self.assertEqual(third, first)

    def test_concurrent_channel_registration_keeps_index_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = ProviderConfig(api_key_env="", api_key_env_pool=["KEY_A", "KEY_B", "KEY_C"])
            store = ConversationChannelStore(
                root,
                ApiKeyPool(provider),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            )

            with ThreadPoolExecutor(max_workers=4) as executor:
                list(
                    executor.map(
                        store.ensure_channel,
                        [
                            _message(conversation_id=f"conv-{index}", conversation_type="private")
                            for index in range(12)
                        ],
                    )
                )

            channels = store.list_channels()

            self.assertEqual(len(channels), 12)
            self.assertTrue((root / "conversation_channels" / "index.json").exists())


def _message(
    conversation_id: str,
    conversation_type: str,
    chat_title: str = "PAGE",
) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=f"msg-{conversation_id}",
        conversation_id=conversation_id,
        conversation_type=conversation_type,  # type: ignore[arg-type]
        chat_title=chat_title,
        sender_name=chat_title,
        text="hello",
        is_self=False,
        received_at="2026-06-29T00:00:00+00:00",
        sender_wechat_id=chat_title,
    )


if __name__ == "__main__":
    unittest.main()

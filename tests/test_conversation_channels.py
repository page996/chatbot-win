from __future__ import annotations

import tempfile
import unittest
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.personal_wechat_bot.conversation.channel_store import ConversationChannelStore
from app.personal_wechat_bot.conversation.channel_registry_store import ChannelRegistryStore
from app.personal_wechat_bot.conversation.ledger_database import ConversationLedgerDatabase
from app.personal_wechat_bot.conversation.segment import conversation_segment, resolve_segment
from app.personal_wechat_bot.conversation.session_database import ConversationSessionDatabase
from app.personal_wechat_bot.config.schema import ProviderConfig
from app.personal_wechat_bot.domain.models import NormalizedMessage
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool
from app.personal_wechat_bot.runtime.process_lock import scoped_process_lock_path, short_process_lock


class ConversationChannelStoreTest(unittest.TestCase):
    def test_unknown_sender_name_does_not_degrade_channel_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            key_pool = ApiKeyPool(ProviderConfig(api_key_env="", api_key_env_pool=["KEY_A"]), data_dir)
            store = ConversationChannelStore(
                data_dir,
                key_pool,
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            first = _message("conv-a", chat_title="Alice", sender_name="Alice")
            second = _message("conv-a", chat_title="unknown", sender_name="unknown")

            store.ensure_channel(first)
            channel = store.ensure_channel(second)

            self.assertEqual(channel.chat_title, "Alice")
            self.assertNotIn("unknown", channel.sender_names)

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
            self.assertEqual(first.source_names, ["backend_events_jsonl"])
            self.assertTrue(first.trusted_channel_source)
            self.assertIsNotNone(second)
            self.assertEqual(second.api_key_refs, first.api_key_refs)
            # After the human-readable naming change, context_dir/file_workspace_dir
            # contain chat_title + hash prefix, not raw conversation_id.
            self.assertIn("Alice", first.context_dir)
            self.assertIn("Alice", first.file_workspace_dir)

    def test_sqlite_registry_remains_authoritative_without_file_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key_file = root / "keys.md"
            key_file.write_text("KEY_A=secret-a\n", encoding="utf-8")
            provider = ProviderConfig(api_key_env="", api_key_file="keys.md")
            store = ConversationChannelStore(
                root,
                ApiKeyPool(provider, root),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            )
            conversation_id = "private-sqlite-authority"
            channel = store.ensure_channel(
                _message(conversation_id=conversation_id, conversation_type="private", chat_title="Alice")
            )
            projection_dir = root / "conversation_channels" / channel.segment
            (projection_dir / "channel.json").unlink()
            (root / "conversation_channels" / "index.json").unlink()

            reopened_store = ConversationChannelStore(
                root,
                ApiKeyPool(provider, root),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            )
            reopened = reopened_store.get_channel(conversation_id)
            listed = reopened_store.list_channels()
            selected_key = reopened_store.api_key_for_request(conversation_id)

            self.assertTrue((root / "conversation_channels.sqlite").exists())
            self.assertIsNotNone(reopened)
            self.assertEqual(reopened.chat_title, "Alice")
            self.assertEqual([item.conversation_id for item in listed], [conversation_id])
            self.assertEqual(selected_key, "secret-a")
            self.assertEqual(resolve_segment(root, conversation_id), channel.segment)
            self.assertTrue((projection_dir / "channel.json").exists())
            self.assertTrue((root / "conversation_channels" / "index.json").exists())

    def test_file_projection_does_not_register_a_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conversation_id = "private-projection-only"
            segment = conversation_segment(conversation_id, "Projection Only")
            channel_path = root / "conversation_channels" / segment / "channel.json"
            channel_path.parent.mkdir(parents=True, exist_ok=True)
            channel_path.write_text(
                json.dumps(
                    {
                        "conversation_id": conversation_id,
                        "conversation_type": "private",
                        "chat_title": "Projection Only",
                        "segment": segment,
                        "status": "active",
                        "key_slots": 1,
                        "api_key_refs": [],
                        "sender_names": ["Projection Only"],
                        "sender_wechat_ids": ["wxid_projection_only"],
                        "source_names": ["weflow_discovery"],
                        "trusted_channel_source": True,
                        "is_friend": True,
                        "contact_authorization": "explicit_friend",
                    }
                ),
                encoding="utf-8",
            )

            store = ConversationChannelStore(
                root,
                ApiKeyPool(ProviderConfig(api_key_env="", api_key_env_pool=["KEY_A"])),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            )

            self.assertIsNone(store.get_channel(conversation_id))
            self.assertEqual(store.list_channels(), [])
            self.assertTrue((root / "conversation_channels.sqlite").exists())

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
            # Human-readable naming: directory name is now chat_title_hashPrefix.
            self.assertTrue((root / "conversation_channels" / "Study Group_group-1" / "channel.json").exists())

    def test_segment_resolution_uses_sqlite_registry_without_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conversation_id = "private-readable"
            segment = conversation_segment(conversation_id, "Alice")
            ChannelRegistryStore(root).upsert(
                {
                    "conversation_id": conversation_id,
                    "conversation_type": "private",
                    "chat_title": "Alice",
                    "segment": segment,
                }
            )

            self.assertEqual(resolve_segment(root, conversation_id), segment)

    def test_chat_title_change_preserves_existing_channel_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = ProviderConfig(api_key_env="", api_key_env_pool=["KEY_A", "KEY_B"])
            store = ConversationChannelStore(
                root,
                ApiKeyPool(provider),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            )
            conversation_id = "private-title-change"
            old_segment = conversation_segment(conversation_id, "Alice")
            new_segment = conversation_segment(conversation_id, "Alice Renamed")

            first = store.ensure_channel(
                _message(conversation_id=conversation_id, conversation_type="private", chat_title="Alice")
            )
            second = ConversationChannelStore(
                root,
                ApiKeyPool(provider),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            ).ensure_channel(
                _message(conversation_id=conversation_id, conversation_type="private", chat_title="Alice Renamed")
            )
            index = json.loads((root / "conversation_channels" / "index.json").read_text(encoding="utf-8"))

            self.assertEqual(first.segment, old_segment)
            self.assertEqual(second.segment, old_segment)
            self.assertEqual(second.chat_title, "Alice Renamed")
            self.assertEqual(resolve_segment(root, conversation_id), old_segment)
            self.assertTrue((root / "conversation_channels" / old_segment / "channel.json").exists())
            self.assertFalse((root / "conversation_channels" / new_segment / "channel.json").exists())
            self.assertEqual(len(list((root / "conversation_channels").glob("*/channel.json"))), 1)
            self.assertEqual(index["channels"][0]["segment"], old_segment)
            self.assertEqual(index["channels"][0]["chat_title"], "Alice Renamed")

    def test_wxid_title_does_not_overwrite_human_channel_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = ProviderConfig(api_key_env="", api_key_env_pool=["KEY_A", "KEY_B"])
            store = ConversationChannelStore(
                root,
                ApiKeyPool(provider),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            )
            conversation_id = "private-title-safe"
            segment = conversation_segment(conversation_id, "Alice")

            first = store.ensure_channel(
                _message(conversation_id=conversation_id, conversation_type="private", chat_title="Alice")
            )
            second = ConversationChannelStore(
                root,
                ApiKeyPool(provider),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            ).ensure_channel(
                _message(
                    conversation_id=conversation_id,
                    conversation_type="private",
                    chat_title="wxid_alice",
                )
            )
            index = json.loads((root / "conversation_channels" / "index.json").read_text(encoding="utf-8"))

            self.assertEqual(first.segment, segment)
            self.assertEqual(second.segment, segment)
            self.assertEqual(second.chat_title, "Alice")
            self.assertEqual(index["channels"][0]["chat_title"], "Alice")

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

    def test_delete_channel_removes_registry_and_index_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = ProviderConfig(api_key_env="", api_key_env_pool=["KEY_A"])
            store = ConversationChannelStore(
                root,
                ApiKeyPool(provider),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            )
            channel = store.ensure_channel(_message(conversation_id="private-delete", conversation_type="private"))
            ledger_file = root / "conversation_ledgers" / "private-delete" / "conversation.md"
            workspace_file = root / "file_workspace" / "private-delete" / "session_default" / "file.txt"
            session_file = root / "conversation_sessions" / "private-delete" / "state.json"
            ledger_file.parent.mkdir(parents=True, exist_ok=True)
            workspace_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            ledger_file.write_text("keep ledger", encoding="utf-8")
            workspace_file.write_text("keep file", encoding="utf-8")
            session_file.write_text("keep session", encoding="utf-8")

            cleanup = store.delete_channel_with_cleanup(channel.conversation_id)
            deleted_again = store.delete_channel(channel.conversation_id)
            index = (root / "conversation_channels" / "index.json").read_text(encoding="utf-8")

            self.assertTrue(cleanup["deleted"])
            self.assertEqual(cleanup["cleanup_policy"], "wechat_preserve")
            self.assertFalse(deleted_again)
            self.assertIsNone(store.get_channel(channel.conversation_id))
            self.assertNotIn("private-delete", index)
            self.assertTrue(ledger_file.exists())
            self.assertTrue(workspace_file.exists())
            self.assertTrue(session_file.exists())

    def test_delete_untrusted_non_wechat_channel_purges_associated_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = ProviderConfig(api_key_env="", api_key_env_pool=["KEY_A"])
            store = ConversationChannelStore(
                root,
                ApiKeyPool(provider),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            )
            conversation_id = "untrusted-noise"
            segment = conversation_segment(conversation_id, "NOISE")
            channel_path = root / "conversation_channels" / segment / "channel.json"
            ledger_file = root / "conversation_ledgers" / segment / "conversation.md"
            workspace_file = root / "file_workspace" / segment / "session_default" / "file.txt"
            session_file = root / "conversation_sessions" / segment / "state.json"
            channel_path.parent.mkdir(parents=True, exist_ok=True)
            ledger_file.parent.mkdir(parents=True, exist_ok=True)
            workspace_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            channel_payload = {
                "conversation_id": conversation_id,
                "conversation_type": "private",
                "chat_title": "NOISE",
                "segment": segment,
                "status": "active",
                "key_slots": 1,
                "api_key_refs": [],
                "session_scope": "per_conversation_current_session",
                "sender_names": ["NOISE"],
                "sender_wechat_ids": [],
                "source_names": [],
                "trusted_channel_source": False,
            }
            store.registry.upsert(channel_payload)
            channel_path.write_text(json.dumps(channel_payload), encoding="utf-8")
            ledger_file.write_text("purge ledger", encoding="utf-8")
            workspace_file.write_text("purge file", encoding="utf-8")
            session_file.write_text("purge session", encoding="utf-8")
            ledger_database = ConversationLedgerDatabase(root)
            ledger_database.set_segment(conversation_id, segment)
            session_database = ConversationSessionDatabase(root)
            session_database.upsert_state(
                conversation_id,
                segment,
                {"conversation_id": conversation_id, "current_session_id": "session_default"},
            )

            cleanup = store.delete_channel_with_cleanup(conversation_id)

            self.assertTrue(cleanup["deleted"])
            self.assertEqual(cleanup["cleanup_policy"], "non_wechat_purge")
            self.assertFalse(channel_path.parent.exists())
            self.assertFalse(ledger_file.parent.exists())
            self.assertFalse((root / "file_workspace" / segment).exists())
            self.assertFalse(session_file.parent.exists())
            self.assertNotIn(conversation_id, ledger_database.list_conversation_ids())
            self.assertIsNone(session_database.get_state(conversation_id))

    def test_delete_waits_for_external_conversation_lifecycle_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = ProviderConfig(api_key_env="", api_key_env_pool=["KEY_A"])
            store = ConversationChannelStore(
                root,
                ApiKeyPool(provider),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            )
            conversation_id = "delete-serialized"
            store.ensure_channel(_message(conversation_id=conversation_id))
            lock_path = scoped_process_lock_path(
                root,
                "conversation-lifecycle",
                conversation_id,
            )

            with ThreadPoolExecutor(max_workers=1) as executor:
                with short_process_lock(
                    lock_path,
                    timeout_seconds=1.0,
                    stale_after_seconds=60.0,
                ):
                    future = executor.submit(
                        store.delete_channel_with_cleanup,
                        conversation_id,
                    )
                    self.assertFalse(future.done())
                    self.assertIsNotNone(store.get_channel(conversation_id))
                self.assertTrue(future.result(timeout=5.0)["deleted"])
            self.assertIsNone(store.get_channel(conversation_id))

    def test_stale_reader_does_not_restore_channel_after_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = ProviderConfig(api_key_env="", api_key_env_pool=["KEY_A"])
            store = ConversationChannelStore(
                root,
                ApiKeyPool(provider),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            )
            conversation_id = "delete-stale-reader"
            channel = store.ensure_channel(_message(conversation_id=conversation_id))
            restore_started = threading.Event()
            allow_restore = threading.Event()
            original_restore = store._restore_readable_projection

            def delayed_restore(payload: dict[str, object]):
                restore_started.set()
                self.assertTrue(allow_restore.wait(5.0))
                return original_restore(payload)

            store._restore_readable_projection = delayed_restore  # type: ignore[method-assign]
            with ThreadPoolExecutor(max_workers=1) as executor:
                reader = executor.submit(store.get_channel, conversation_id)
                self.assertTrue(restore_started.wait(5.0))
                self.assertTrue(store.delete_channel_with_cleanup(conversation_id)["deleted"])
                allow_restore.set()
                self.assertIsNone(reader.result(timeout=5.0))

            self.assertIsNone(store.registry.get(conversation_id))
            self.assertFalse((store.root / channel.segment / "channel.json").exists())
            index = json.loads((store.root / "index.json").read_text(encoding="utf-8"))
            self.assertFalse(
                any(item.get("conversation_id") == conversation_id for item in index["channels"])
            )

    def test_store_lock_lives_outside_deletable_channel_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = ProviderConfig(api_key_env="", api_key_env_pool=["KEY_A"])
            store = ConversationChannelStore(
                root,
                ApiKeyPool(provider),
                file_workspace_root=root / "file_workspace",
                context_root=root / "conversation_ledgers",
            )

            with store._store_lock():
                lock_path = scoped_process_lock_path(
                    root,
                    "conversation-channel-store",
                    "global",
                )
                self.assertTrue(lock_path.exists())

            self.assertFalse(lock_path.exists())
            self.assertTrue(Path(f"{lock_path}.guard").exists())

    def test_delete_rejects_registry_segment_that_escapes_storage_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            provider = ProviderConfig(api_key_env="", api_key_env_pool=["KEY_A"])
            store = ConversationChannelStore(
                data_dir,
                ApiKeyPool(provider),
                file_workspace_root=data_dir / "file_workspace",
                context_root=data_dir / "conversation_ledgers",
            )
            conversation_id = "malicious-segment"
            store.registry.upsert(
                {
                    "conversation_id": conversation_id,
                    "conversation_type": "private",
                    "segment": "../outside",
                    "trusted_channel_source": False,
                    "source_names": [],
                }
            )

            with self.assertRaises(ValueError):
                store.delete_channel_with_cleanup(conversation_id)

            self.assertTrue(sentinel.exists())
            self.assertIsNotNone(store.registry.get(conversation_id))


def _message(
    conversation_id: str,
    conversation_type: str = "private",
    chat_title: str = "PAGE",
    *,
    sender_name: str | None = None,
) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=f"msg-{conversation_id}",
        conversation_id=conversation_id,
        conversation_type=conversation_type,  # type: ignore[arg-type]
        chat_title=chat_title,
        sender_name=sender_name if sender_name is not None else chat_title,
        text="hello",
        is_self=False,
        received_at="2026-06-29T00:00:00+00:00",
        sender_wechat_id=chat_title,
        metadata={"source": "backend_events_jsonl"},
    )


if __name__ == "__main__":
    unittest.main()

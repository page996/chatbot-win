from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.config.schema import ProviderConfig
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool, ConversationKeyAssigner


class ApiKeyPoolTest(unittest.TestCase):
    def test_refs_include_primary_env_and_pool_without_duplicates(self) -> None:
        provider = ProviderConfig(api_key_env="KEY_A", api_key_env_pool=["KEY_A", "KEY_B"])

        refs = ApiKeyPool(provider).refs()

        self.assertEqual([item.ref for item in refs], ["KEY_A", "KEY_B"])

    def test_key_file_reads_env_names_and_direct_secret_values_without_exposing_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            key_file = data_dir / "API key.md"
            key_file.write_text(
                "\n".join(
                    [
                        "# local keys",
                        "DEEPSEEK_KEY_01",
                        "DEEPSEEK_KEY_02=direct-secret-value",
                        "`DEEPSEEK_KEY_03`",
                        "plain-direct-secret",
                    ]
                ),
                encoding="utf-8",
            )
            provider = ProviderConfig(api_key_env="", api_key_file="API key.md")

            refs = ApiKeyPool(provider, data_dir).refs()
            pool_text = str(refs)

            self.assertEqual(refs[0].ref, "DEEPSEEK_KEY_01")
            self.assertEqual(refs[0].source, "file_env")
            self.assertTrue(refs[1].ref.startswith("DEEPSEEK_KEY_02:secret:"))
            self.assertEqual(refs[1].source, "file_secret")
            self.assertEqual(refs[2].ref, "DEEPSEEK_KEY_03")
            self.assertTrue(refs[3].ref.startswith("file:secret:"))
            self.assertNotIn("direct-secret-value", pool_text)
            self.assertNotIn("plain-direct-secret", pool_text)
            self.assertEqual(ApiKeyPool(provider, data_dir).key_for_ref(refs[1].ref), "direct-secret-value")

    def test_available_count_uses_environment_without_exposing_secret(self) -> None:
        provider = ProviderConfig(api_key_env="KEY_POOL_TEST")
        old_value = os.environ.get("KEY_POOL_TEST")
        os.environ["KEY_POOL_TEST"] = "secret-value"
        try:
            pool = ApiKeyPool(provider)

            self.assertEqual(pool.available_count(), 1)
            self.assertEqual(pool.default_key(), "secret-value")
            self.assertNotIn("secret-value", str(pool.refs()))
        finally:
            if old_value is None:
                os.environ.pop("KEY_POOL_TEST", None)
            else:
                os.environ["KEY_POOL_TEST"] = old_value

    def test_add_key_appends_named_secret_and_describe_masks_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            provider = ProviderConfig(api_key_env="", api_key_file="API key.md")
            (data_dir / "API key.md").write_text("DEEPSEEK_KEY_01 = sk-existing000\n", encoding="utf-8")
            pool = ApiKeyPool(provider, data_dir)

            ref = pool.add_key("sk-new-secret-9999")

            self.assertEqual(ref.source, "file_secret")
            file_text = (data_dir / "API key.md").read_text(encoding="utf-8")
            self.assertIn("DEEPSEEK_KEY_02 = sk-new-secret-9999", file_text)
            described = ApiKeyPool(provider, data_dir).describe()
            previews = {item["ref"]: item["preview"] for item in described}
            self.assertIn(ref.ref, previews)
            self.assertEqual(previews[ref.ref], "****9999")
            # raw secret is never present in the described payload
            self.assertNotIn("sk-new-secret-9999", str(described))

    def test_add_key_rejects_duplicate_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            provider = ProviderConfig(api_key_env="", api_key_file="API key.md")
            (data_dir / "API key.md").write_text("DEEPSEEK_KEY_01 = sk-dup-value\n", encoding="utf-8")
            pool = ApiKeyPool(provider, data_dir)

            with self.assertRaises(ValueError):
                pool.add_key("sk-dup-value")

    def test_add_key_creates_file_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            provider = ProviderConfig(api_key_env="", api_key_file="nested/API key.md")
            pool = ApiKeyPool(provider, data_dir)

            ref = pool.add_key("sk-first-key-1234")

            self.assertTrue((data_dir / "nested" / "API key.md").exists())
            self.assertEqual(pool.key_for_ref(ref.ref), "sk-first-key-1234")

    def test_remove_key_drops_only_matching_secret_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            provider = ProviderConfig(api_key_env="", api_key_file="API key.md")
            (data_dir / "API key.md").write_text(
                "DEEPSEEK_KEY_01 = sk-keep-1111\nDEEPSEEK_KEY_02 = sk-drop-2222\n",
                encoding="utf-8",
            )
            pool = ApiKeyPool(provider, data_dir)
            refs = {item.ref: item for item in pool.refs()}
            drop_ref = next(ref for ref, item in refs.items() if "KEY_02" in ref)

            self.assertTrue(pool.remove_key(drop_ref))

            file_text = (data_dir / "API key.md").read_text(encoding="utf-8")
            self.assertIn("sk-keep-1111", file_text)
            self.assertNotIn("sk-drop-2222", file_text)
            self.assertFalse(pool.remove_key("nonexistent:secret:00000000"))

    def test_next_key_name_continues_existing_prefix_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            provider = ProviderConfig(api_key_env="", api_key_file="API key.md")
            (data_dir / "API key.md").write_text(
                "DEEPSEEK_KEY_01 = sk-a\nDEEPSEEK_KEY_07 = sk-b\n", encoding="utf-8"
            )
            pool = ApiKeyPool(provider, data_dir)

            pool.add_key("sk-c-value")

            file_text = (data_dir / "API key.md").read_text(encoding="utf-8")
            self.assertIn("DEEPSEEK_KEY_08 = sk-c-value", file_text)

    def test_conversation_key_assigner_is_sticky(self) -> None:
        provider = ProviderConfig(api_key_env="", api_key_env_pool=["KEY_A", "KEY_B", "KEY_C"])
        assigner = ConversationKeyAssigner(ApiKeyPool(provider))

        first = assigner.assign("conversation-1", slots=2)
        second = assigner.assign("conversation-1", slots=2)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)


if __name__ == "__main__":
    unittest.main()

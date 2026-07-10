from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.config.loader import create_default_config, load_config, set_chat_provider
from app.personal_wechat_bot.llm.openai_client import RelayOpenAIClient
from app.personal_wechat_bot.llm.model_router import ModelRouter


class ModelRouterTest(unittest.TestCase):
    def test_default_config_has_chat_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = create_default_config(data_dir)

            self.assertIn("chat", config.providers)
            self.assertEqual(config.providers["chat"].model, "deepseek-v4-flash")
            self.assertEqual(config.providers["chat"].provider, "deepseek")

    def test_missing_provider_map_uses_current_chat_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config_path = data_dir / "config.json"
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw.pop("providers", None)
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

            config = load_config(data_dir)

            self.assertEqual(config.providers["chat"].model, "deepseek-v4-flash")

    def test_model_router_selects_chat_provider_by_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            config = create_default_config(data_dir)
            router = ModelRouter(config.providers)

            selection = router.chat_provider()

            self.assertEqual(selection.provider_id, "chat")
            self.assertEqual(selection.config.model, "deepseek-v4-flash")

    def test_set_deepseek_provider_uses_official_defaults(self) -> None:
        from app.personal_wechat_bot.config.loader import set_deepseek_provider

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_deepseek_provider(data_dir)

            config = load_config(data_dir)

            self.assertEqual(config.providers["chat"].provider, "deepseek")
            self.assertEqual(config.providers["chat"].model, "deepseek-v4-flash")
            self.assertEqual(config.providers["chat"].base_url, "https://api.deepseek.com")
            self.assertEqual(config.providers["chat"].api_key_env, "DEEPSEEK_API_KEY")

    def test_runtime_uses_relay_client_when_chat_provider_has_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_chat_provider(data_dir, "https://relay.example.com", max_wait_seconds=30)
            config = load_config(data_dir)

            runtime = build_runtime(config)

            self.assertIsInstance(runtime.conversation.llm, RelayOpenAIClient)
            self.assertEqual(runtime.model_router.chat_provider().config.base_url, "https://relay.example.com")


if __name__ == "__main__":
    unittest.main()

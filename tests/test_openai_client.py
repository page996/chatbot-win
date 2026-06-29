from __future__ import annotations

import unittest

from app.personal_wechat_bot.llm.openai_client import normalize_openai_base_url


class OpenAIClientCompatibilityTest(unittest.TestCase):
    def test_normalize_base_url_adds_v1_when_missing(self) -> None:
        self.assertEqual(
            normalize_openai_base_url("https://relay.example.com"),
            "https://relay.example.com/v1",
        )

    def test_normalize_base_url_keeps_existing_v1(self) -> None:
        self.assertEqual(
            normalize_openai_base_url("https://relay.example.com/v1/"),
            "https://relay.example.com/v1",
        )

    def test_normalize_base_url_keeps_deepseek_official_root(self) -> None:
        self.assertEqual(
            normalize_openai_base_url("https://api.deepseek.com", provider="deepseek"),
            "https://api.deepseek.com",
        )

    def test_normalize_base_url_strips_deepseek_v1_suffix(self) -> None:
        self.assertEqual(
            normalize_openai_base_url("https://api.deepseek.com/v1/", provider="deepseek"),
            "https://api.deepseek.com",
        )


if __name__ == "__main__":
    unittest.main()

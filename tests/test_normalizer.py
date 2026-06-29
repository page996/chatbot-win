from __future__ import annotations

import unittest

from app.personal_wechat_bot.domain.models import RawWeChatMessage
from app.personal_wechat_bot.normalizer.normalizer import MessageNormalizer


class MessageNormalizerTest(unittest.TestCase):
    def test_message_id_does_not_depend_on_observed_at(self) -> None:
        normalizer = MessageNormalizer()
        first = normalizer.normalize(
            RawWeChatMessage(
                raw_id="raw-1",
                chat_title="Alice",
                sender_name="Alice",
                sender_wechat_id="wxid_alice",
                text="hello",
                observed_at="2026-06-28T01:00:00+00:00",
            )
        )
        second = normalizer.normalize(
            RawWeChatMessage(
                raw_id="raw-1",
                chat_title="Alice",
                sender_name="Alice",
                sender_wechat_id="wxid_alice",
                text="hello",
                observed_at="2026-06-28T02:00:00+00:00",
            )
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.message_id, second.message_id)
        self.assertNotEqual(first.received_at, second.received_at)

    def test_missing_raw_id_uses_observed_at_to_avoid_over_deduping_repeated_text(self) -> None:
        normalizer = MessageNormalizer()
        first = normalizer.normalize(
            RawWeChatMessage(
                raw_id="",
                chat_title="Alice",
                sender_name="Alice",
                sender_wechat_id="wxid_alice",
                text="hello",
                observed_at="2026-06-28T01:00:00+00:00",
            )
        )
        second = normalizer.normalize(
            RawWeChatMessage(
                raw_id="",
                chat_title="Alice",
                sender_name="Alice",
                sender_wechat_id="wxid_alice",
                text="hello",
                observed_at="2026-06-28T01:00:01+00:00",
            )
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.message_id, second.message_id)

    def test_self_message_is_normalized_for_ledger_recording(self) -> None:
        message = MessageNormalizer().normalize(
            RawWeChatMessage(
                raw_id="self-1",
                chat_title="Alice",
                sender_name="Me",
                text="sent by me",
                is_self=True,
                observed_at="2026-06-28T01:00:00+00:00",
            )
        )

        self.assertIsNotNone(message)
        self.assertTrue(message.is_self)


if __name__ == "__main__":
    unittest.main()

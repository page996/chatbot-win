from __future__ import annotations

import unittest

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.domain.models import NormalizedMessage
from app.personal_wechat_bot.router.deduper import Deduper
from app.personal_wechat_bot.router.router import Router


class RouterDedupTimingTest(unittest.TestCase):
    def test_route_decision_does_not_mark_message_until_done(self) -> None:
        config = BotConfig()
        router = Router(config, Deduper())
        message = NormalizedMessage(
            message_id="message-1",
            conversation_id="private-1",
            conversation_type="private",
            chat_title="Alice",
            sender_name="Alice",
            text="hello",
            is_self=False,
            received_at="2026-06-28T01:00:00+00:00",
            sender_wechat_id="wxid_alice",
        )

        first = router.decide(message)
        second_before_done = router.decide(message)
        router.mark_done(message.message_id)
        third_after_done = router.decide(message)

        self.assertEqual(first.action, "process")
        self.assertEqual(second_before_done.action, "process")
        self.assertEqual(third_after_done.action, "duplicate")

    def test_private_contact_can_match_chat_title_when_wechat_id_is_missing(self) -> None:
        config = BotConfig()
        router = Router(config, Deduper())
        message = NormalizedMessage(
            message_id="message-page",
            conversation_id="private-page",
            conversation_type="private",
            chat_title="PAGE",
            sender_name="PAGE",
            text="hello",
            is_self=False,
            received_at="2026-06-28T01:00:00+00:00",
            sender_wechat_id=None,
        )

        decision = router.decide(message)

        self.assertEqual(decision.action, "process")

    def test_unknown_group_is_auto_accepted_for_channel_routing(self) -> None:
        config = BotConfig()
        router = Router(config, Deduper())
        message = NormalizedMessage(
            message_id="message-group",
            conversation_id="group-unknown",
            conversation_type="group",
            chat_title="New Group",
            sender_name="Alice",
            text="@bot hello",
            is_self=False,
            received_at="2026-06-28T01:00:00+00:00",
            sender_wechat_id="wxid_alice",
        )

        decision = router.decide(message)

        self.assertEqual(decision.action, "process")
        self.assertTrue(decision.requires_topic_decision)


if __name__ == "__main__":
    unittest.main()

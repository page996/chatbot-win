from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.router.cooldown import ConversationCooldown


class ConversationCooldownTest(unittest.TestCase):
    def test_cooldown_blocks_within_window_and_allows_after_window(self) -> None:
        cooldown = ConversationCooldown(seconds=60)

        first = cooldown.allow("group-1", "2026-06-28T01:00:00+00:00")
        second = cooldown.allow("group-1", "2026-06-28T01:00:30+00:00")
        third = cooldown.allow("group-1", "2026-06-28T01:01:01+00:00")

        self.assertEqual(first, (True, "cooldown_allowed"))
        self.assertFalse(second[0])
        self.assertIn("group_cooldown", second[1])
        self.assertEqual(third, (True, "cooldown_allowed"))

    def test_cooldown_persists_last_reply_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cooldowns.sqlite"
            first = ConversationCooldown(seconds=60, db_path=path)
            self.assertEqual(first.allow("group-1", "2026-06-28T01:00:00+00:00")[0], True)

            second = ConversationCooldown(seconds=60, db_path=path)

            self.assertEqual(second.allow("group-1", "2026-06-28T01:00:30+00:00")[0], False)


if __name__ == "__main__":
    unittest.main()

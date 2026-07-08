import unittest

from app.personal_wechat_bot.conversation.channel_control import normalize_control_mode, parse_bool, snooze_is_active


class ChannelControlHelperTests(unittest.TestCase):
    def test_parse_bool_handles_common_values_and_fallback(self) -> None:
        self.assertTrue(parse_bool("on"))
        self.assertTrue(parse_bool("YES"))
        self.assertFalse(parse_bool("false"))
        self.assertFalse(parse_bool("0"))
        self.assertTrue(parse_bool("maybe", default=True))
        self.assertFalse(parse_bool(None, default=False))

    def test_normalize_control_mode_defaults_unknown_values_to_active(self) -> None:
        self.assertEqual(normalize_control_mode("PAUSED"), "paused")
        self.assertEqual(normalize_control_mode(" muted "), "muted")
        self.assertEqual(normalize_control_mode("bad-mode"), "active")
        self.assertEqual(normalize_control_mode(None), "active")

    def test_snooze_is_active_treats_future_and_invalid_values_as_blocking(self) -> None:
        self.assertTrue(snooze_is_active("2999-01-01T00:00:00Z"))
        self.assertFalse(snooze_is_active("2000-01-01T00:00:00Z"))
        self.assertTrue(snooze_is_active(""))
        self.assertTrue(snooze_is_active("not-a-date"))


if __name__ == "__main__":
    unittest.main()

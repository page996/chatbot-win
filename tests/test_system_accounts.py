from __future__ import annotations

import unittest

from app.personal_wechat_bot.wechat_driver.system_accounts import is_system_account


class SystemAccountsTest(unittest.TestCase):
    def test_filehelper_is_system(self) -> None:
        self.assertTrue(is_system_account("filehelper"))
        self.assertTrue(is_system_account("FileHelper"))
        self.assertTrue(is_system_account("  filehelper  "))

    def test_official_account_prefix(self) -> None:
        self.assertTrue(is_system_account("gh_abcdef123456"))

    def test_other_system_ids(self) -> None:
        for talker in ("weixin", "fmessage", "medianote", "floatbottle", "newsapp"):
            self.assertTrue(is_system_account(talker), talker)

    def test_real_users_are_not_system(self) -> None:
        for talker in ("wxid_abc123", "PAGE", "room1@chatroom", "alice"):
            self.assertFalse(is_system_account(talker), talker)

    def test_empty_is_not_system(self) -> None:
        self.assertFalse(is_system_account(""))
        self.assertFalse(is_system_account(None))


if __name__ == "__main__":
    unittest.main()

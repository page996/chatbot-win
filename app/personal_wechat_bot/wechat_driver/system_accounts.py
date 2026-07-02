from __future__ import annotations

"""Well-known WeChat system / service accounts that are not real conversations.

These talker ids belong to WeChat's own built-in tools (file transfer helper,
system notifications, etc.). The agent should never open a conversation for
them, backfill their history, or reply to them. Filtering happens at the pull
source (so no hook events are produced) and again at normalization as a
defensive backstop.
"""


# Exact talker ids (wxid / special usernames) that are always system accounts.
SYSTEM_ACCOUNT_IDS: frozenset[str] = frozenset(
    {
        "filehelper",  # 文件传输助手
        "fmessage",  # 朋友推荐消息
        "medianote",  # 语音记事本
        "floatbottle",  # 漂流瓶
        "weixin",  # 微信团队
        "newsapp",  # 腾讯新闻
        "qmessage",  # QQ 离线消息
        "qqmail",  # QQ 邮箱提醒
        "tmessage",
        "qqsync",
        "notifymessage",
    }
)

# Prefixes that mark official / service accounts (公众号 etc.).
SYSTEM_ACCOUNT_PREFIXES: tuple[str, ...] = ("gh_",)


def is_system_account(talker_id: str | None) -> bool:
    """Return True when talker_id is a WeChat system/service account.

    Comparison is case-insensitive on the trimmed id. Official-account ids
    (``gh_...``) are matched by prefix.
    """

    if not talker_id:
        return False
    value = str(talker_id).strip().lower()
    if not value:
        return False
    if value in SYSTEM_ACCOUNT_IDS:
        return True
    for prefix in SYSTEM_ACCOUNT_PREFIXES:
        if prefix and value.startswith(prefix):
            return True
    return False

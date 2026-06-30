from __future__ import annotations

from app.personal_wechat_bot.conversation.session_store import (
    CLEAR_CONTEXT_PHRASES,
    DEFAULT_SESSION_ID,
    ConversationSessionStore,
    is_reset_command,
)


class ConversationContextStore(ConversationSessionStore):
    def __init__(self, data_dir, max_recent_messages: int | None = None):
        _ = max_recent_messages
        super().__init__(data_dir)

__all__ = [
    "CLEAR_CONTEXT_PHRASES",
    "DEFAULT_SESSION_ID",
    "ConversationContextStore",
    "ConversationSessionStore",
    "is_reset_command",
]

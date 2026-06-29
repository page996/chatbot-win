class BotError(Exception):
    """Base error for bot runtime failures."""


class ConfigError(BotError):
    """Raised when configuration is missing or invalid."""


class ToolPermissionError(BotError):
    """Raised when a tool tries to access a disallowed resource."""

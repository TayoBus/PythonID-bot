"""
Bot info caching service for the PythonID bot.

This module provides a simple cache for bot information (like username)
to avoid repeated API calls. The bot's username rarely changes, so
caching it after the first fetch is efficient.
"""

from aiogram import Bot


class BotInfoCache:
    """
    Cache for bot information.

    Caches the bot's username after the first API call to avoid
    repeated get_me() calls. The cache persists for the lifetime
    of the application.

    Usage:
        username = await BotInfoCache.get_username(bot)
    """

    # Class-level cache for bot username
    _username: str | None = None

    @classmethod
    async def get_username(cls, bot: Bot) -> str:
        """
        Get bot's username, fetching from API only on first call.

        Args:
            bot: Telegram bot instance.

        Returns:
            str: Bot's username (without @ prefix).
        """
        if cls._username is None:
            me = await bot.get_me()
            cls._username = me.username
        return cls._username

    @classmethod
    def reset(cls) -> None:
        """
        Clear the cached username (primarily for testing).
        """
        cls._username = None

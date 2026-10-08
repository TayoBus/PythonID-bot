"""Markdown helpers vendored from python-telegram-bot.

``escape_markdown`` / ``mention_markdown`` (Markdown v1 semantics) were
previously imported from ``telegram.helpers``. They are pure functions
with stable output; vendoring them here keeps message formatting
byte-identical after the aiogram migration.
"""

import re


def escape_markdown(text: str, version: int = 1) -> str:
    """Escape markdown characters.

    Args:
        text: Text to escape.
        version: Markdown version (1 or 2).

    Returns:
        Text with markdown control characters backslash-escaped.
    """
    if version == 1:
        escape_chars = r"_*`["
    elif version == 2:
        escape_chars = r"\_*[]()~`>#+-=|{}.!"
    else:
        raise ValueError("Markdown version must be either 1 or 2")
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)


def mention_markdown(user_id: int | str, name: str, version: int = 1) -> str:
    """Build a markdown mention link for a user.

    Args:
        user_id: Telegram user ID.
        name: Display name (should already be escaped).
        version: Markdown version (accepted for API compatibility).

    Returns:
        Markdown user mention, e.g. ``[Name](tg://user?id=123)``.
    """
    return f"[{name}](tg://user?id={user_id})"

"""Built-in plugin wrappers for the PythonID bot.

Each submodule exports registrar functions that register their handlers
against the shared ``AppState``, returning lists of ``HandlerSpec``.
"""

from bot.plugins.builtin import (
    captcha,
    commands,
    dm,
    jobs,
    profile_monitor,
    spam,
    topic_guard,
)

__all__ = [
    "captcha",
    "commands",
    "dm",
    "jobs",
    "profile_monitor",
    "spam",
    "topic_guard",
]
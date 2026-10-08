"""Built-in plugin: spam.

Wraps all anti-spam handlers (inline_keyboard_spam, bio_bait_spam,
contact_spam, new_user_spam, duplicate_spam) with their respective
filter and group patterns. All group-scoped callbacks are wrapped with
``guard_plugin`` for runtime per-group gating.

Also exposes individual registrar functions (register_inline_keyboard_spam,
register_bio_bait_spam, etc.) for fine-grained plugin registration.
"""

from __future__ import annotations

import logging

from bot.dispatch import (
    AppState,
    HandlerSpec,
    has_contact,
    is_command_message,
    is_group_chat,
)
from bot.handlers.anti_spam import (
    handle_contact_spam,
    handle_inline_keyboard_spam,
    handle_new_user_spam,
)
from bot.handlers.bio_bait import BIO_BAIT_FILTER, handle_bio_bait_spam
from bot.handlers.duplicate_spam import handle_duplicate_spam
from bot.handlers.guest_bot import guest_bot_filter, handle_guest_bot_message
from bot.plugins.config import guard_plugin

logger = logging.getLogger(__name__)

_MESSAGE_KINDS = ("message", "edited_message")


# --- Helper for spam handler spec construction ---

def _register_spam(
    plugin_name: str, group: int, check, callback, label: str
) -> list[HandlerSpec]:
    """Build a spam handler spec wrapped with guard_plugin."""
    spec = HandlerSpec(
        plugin_name=plugin_name,
        group=group,
        update_kinds=_MESSAGE_KINDS,
        check=check,
        callback=guard_plugin(plugin_name)(callback),
        label=label,
    )
    logger.info(f"Registered handler: {label} (group={group})")
    return [spec]


# --- Individual registrar functions ---

def register_inline_keyboard_spam(state: AppState) -> list[HandlerSpec]:
    """Register inline keyboard spam handler (group=1).

    Callback wrapped with ``guard_plugin("inline_keyboard_spam")``.
    """
    _ = state
    return _register_spam(
        "inline_keyboard_spam", 1, is_group_chat,
        handle_inline_keyboard_spam, "inline_keyboard_spam_handler",
    )

def register_guest_bot_block(state: AppState) -> list[HandlerSpec]:
    """Register guest bot block handler (group=0).

    Callback wrapped with ``guard_plugin("guest_bot_block")``. Runs at
    group=0 (same group as commands and captcha) to intercept Telegram
    Guest Mode messages before other spam checks at higher groups.
    """
    _ = state
    return _register_spam(
        "guest_bot_block", 0, guest_bot_filter,
        handle_guest_bot_message, "guest_bot_block_handler",
    )

def register_bio_bait_spam(state: AppState) -> list[HandlerSpec]:
    """Register bio bait spam handler (group=5).

    Callback wrapped with ``guard_plugin("bio_bait_spam")``. Must NOT share
    a group number with ``duplicate_spam`` (group=4): the dispatcher runs
    at most one handler per group (first match wins), so sharing a group
    would make this handler unreachable. Raising ``StopPropagation``
    after enforcement stops downstream groups.
    """
    _ = state
    return _register_spam(
        "bio_bait_spam", 5, BIO_BAIT_FILTER,
        handle_bio_bait_spam, "bio_bait_spam_handler",
    )

def register_contact_spam(state: AppState) -> list[HandlerSpec]:
    """Register contact spam handler (group=2).

    Callback wrapped with ``guard_plugin("contact_spam")``.
    """
    _ = state
    return _register_spam(
        "contact_spam", 2,
        lambda update: is_group_chat(update) and has_contact(update),
        handle_contact_spam, "contact_spam_handler",
    )

def register_new_user_spam(state: AppState) -> list[HandlerSpec]:
    """Register new user spam handler (probation, group=3).

    Callback wrapped with ``guard_plugin("new_user_spam")``.
    """
    _ = state
    return _register_spam(
        "new_user_spam", 3, is_group_chat,
        handle_new_user_spam, "anti_spam_handler",
    )

def register_duplicate_spam(state: AppState) -> list[HandlerSpec]:
    """Register duplicate message spam handler (group=4).

    Callback wrapped with ``guard_plugin("duplicate_spam")``. Raising
    ``StopPropagation`` after enforcement stops downstream handler groups.
    """
    _ = state
    return _register_spam(
        "duplicate_spam", 4,
        lambda update: is_group_chat(update) and not is_command_message(update),
        handle_duplicate_spam, "duplicate_spam_handler",
    )

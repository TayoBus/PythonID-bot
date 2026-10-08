"""Built-in plugin: commands.

Wraps all command and callback handlers (verify, unverify, check, trust,
untrust, trusted_list, check_forwarded_message, and their callbacks).
All register at group=0.

Note: guard_plugin is intentionally NOT applied to admin
commands/callbacks. Admin overrides must work in every group regardless
of plugin toggle state. This matches pre-refactor behavior where admin
commands were never gated.

Also exposes individual registrar functions (register_verify,
register_unverify, etc.) for fine-grained plugin registration.
"""

from __future__ import annotations

import logging

from bot.dispatch import (
    AppState,
    HandlerSpec,
    callback_data_pattern,
    command_filter,
    is_forwarded,
    is_private_chat,
)
from bot.handlers.check import (
    handle_check_command,
    handle_check_forwarded_message,
    handle_check_group_callback,
    handle_warn_callback,
)
from bot.handlers.trust import (
    handle_trust_callback,
    handle_trust_command,
    handle_trusted_list_command,
    handle_untrust_callback,
    handle_untrust_command,
)
from bot.handlers.verify import (
    handle_unrestrict_callback,
    handle_unverify_callback,
    handle_unverify_command,
    handle_verify_callback,
    handle_verify_command,
)
from bot.handlers.warn import handle_warn_command

logger = logging.getLogger(__name__)

_MESSAGE_KINDS = ("message", "edited_message")


# --- Helpers for handler spec construction ---

def _command_spec(
    state: AppState, command: str, callback, label: str
) -> list[HandlerSpec]:
    """Build a group=0 command handler spec (matches /cmd and /cmd@botname)."""
    spec = HandlerSpec(
        plugin_name=label,
        group=0,
        update_kinds=_MESSAGE_KINDS,
        check=command_filter(command, state),
        callback=callback,
        label=f"{label}_command",
        command=command,
    )
    logger.info(f"Registered handler: {label}_command (group=0)")
    return [spec]


def _callback_spec(plugin_name: str, pattern: str, callback, label: str) -> list[HandlerSpec]:
    """Build a group=0 callback query handler spec."""
    spec = HandlerSpec(
        plugin_name=plugin_name,
        group=0,
        update_kinds=("callback_query",),
        check=callback_data_pattern(pattern),
        callback=callback,
        label=label,
    )
    logger.info(f"Registered handler: {label} (group=0)")
    return [spec]


# --- Individual registrar functions ---

def register_verify(state: AppState) -> list[HandlerSpec]:
    """Register /verify command handler."""
    return _command_spec(state, "verify", handle_verify_command, "verify")


def register_unverify(state: AppState) -> list[HandlerSpec]:
    """Register /unverify command handler."""
    return _command_spec(state, "unverify", handle_unverify_command, "unverify")


def register_check(state: AppState) -> list[HandlerSpec]:
    """Register /check command handler."""
    return _command_spec(state, "check", handle_check_command, "check")


def register_trust(state: AppState) -> list[HandlerSpec]:
    """Register /trust command handler."""
    return _command_spec(state, "trust", handle_trust_command, "trust")


def register_untrust(state: AppState) -> list[HandlerSpec]:
    """Register /untrust command handler."""
    return _command_spec(state, "untrust", handle_untrust_command, "untrust")


def register_trusted_list(state: AppState) -> list[HandlerSpec]:
    """Register /trusted command handler."""
    return _command_spec(state, "trusted", handle_trusted_list_command, "trusted_list")


def register_check_forwarded_message(state: AppState) -> list[HandlerSpec]:
    """Register forwarded message handler for /check context."""
    _ = state
    spec = HandlerSpec(
        plugin_name="check_forwarded_message",
        group=0,
        update_kinds=_MESSAGE_KINDS,
        check=lambda update: is_private_chat(update) and is_forwarded(update),
        callback=handle_check_forwarded_message,
        label="check_forwarded_message",
    )
    logger.info("Registered handler: check_forwarded_message (group=0)")
    return [spec]


def register_check_group_callback(state: AppState) -> list[HandlerSpec]:
    """Register group selector callback for /check."""
    _ = state
    return _callback_spec(
        "check_group_callback", r"^checkgrp:-?\d+:\d+$",
        handle_check_group_callback, "check_group_callback",
    )


def register_verify_callback(state: AppState) -> list[HandlerSpec]:
    """Register verify callback handler (group-scoped)."""
    _ = state
    return _callback_spec(
        "verify_callback", r"^verify:-?\d+:\d+$",
        handle_verify_callback, "verify_callback",
    )


def register_unverify_callback(state: AppState) -> list[HandlerSpec]:
    """Register unverify callback handler (group-scoped)."""
    _ = state
    return _callback_spec(
        "unverify_callback", r"^unverify:-?\d+:\d+$",
        handle_unverify_callback, "unverify_callback",
    )


def register_warn_callback(state: AppState) -> list[HandlerSpec]:
    """Register warn callback handler (group-scoped)."""
    _ = state
    return _callback_spec(
        "warn_callback", r"^warn:-?\d+:\d+:",
        handle_warn_callback, "warn_callback",
    )


def register_trust_callback(state: AppState) -> list[HandlerSpec]:
    """Register trust callback handler (group-scoped)."""
    _ = state
    return _callback_spec(
        "trust_callback", r"^trust:-?\d+:\d+$",
        handle_trust_callback, "trust_callback",
    )


def register_untrust_callback(state: AppState) -> list[HandlerSpec]:
    """Register untrust callback handler (group-scoped)."""
    _ = state
    return _callback_spec(
        "untrust_callback", r"^untrust:-?\d+:\d+$",
        handle_untrust_callback, "untrust_callback",
    )


def register_unrestrict_callback(state: AppState) -> list[HandlerSpec]:
    """Register unrestrict callback handler (group-scoped)."""
    _ = state
    return _callback_spec(
        "unrestrict_callback", r"^unrestrict:-?\d+:\d+$",
        handle_unrestrict_callback, "unrestrict_callback",
    )


def register_warn_command(state: AppState) -> list[HandlerSpec]:
    """Register /warn command handler (in-group, admin-issued)."""
    return _command_spec(state, "warn", handle_warn_command, "warn_command")

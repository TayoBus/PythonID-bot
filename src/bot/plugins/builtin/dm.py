"""Built-in plugin: dm.

Wraps ``bot.handlers.dm.handle_dm`` for DM unrestriction flow.
Registers at group=0 for private text messages.

Also exposes individual registrar function ``register_dm`` for
fine-grained plugin registration.
"""

from __future__ import annotations

import logging

from aiogram.types import Update

from bot.dispatch import (
    AppState,
    HandlerSpec,
    command_filter,
    has_text,
    is_command_message,
    is_private_chat,
)
from bot.handlers.dm import handle_dm

logger = logging.getLogger(__name__)


# --- Individual registrar function ---

def _dm_check(state: AppState):
    """Build the DM-flow predicate.

    The flow owns free-text DMs and ``/start`` deep links. Every other
    command belongs to its command handler: without the carve-out, a
    ``/status`` DM (whose plugin registers after ``dm`` in group 0)
    would be swallowed here by first-match-wins and never reach
    ``handle_status``.
    """
    is_start = command_filter("start", state)

    def _check(update: Update) -> bool:
        return (
            is_private_chat(update)
            and has_text(update)
            and not (is_command_message(update) and not is_start(update))
        )

    return _check


def register_dm(state: AppState) -> list[HandlerSpec]:
    """Register DM handler spec."""
    spec = HandlerSpec(
        plugin_name="dm",
        group=0,
        update_kinds=("message", "edited_message"),
        check=_dm_check(state),
        callback=handle_dm,
        label="dm_handler",
    )
    logger.info("Registered handler: dm_handler (group=0)")
    return [spec]

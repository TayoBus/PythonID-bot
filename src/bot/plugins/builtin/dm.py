"""Built-in plugin: dm.

Wraps ``bot.handlers.dm.handle_dm`` for DM unrestriction flow.
Registers at group=0 for private text messages.

Also exposes individual registrar function ``register_dm`` for
fine-grained plugin registration.
"""

from __future__ import annotations

import logging

from bot.dispatch import AppState, HandlerSpec, has_text, is_private_chat
from bot.handlers.dm import handle_dm

logger = logging.getLogger(__name__)


# --- Individual registrar function ---

def register_dm(state: AppState) -> list[HandlerSpec]:
    """Register DM handler spec."""
    _ = state  # no state needed beyond the signature
    spec = HandlerSpec(
        plugin_name="dm",
        group=0,
        update_kinds=("message", "edited_message"),
        check=lambda update: is_private_chat(update) and has_text(update),
        callback=handle_dm,
        label="dm_handler",
    )
    logger.info("Registered handler: dm_handler (group=0)")
    return [spec]

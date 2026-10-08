"""Built-in plugin: profile_monitor.

Wraps ``bot.handlers.message.handle_message`` for profile compliance
monitoring. Registers at group=6 with GROUPS & ~COMMAND filter (runs
last, after duplicate_spam at group=4 and bio_bait_spam at group=5).
Applies runtime gating via ``guard_plugin("profile_monitor")``.

Also exposes individual registrar function ``register_profile_monitor``
for fine-grained plugin registration.
"""

from __future__ import annotations

import logging

from bot.dispatch import (
    AppState,
    HandlerSpec,
    is_command_message,
    is_group_chat,
)
from bot.handlers.message import handle_message
from bot.plugins.config import guard_plugin

logger = logging.getLogger(__name__)

# --- Individual registrar function ---

def register_profile_monitor(state: AppState) -> list[HandlerSpec]:
    """Register profile monitor handler spec (group=6).

    The callback is wrapped with ``guard_plugin("profile_monitor")`` for
    runtime per-group enable/disable gating.
    """
    _ = state
    spec = HandlerSpec(
        plugin_name="profile_monitor",
        group=6,
        update_kinds=("message", "edited_message"),
        check=lambda update: is_group_chat(update) and not is_command_message(update),
        callback=guard_plugin("profile_monitor")(handle_message),
        label="message_handler",
    )
    logger.info("Registered handler: message_handler (group=6)")
    return [spec]

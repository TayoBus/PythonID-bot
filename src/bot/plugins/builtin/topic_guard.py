"""Built-in plugin: topic_guard.

Wraps ``bot.handlers.topic_guard.guard_warning_topic`` (message +
edited_message, group=-1). Applies runtime gating via
``guard_plugin("topic_guard")``.

Also exposes individual registrar function ``register_topic_guard`` for
fine-grained plugin registration.
"""

from __future__ import annotations

import logging

from bot.dispatch import AppState, HandlerSpec, is_message_or_edited
from bot.handlers.topic_guard import guard_warning_topic
from bot.plugins.config import guard_plugin

logger = logging.getLogger(__name__)

# --- Individual registrar function ---

def register_topic_guard(state: AppState) -> list[HandlerSpec]:
    """Register topic_guard handler spec (group=-1).

    The callback is wrapped with ``guard_plugin("topic_guard")`` for
    runtime per-group enable/disable gating.
    """
    _ = state  # no state needed beyond the signature
    spec = HandlerSpec(
        plugin_name="topic_guard",
        group=-1,
        update_kinds=("message", "edited_message"),
        check=is_message_or_edited,
        callback=guard_plugin("topic_guard")(guard_warning_topic),
        label="topic_guard",
    )
    logger.info("Registered handler: topic_guard (group=-1, message + edited_message)")
    return [spec]

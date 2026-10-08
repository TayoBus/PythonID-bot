"""Built-in plugin: status.

Wraps ``bot.handlers.status`` for the ``/status`` DM-admin command.
No ``guard_plugin`` wrap — /status is admin-only by handler checks, not
per-group gated.
"""

from __future__ import annotations

import logging

from bot.dispatch import AppState, HandlerSpec
from bot.handlers import status

logger = logging.getLogger(__name__)


def register_status(state: AppState) -> list[HandlerSpec]:
    """Register /status command handler spec."""
    specs = status.get_handlers(state)
    # No guard_plugin wrap — /status is admin-gated by the handler itself
    logger.info("Registered handler: status (group=0)")
    return specs

"""Built-in plugin: captcha.

Wraps ``bot.handlers.captcha`` handlers for new member verification.
All register at group=0 via ``captcha.get_handlers()``.
All callbacks are wrapped with ``guard_plugin("captcha")``
for runtime per-group gating.

Also exposes individual registrar function ``register_captcha`` for
fine-grained plugin registration.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from bot.dispatch import AppState, HandlerSpec
from bot.handlers import captcha
from bot.plugins.config import guard_plugin

logger = logging.getLogger(__name__)

# --- Individual registrar function ---

def register_captcha(state: AppState) -> list[HandlerSpec]:
    """Register captcha handler specs.

    Each spec is rebuilt (not mutated) with its callback wrapped in
    ``guard_plugin("captcha")``.
    """
    _ = state  # no state needed beyond the signature
    guarded_callback = guard_plugin("captcha")
    registered = [
        replace(spec, callback=guarded_callback(spec.callback))
        for spec in captcha.get_handlers()
    ]
    logger.info("Registered handler: captcha_handlers (group=0)")
    return registered

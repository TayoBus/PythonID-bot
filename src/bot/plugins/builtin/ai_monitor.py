"""Built-in plugin: ai_spam_monitor.

Wraps ``bot.handlers.ai_spam_monitor`` for classifier.dev-powered spam
monitoring. Registers the message handler at group=7 (the last defense:
only messages that survived every enforcement handler reach it) and the
alert action callback at group=0 alongside other callbacks. Applies
runtime gating via ``guard_plugin("ai_spam_monitor")`` /
``guard_plugin("ai_spam_callback")``.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from bot.dispatch import AppState, HandlerSpec
from bot.handlers import ai_spam_monitor
from bot.plugins.config import guard_plugin

logger = logging.getLogger(__name__)

# --- Individual registrar functions ---

def _guarded_specs(plugin_name: str) -> list[HandlerSpec]:
    """Return handler specs for one plugin with guard_plugin applied."""
    guarded = guard_plugin(plugin_name)
    return [
        replace(spec, callback=guarded(spec.callback))
        for spec in ai_spam_monitor.get_handlers()
        if spec.plugin_name == plugin_name
    ]


def register_ai_spam_monitor(state: AppState) -> list[HandlerSpec]:
    """Register the AI spam monitor message handler (group=7).

    Runs at the highest handler group so it only classifies messages
    that passed all other handler groups. Wrapped with
    ``guard_plugin("ai_spam_monitor")`` for runtime per-group gating.
    """
    _ = state
    specs = _guarded_specs("ai_spam_monitor")
    logger.info("Registered handler: ai_spam_monitor (group=7)")
    return specs


def register_ai_spam_callback(state: AppState) -> list[HandlerSpec]:
    """Register the alert action callback handler (group=0).

    Runs alongside the other admin action callbacks. Wrapped with
    ``guard_plugin("ai_spam_callback")`` for runtime per-group gating.
    """
    _ = state
    specs = _guarded_specs("ai_spam_callback")
    logger.info("Registered handler: ai_spam_callback (group=0)")
    return specs

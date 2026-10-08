"""Built-in plugin: jobs.

Wraps periodic APScheduler jobs (auto_restrict_job, refresh_admin_ids_job).
Registers repeating jobs on the shared ``AsyncIOScheduler`` held by the
app state. The scheduler itself is started/stopped by the dispatcher's
startup/shutdown handlers in ``main.py``.

Also exposes individual registrar functions (register_auto_restrict_job,
register_refresh_admin_ids_job) for fine-grained plugin registration.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from bot.dispatch import AppState, HandlerSpec
from bot.services.admin_cache import refresh_admin_ids
from bot.services.scheduler import auto_restrict_expired_warnings

logger = logging.getLogger(__name__)

# --- Individual registrar functions ---

def register_auto_restrict_job(state: AppState) -> list[HandlerSpec]:
    """Register auto-restrict repeating job (every 5 minutes, first run in 5 minutes)."""
    scheduler = state.scheduler
    if scheduler is not None:
        scheduler.add_job(
            auto_restrict_expired_warnings,
            "interval",
            minutes=5,
            id="auto_restrict_job",
            args=[state],
            replace_existing=True,
            next_run_time=datetime.now(UTC) + timedelta(minutes=5),
        )
        logger.info("Scheduler registered: auto_restrict_job (every 5 minutes, first run in 5 minutes)")
    # Jobs don't return handler specs
    return []

def register_refresh_admin_ids_job(state: AppState) -> list[HandlerSpec]:
    """Register admin cache refresh job (every 10 minutes, first run in 10 minutes)."""
    scheduler = state.scheduler
    if scheduler is not None:
        scheduler.add_job(
            refresh_admin_ids,
            "interval",
            minutes=10,
            id="refresh_admin_ids_job",
            args=[state],
            replace_existing=True,
            next_run_time=datetime.now(UTC) + timedelta(minutes=10),
        )
        logger.info("Scheduler registered: refresh_admin_ids_job (every 10 minutes, first run in 10 minutes)")
    # Jobs don't return handler specs
    return []

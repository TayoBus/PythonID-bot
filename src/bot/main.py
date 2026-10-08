"""
Main entry point for the PythonID bot.

This module initializes the bot, registers all handlers via the plugin
system, and starts the polling loop (aiogram 3.x).
"""

import asyncio
import logging
import time
from typing import Literal

import logfire
from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent, TelegramObject, Update
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.config import get_settings
from bot.database.service import get_database, init_database
from bot.dispatch import AppState, dispatch_update, handle_bot_error
from bot.group_config import get_group_registry, init_group_registry
from bot.plugins.manager import PluginManager
from bot.services.admin_cache import preload_admin_ids
from bot.services.captcha_recovery import recover_pending_captchas
from bot.services.classifier_client import close_client


def configure_logging() -> None:
    """
    Configure logging with Logfire integration.

    Uses minimal instrumentation to conserve Logfire quota:
    - Configurable log level via LOG_LEVEL environment variable
    - Disables database query tracing
    - Disables auto-instrumentation for less critical operations
    - Suppresses verbose HTTP request logs from httpx/httpcore libraries
    - In local dev: console output only (send_to_logfire=False)
    - In production: sends to Logfire only if LOGFIRE_TOKEN is set
    """
    # Configure basic logging FIRST to capture Settings initialization logs
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
        force=True,  # Override any existing config
    )

    # Now load settings (this will trigger model_post_init logging)
    settings = get_settings()

    # Get log level from settings and convert to logging constant
    log_level_str = settings.log_level.upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    # Determine if we should send to Logfire
    # Only send if enabled AND token is provided
    send_to_logfire = settings.logfire_enabled and settings.logfire_token is not None

    # Map log level to Logfire console min_log_level
    logfire_min_level: Literal["trace", "debug", "info", "notice", "warn", "warning", "error", "fatal"] = log_level_str.lower()  # type: ignore[assignment]

    # Configure Logfire with minimal instrumentation
    logfire.configure(
        token=settings.logfire_token,
        service_name=settings.logfire_service_name,
        environment=settings.logfire_environment,
        send_to_logfire=send_to_logfire,
        console=logfire.ConsoleOptions(
            colors="auto",
            include_timestamps=True,
            min_log_level=logfire_min_level,
        ),
        # Disable auto-instrumentation to save quota
        inspect_arguments=False,
    )

    # Reconfigure logging with Logfire handler and configured level
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=log_level,
        handlers=[logfire.LogfireLoggingHandler()],
        force=True,  # Override previous config
    )

    # Suppress verbose HTTP logs from httpx/httpcore used by aiogram
    # These libraries log every HTTP request at INFO level, flooding logs with Telegram API polling requests
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info(f"Logging level set to {log_level_str}")
    if send_to_logfire:
        logger.info(f"Logfire enabled - sending logs to {settings.logfire_environment}")
    else:
        logger.info("Logfire disabled - console output only")

logger = logging.getLogger(__name__)

async def error_handler(event: ErrorEvent) -> None:
    """
    Handle errors in the bot.

    Logs the error and continues operation. Network errors are logged
    at warning level since they're transient issues.
    """
    await handle_bot_error(event.update, event.exception)

async def on_shutdown(bot: Bot, app_state: AppState) -> None:
    """Release shared resources after the bot stops polling."""
    _ = bot
    if app_state.scheduler is not None:
        app_state.scheduler.shutdown()
        logger.info("on_shutdown: scheduler stopped")
    await close_client()
    logger.info("on_shutdown: classifier HTTP client closed")


async def on_startup(bot: Bot, app_state: AppState) -> None:
    """
    Startup handler: resolve bot username, start the scheduler, fetch and
    cache group admin IDs.

    This runs once after the dispatcher starts and before polling begins.
    Uses ``preload_admin_ids`` which preserves existing cached data
    for groups that fail to fetch, preventing admin cache wipe on
    startup failures.
    """
    logger.info("Starting on_startup: resolving bot username, starting scheduler, fetching admin IDs")

    me = await bot.get_me()
    app_state.bot_username = me.username
    logger.info(f"Bot username resolved: @{me.username}")

    if app_state.scheduler is not None:
        app_state.scheduler.start()
        logger.info("Scheduler started")

    registry = get_group_registry()

    # Use preload_admin_ids which preserves cache on failures
    await preload_admin_ids(app_state)

    # Record start time for /status uptime
    app_state.start_time = time.monotonic()

    # Preload trusted users cache
    db = get_database()
    trusted_ids = db.get_trusted_user_ids()
    app_state.trusted_user_ids = trusted_ids
    logger.info(f"Loaded {len(trusted_ids)} trusted user(s) into cache")

    # Recover pending captcha verifications for groups with captcha enabled
    has_captcha = any(gc.captcha_enabled for gc in registry.all_groups())
    if has_captcha:
        logger.info("Recovering pending captcha verifications from database")
        await recover_pending_captchas(app_state)


def build_dispatcher() -> Dispatcher:
    """Build the aiogram Dispatcher with lifecycle, error, and update wiring.

    The single update entrypoint runs the manual group-ordered dispatch
    (``dispatch_update``) for every incoming update.

    Two aiogram behaviors shape this wiring — do not "simplify" it:

    1. Never register the entrypoint on ``dp.update()``. ``Dispatcher.__init__``
       registers its own internal ``_listen_update`` handler there first, and
       ``TelegramEventObserver.trigger()`` stops at the first matching
       handler, so a user ``@dp.update()`` handler never runs and every update
       is logged "not handled". The four concrete sub-observers below have no
       internal handlers, so the entrypoint is always reached.
    2. The shared-state kwarg is named ``app_state``, not ``state``.
       ``FSMContextMiddleware`` (always registered on ``dp.update``) injects
       its own ``FSMContext`` as ``state`` on update handlers, which would
       shadow ours.
    """
    dp = Dispatcher()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    dp.errors.register(error_handler)

    @dp.message()
    @dp.edited_message()
    @dp.callback_query()
    @dp.chat_member()
    async def _route_update(
        event: TelegramObject,
        bot: Bot,
        app_state: AppState,
        event_update: Update,
    ) -> None:
        # event_update is the full Update (aiogram sets it in _listen_update);
        # dispatch_update needs the Update wrapper, not the bare event.
        await dispatch_update(event_update, bot, app_state)

    return dp

def main() -> None:
    """
    Initialize and run the bot.

    This function:
    1. Configures logging with Logfire integration
    2. Loads configuration from environment
    3. Initializes the group registry (from groups.json or .env fallback)
    4. Initializes the SQLite database
    5. Builds the aiogram Bot + shared AppState (with APScheduler)
    6. Registers all handler specs and jobs via PluginManager in MANIFEST_ORDER
    7. Computes per-group effective plugin toggle map for runtime gating
    8. Starts the bot polling loop
    """
    # Configure logging first
    configure_logging()

    settings = get_settings()

    # Initialize group registry
    registry = init_group_registry(settings)
    group_count = len(registry.all_groups())
    logger.info(f"Starting PythonID bot (environment: {settings.logfire_environment}, groups: {group_count})")
    for gc in registry.all_groups():
        logger.info(
            f"  Group {gc.group_id}: warning_topic={gc.warning_topic_id}, "
            f"restrict={gc.restrict_failed_users}, captcha={gc.captcha_enabled}"
        )

    # Initialize database (creates tables if they don't exist)
    init_database(settings.database_path)
    logger.info(f"Database initialized at {settings.database_path}")

    # Build the bot, shared state, and scheduler
    bot = Bot(token=settings.telegram_bot_token)
    # misfire_grace_time=None: never skip a late job. APScheduler 3's default
    # is 1 second, which would silently drop captcha timeouts whenever the
    # event loop is briefly busy (exactly when the bot is under load).
    # PTB's JobQueue always ran late jobs; all three job types here are
    # DB-guarded or idempotent, so running late is always safe.
    state = AppState(
        bot=bot,
        scheduler=AsyncIOScheduler(job_defaults={"misfire_grace_time": None}),
    )
    logger.info("Bot and application state built successfully")

    # Register all handler specs and jobs via PluginManager in deterministic order
    pm = PluginManager()
    plugin_handlers = pm.register_all(state)

    logger.info(f"Registered {sum(len(h) for h in plugin_handlers.values())} handler(s) across {len(plugin_handlers)} plugin(s)")

    # Compute and store per-group effective plugin toggle map for runtime gating
    pm.compute_effective_map(settings, registry, state)
    logger.info("Computed per-group effective plugin toggle map")

    # Wire up the dispatcher: startup/shutdown lifecycle, error handler,
    # and a single update entrypoint that runs the group-ordered dispatch.
    dp = build_dispatcher()

    logger.info(f"Starting bot polling for {group_count} group(s)")
    logger.info("All handlers registered successfully")

    asyncio.run(
        dp.start_polling(
            bot,
            app_state=state,
            allowed_updates=["message", "edited_message", "callback_query", "chat_member"],
        )
    )

if __name__ == "__main__":
    main()

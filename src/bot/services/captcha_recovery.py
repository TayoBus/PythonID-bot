"""
Captcha recovery service for the PythonID bot.

This module handles recovery of lost captcha timeout jobs on bot restart.
Since the scheduler is in-memory, pending verifications need to be recovered
from the database to prevent users from being stuck in restricted state.
"""

import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot

from bot.constants import CAPTCHA_TIMEOUT_MESSAGE
from bot.database.service import get_database
from bot.dispatch import AppState
from bot.group_config import get_group_registry
from bot.services.bot_info import BotInfoCache
from bot.services.restriction_lock import restriction_lock
from bot.services.telegram_utils import get_user_mention_by_id

logger = logging.getLogger(__name__)


def get_captcha_job_name(group_id: int, user_id: int) -> str:
    """
    Generate consistent job name for captcha timeout.

    Args:
        group_id: Telegram group ID.
        user_id: Telegram user ID.

    Returns:
        str: Standardized job name for captcha timeout.
    """
    return f"captcha_timeout_{group_id}_{user_id}"


async def handle_captcha_expiration(
    bot: Bot,
    user_id: int,
    group_id: int,
    chat_id: int,
    message_id: int,
    user_full_name: str,
) -> None:
    """
    Handle captcha expiration for a user.

    Edits the challenge message to show timeout, removes from database.
    This is shared between live timeouts and recovery on restart.

    Args:
        bot: The bot instance.
        user_id: The user ID.
        group_id: The group ID.
        chat_id: The chat ID where the message was sent.
        message_id: The message ID of the captcha challenge.
        user_full_name: The user's full name.
    """
    db = get_database()

    async with restriction_lock(group_id, user_id):
        pending = db.get_pending_captcha(user_id, group_id)
        if not pending:
            logger.info(f"No pending captcha for user {user_id}, already verified")
            return

        removed = db.remove_pending_captcha(user_id, group_id)
        if not removed:
            logger.info(f"Captcha for user {user_id} already finalized, ignoring timeout")
            return

        # Create UserWarning to track this bot-applied restriction
        # Allows DM handler to unrestrict user later when profile is complete
        warning = db.get_or_create_user_warning(user_id, group_id)
        if not warning.is_restricted:
            db.mark_user_restricted(user_id, group_id)

    bot_username = await BotInfoCache.get_username(bot)
    dm_link = f"[hubungi robot](https://t.me/{bot_username}?start=verify_{group_id})"
    user_mention = get_user_mention_by_id(user_id, user_full_name)

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=CAPTCHA_TIMEOUT_MESSAGE.format(
                user_mention=user_mention,
                dm_link=dm_link,
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Failed to edit captcha timeout message: {e}")

    logger.info(f"User {user_id} captcha timeout - kept restricted")


async def captcha_timeout_callback(
    *,
    state: AppState,
    user_id: int,
    group_id: int,
    chat_id: int,
    message_id: int,
    user_full_name: str,
) -> None:
    """One-shot scheduler job: expire a pending captcha when its timeout elapses.

    Scheduled with a stable job id per (group_id, user_id) so re-scheduling
    replaces any existing timeout instead of stacking duplicates.
    """
    bot = state.bot
    if bot is None:
        logger.error("captcha_timeout_callback: no bot on AppState, skipping")
        return
    await handle_captcha_expiration(
        bot=bot,
        user_id=user_id,
        group_id=group_id,
        chat_id=chat_id,
        message_id=message_id,
        user_full_name=user_full_name,
    )


def schedule_captcha_timeout(
    state: AppState,
    *,
    group_id: int,
    user_id: int,
    chat_id: int,
    message_id: int,
    user_full_name: str,
    delay_seconds: float,
) -> None:
    """Schedule (or reschedule) the one-shot captcha timeout job.

    Args:
        state: Shared application state (scheduler).
        group_id: Telegram group ID.
        user_id: Telegram user ID.
        chat_id: Chat ID where the challenge message was sent.
        message_id: Message ID of the captcha challenge.
        user_full_name: User's display name for the timeout message.
        delay_seconds: Seconds from now until the timeout fires.
    """
    scheduler = state.scheduler
    if scheduler is None:
        logger.error("schedule_captcha_timeout: no scheduler on AppState")
        return
    scheduler.add_job(
        captcha_timeout_callback,
        "date",
        run_date=datetime.now(UTC) + timedelta(seconds=delay_seconds),
        id=get_captcha_job_name(group_id, user_id),
        kwargs={
            "state": state,
            "user_id": user_id,
            "group_id": group_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "user_full_name": user_full_name,
        },
        replace_existing=True,
    )


def cancel_captcha_timeout(state: AppState, group_id: int, user_id: int) -> bool:
    """Cancel a pending captcha timeout job.

    Args:
        state: Shared application state (scheduler).
        group_id: Telegram group ID.
        user_id: Telegram user ID.

    Returns:
        True if a pending timeout job was found and removed.
    """
    scheduler = state.scheduler
    if scheduler is None:
        return False
    job = scheduler.get_job(get_captcha_job_name(group_id, user_id))
    if job is None:
        return False
    job.remove()
    return True


async def recover_pending_captchas(state: AppState) -> None:
    """
    Recover pending captcha verifications on bot startup.

    Queries the database for all pending captcha records and:
    1. If timeout has already passed: immediately expire them
    2. If timeout hasn't passed yet: reschedule the timeout job

    Each pending captcha uses the timeout from its group's config.
    Captchas for groups no longer in the registry are skipped.

    This prevents users from being stuck in restricted state after bot restart.

    Args:
        state: Shared application state (bot + scheduler).
    """
    registry = get_group_registry()
    db = get_database()

    bot = state.bot
    if bot is None:
        logger.error("recover_pending_captchas: no bot on AppState, skipping")
        return

    pending_records = db.get_all_pending_captchas()

    if not pending_records:
        logger.info("No pending captcha verifications to recover")
        return

    logger.info(f"Recovering {len(pending_records)} pending captcha verification(s)")

    now = datetime.now(UTC)

    for record in pending_records:
        try:
            # Look up the group config for this captcha
            group_config = registry.get(record.group_id)
            if group_config is None:
                logger.warning(
                    f"Skipping captcha for user {record.user_id} in group {record.group_id} "
                    f"- group no longer in registry"
                )
                continue

            # Make created_at timezone-aware (SQLite stores without timezone)
            created_at_utc = record.created_at.replace(tzinfo=UTC)
            elapsed_seconds = (now - created_at_utc).total_seconds()
            remaining_seconds = group_config.captcha_timeout_timedelta.total_seconds() - elapsed_seconds

            if remaining_seconds <= 0:
                # Timeout has already passed, expire immediately
                logger.info(
                    f"Expiring captcha for user {record.user_id} "
                    f"(timeout passed {abs(remaining_seconds):.0f}s ago)"
                )

                await handle_captcha_expiration(
                    bot=bot,
                    user_id=record.user_id,
                    group_id=record.group_id,
                    chat_id=record.chat_id,
                    message_id=record.message_id,
                    user_full_name=record.user_full_name,
                )
            else:
                # Timeout hasn't passed yet, reschedule the job
                logger.info(
                    f"Rescheduling captcha timeout for user {record.user_id} "
                    f"(remaining: {remaining_seconds:.0f}s)"
                )

                schedule_captcha_timeout(
                    state,
                    group_id=record.group_id,
                    user_id=record.user_id,
                    chat_id=record.chat_id,
                    message_id=record.message_id,
                    user_full_name=record.user_full_name,
                    delay_seconds=remaining_seconds,
                )
        except Exception as e:
            logger.error(
                f"Failed to recover captcha for user {record.user_id}: {e}",
                exc_info=True,
            )
            continue

    logger.info("Captcha recovery complete")

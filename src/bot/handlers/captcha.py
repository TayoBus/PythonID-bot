"""
Captcha verification handler for the PythonID bot.

This module handles captcha verification for new group members. When a user
joins the group, they are restricted and presented with a captcha button.
If they don't verify within the timeout period, they remain restricted.
"""

import logging

from aiogram.enums import ChatMemberStatus
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from sqlalchemy.exc import IntegrityError

from bot.dispatch import (
    HandlerContext,
    HandlerSpec,
    StopPropagation,
    callback_data_pattern,
    effective_chat,
    has_new_chat_members,
    is_chat_member_update,
)
from bot.services.captcha_recovery import (
    cancel_captcha_timeout,
    schedule_captcha_timeout,
)

from bot.constants import (
    CAPTCHA_FAILED_VERIFICATION_MESSAGE,
    CAPTCHA_INCOMPLETE_PROFILE_MESSAGE,
    CAPTCHA_PROFILE_CHECK_FAILED_MESSAGE,
    CAPTCHA_VERIFIED_MESSAGE,
    CAPTCHA_WELCOME_MESSAGE,
    CAPTCHA_WRONG_USER_MESSAGE,
    MISSING_ITEMS_SEPARATOR,
    RESTRICTED_PERMISSIONS,
)
from bot.database.models import CaptchaData
from bot.database.service import DatabaseService, get_database
from bot.group_config import GroupConfig, get_group_config_for_update, get_group_registry
from bot.services.restriction_lock import restriction_lock
from bot.services.telegram_utils import (
    get_user_mention,
    restrict_chat_member_with_retry,
    unrestrict_user,
)
from bot.services.user_checker import check_user_profile

logger = logging.getLogger(__name__)


async def _initiate_captcha_challenge(
    context: HandlerContext,
    user: User,
    chat_id: int,
    group_config: GroupConfig,
) -> None:
    """
    Initiate captcha challenge for a new member.

    Sends captcha message with keyboard, stores in database, and schedules timeout job.

    Args:
        context: Bot context with helper methods and job queue.
        user: The user to challenge.
        chat_id: The group chat ID.
        group_config: Per-group configuration.
    """
    user_id = user.id
    user_mention = get_user_mention(user)

    try:
        ok = await restrict_chat_member_with_retry(
            context.bot,
            chat_id=chat_id,
            user_id=user_id,
            permissions=RESTRICTED_PERMISSIONS,
        )
        if not ok:
            logger.error(f"Gave up restricting new member {user_id} after RetryAfter")
            return
        logger.info(f"Restricted new member {user_id} ({user.full_name}) for captcha")
    except Exception as e:
        logger.error(f"Failed to restrict new member {user_id}: {e}")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Saya bukan robot",
            callback_data=f"captcha_verify_{chat_id}_{user_id}",
        )
    ]])

    welcome_message = CAPTCHA_WELCOME_MESSAGE.format(
        user_mention=user_mention,
        timeout=group_config.captcha_timeout_seconds,
    )

    sent_message = await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=group_config.warning_topic_id,
        text=welcome_message,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

    db = get_database()
    try:
        db.add_pending_captcha(
            CaptchaData(
                user_id=user_id,
                group_id=group_config.group_id,
                chat_id=sent_message.chat.id,
                message_id=sent_message.message_id,
                user_full_name=user.full_name,
            )
        )
    except IntegrityError:
        logger.info(f"Captcha already exists for user {user_id} (race condition handled)")
        return

    schedule_captcha_timeout(
        context.state,
        group_id=group_config.group_id,
        user_id=user_id,
        chat_id=sent_message.chat.id,
        message_id=sent_message.message_id,
        user_full_name=user.full_name,
        delay_seconds=group_config.captcha_timeout_seconds,
    )

    logger.info(
        f"Sent captcha challenge to user {user_id} ({user.full_name}), "
        f"timeout in {group_config.captcha_timeout_seconds}s"
    )


async def _maybe_start_captcha(
    context: HandlerContext,
    db: DatabaseService,
    member: User,
    group_config: GroupConfig,
) -> None:
    """
    Maybe start captcha verification for a new member.

    Starts probation, checks if captcha is enabled, checks for duplicates,
    and initiates captcha challenge if appropriate.
    """
    user_id = member.id

    db.start_new_user_probation(user_id, group_config.group_id)

    if not group_config.captcha_enabled:
        logger.info(f"Captcha disabled, probation started for user {user_id}")
        return

    if db.get_pending_captcha(user_id, group_config.group_id):
        logger.info(f"Captcha already pending for user {user_id}, skipping duplicate")
        return

    await _initiate_captcha_challenge(context, member, group_config.group_id, group_config)


async def new_member_handler(
    update: Update, context: HandlerContext
) -> None:
    """
    Handle new chat member events.

    When a new user joins the group, restrict their permissions and send
    a captcha challenge message with an inline button. Schedules a timeout
    job to ban the user if they don't verify in time.

    Args:
        update: Telegram update containing the new member info.
        context: Bot context with helper methods and job queue.
    """
    if not update.message or not update.message.new_chat_members:
        logger.info("No message or no new chat members, skipping")
        return

    group_config = get_group_config_for_update(update)

    if group_config is None:
        logger.info(f"Message from unmonitored chat {effective_chat(update).id if effective_chat(update) else None}, skipping")
        return

    logger.info(f"Processing new members: {len(update.message.new_chat_members)} member(s)")

    db = get_database()
    for new_member in update.message.new_chat_members:
        if new_member.is_bot:
            continue

        await _maybe_start_captcha(context, db, new_member, group_config)


async def chat_member_handler(
    update: Update, context: HandlerContext
) -> None:
    """
    Handle chat member updates to detect new members.

    This handler uses ChatMemberUpdated events to detect when users join,
    which works even when "Hide Join Messages" is enabled in the group.
    When a new user joins, restrict their permissions and send a captcha challenge.

    Args:
        update: Telegram update containing chat member changes.
        context: Bot context with helper methods and job queue.
    """
    if not update.chat_member:
        logger.info("No chat_member in update, skipping")
        return

    group_config = get_group_config_for_update(update)

    if group_config is None:
        logger.info(f"Update from unmonitored chat {effective_chat(update).id if effective_chat(update) else None}, skipping")
        return

    old_status = update.chat_member.old_chat_member.status
    new_status = update.chat_member.new_chat_member.status

    # Detect if this is a join event: user was not a member and now is a member
    left_statuses = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
    member_statuses = {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    }

    if old_status not in left_statuses or new_status not in member_statuses:
        logger.info(f"Not a join event: {old_status} -> {new_status}, skipping")
        return

    new_member = update.chat_member.new_chat_member.user

    if new_member.is_bot:
        logger.info(f"New member {new_member.id} is a bot, skipping")
        return

    logger.info(f"Detected new member via ChatMemberUpdated: {new_member.id} ({new_member.full_name})")

    db = get_database()
    await _maybe_start_captcha(context, db, new_member, group_config)


async def captcha_callback_handler(
    update: Update, context: HandlerContext
) -> None:
    """
    Handle captcha verification button press.

    Verifies that the user clicking the button is the same user who needs
    to verify. If valid, removes restrictions and cleans up.

    Args:
        update: Telegram update containing the callback query.
        context: Bot context with helper methods.
    """
    query = update.callback_query
    if not query or not query.data:
        return

    callback_user_id = query.from_user.id
    parts = query.data.split("_")
    try:
        target_user_id = int(parts[-1])
        group_id = int(parts[-2])
    except (ValueError, IndexError):
        logger.warning(f"Malformed captcha callback data: {query.data}")
        await query.answer(CAPTCHA_FAILED_VERIFICATION_MESSAGE, show_alert=True)
        return

    if callback_user_id != target_user_id:
        await query.answer(CAPTCHA_WRONG_USER_MESSAGE, show_alert=True)
        return

    db = get_database()
    registry = get_group_registry()

    group_config = registry.get(group_id)

    pending = db.get_pending_captcha(target_user_id, group_id)
    if group_config is None or not pending:
        logger.warning(f"No pending captcha found for user {target_user_id} in group {group_id}")
        await query.answer(CAPTCHA_FAILED_VERIFICATION_MESSAGE, show_alert=True)
        return

    # Validate that this callback comes from the original challenge message.
    # Prevents stale or spoofed callbacks from acting on a different challenge.
    if query.message and (
        query.message.chat.id != pending.chat_id
        or query.message.message_id != pending.message_id
    ):
        logger.warning(
            f"Captcha callback from wrong message for user {target_user_id}: "
            f"expected chat={pending.chat_id} msg={pending.message_id}, "
            f"got chat={query.message.chat.id} msg={query.message.message_id}"
        )
        await query.answer(CAPTCHA_FAILED_VERIFICATION_MESSAGE, show_alert=True)
        return

    try:
        result = await check_user_profile(context.bot, query.from_user)
    except Exception:
        logger.error(f"Profile check failed during captcha for user {target_user_id}", exc_info=True)
        await query.answer(CAPTCHA_PROFILE_CHECK_FAILED_MESSAGE, show_alert=True)
        return

    if not result.is_complete:
        missing_text = MISSING_ITEMS_SEPARATOR.join(result.get_missing_items())
        await query.answer(
            CAPTCHA_INCOMPLETE_PROFILE_MESSAGE.format(missing_text=missing_text),
            show_alert=True,
        )
        return

    # Telegram unrestrict first, then DB finalization, serialized via lock.
    # If unrestrict fails, pending captcha stays in DB and the user can retry.
    # The timeout job is still armed as a safety net.
    try:
        async with restriction_lock(group_config.group_id, target_user_id):
            await unrestrict_user(context.bot, group_config.group_id, target_user_id)
            db.mark_all_bot_restrictions_unrestricted(target_user_id, group_config.group_id)
            logger.info(f"Unrestricted verified user {target_user_id}")

            # DB finalization after Telegram success. Idempotent guard:
            # remove_pending_captcha returns False if a concurrent callback already
            # cleaned up — ack quietly and stop.
            try:
                removed = db.remove_pending_captcha(target_user_id, group_config.group_id)
                if not removed:
                    logger.info(f"Captcha for user {target_user_id} already finalized, ignoring duplicate callback")
                    raise StopPropagation
                db.start_new_user_probation(target_user_id, group_config.group_id)
            except StopPropagation:
                raise
            except Exception as e:
                logger.error(f"DB finalization failed for user {target_user_id}: {e}", exc_info=True)
                # User is already unrestricted on Telegram. DB inconsistency is
                # non-fatal — continue to show success message.
    except StopPropagation:
        await query.answer()
        return
    except Exception as e:
        logger.error(f"Failed to unrestrict user {target_user_id}: {e}", exc_info=True)
        await query.answer(CAPTCHA_FAILED_VERIFICATION_MESSAGE, show_alert=True)
        return

    if cancel_captcha_timeout(context.state, group_config.group_id, target_user_id):
        logger.info(f"Cancelled timeout job for user {target_user_id}")

    user_mention = get_user_mention(query.from_user)

    await query.answer()

    try:
        if query.message is not None:
            await query.message.edit_text(
                text=CAPTCHA_VERIFIED_MESSAGE.format(user_mention=user_mention),
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"Failed to edit captcha message: {e}")

    logger.info(f"User {target_user_id} ({query.from_user.full_name}) verified successfully")


def get_handlers() -> list[HandlerSpec]:
    """
    Return handler specs for captcha verification.

    Returns:
        List of HandlerSpec: chat member handler, message handler (fallback),
        and callback query handler. The plugin layer wraps callbacks with
        ``guard_plugin("captcha")`` before registration.
    """
    return [
        # Primary handler: ChatMemberUpdated - works even with hidden join messages
        HandlerSpec(
            plugin_name="captcha",
            group=0,
            update_kinds=("chat_member",),
            check=is_chat_member_update,
            callback=chat_member_handler,
            label="captcha_chat_member",
        ),
        # Fallback handler: new chat members - for groups with visible join messages
        HandlerSpec(
            plugin_name="captcha",
            group=0,
            update_kinds=("message", "edited_message"),
            check=has_new_chat_members,
            callback=new_member_handler,
            label="captcha_new_members",
        ),
        HandlerSpec(
            plugin_name="captcha",
            group=0,
            update_kinds=("callback_query",),
            check=callback_data_pattern(r"^captcha_verify_-?\d+_\d+$"),
            callback=captcha_callback_handler,
            label="captcha_callback",
        ),
    ]

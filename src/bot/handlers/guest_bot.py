"""Guest bot message moderation for Telegram Guest Mode."""

import logging

from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, Update, User

from bot.dispatch import HandlerContext, StopPropagation

from bot.constants import (
    GUEST_BOT_RESTRICTION,
    GUEST_BOT_WARNING,
    RESTRICTED_PERMISSIONS,
)
from bot.database.service import get_database
from bot.group_config import get_group_config_for_update
from bot.services.restriction_lock import restriction_lock
from bot.services.telegram_utils import (
    get_user_mention,
    is_user_admin_or_trusted,
    restrict_chat_member_with_retry,
)

logger = logging.getLogger(__name__)


def guest_bot_filter(update: Update) -> bool:
    """Filter predicate matching only Telegram Guest Mode messages.

    aiogram's ``Message`` model natively exposes the Bot API 10.1
    ``guest_bot_caller_user`` / ``guest_bot_caller_chat`` fields.
    """
    message = update.message or update.edited_message
    return message is not None and is_guest_bot_message(message)


# Backwards-compatible alias (the PTB ``MessageFilter`` subclass is gone).
GuestBotFilter = guest_bot_filter


def is_guest_bot_message(message: Message) -> bool:
    """Check if a message was posted by a guest bot."""
    return message.guest_bot_caller_user is not None or message.guest_bot_caller_chat is not None


def is_guest_bot_whitelisted(message: Message, whitelist: list[str]) -> bool:
    """Check if the guest bot that posted this message is whitelisted."""
    username = message.from_user.username if message.from_user else None
    if not username:
        return False
    return username.lower() in whitelist


async def handle_guest_bot_message(update: Update, context: HandlerContext) -> None:
    """Delete unapproved guest bot messages and progressively restrict their caller."""
    message = update.message or update.edited_message
    if message is None:
        return
    if update.edited_message is not None:
        # An edit re-delivers the same guest message; the original delivery
        # already deleted it and counted the caller's strike.
        return

    group_config = get_group_config_for_update(update)
    if group_config is None or not is_guest_bot_message(message):
        return
    if is_guest_bot_whitelisted(message, group_config.guest_bot_whitelist):
        return

    caller = message.guest_bot_caller_user or message.guest_bot_caller_chat
    try:
        await message.delete()
    except TelegramAPIError:
        logger.error("Failed to delete guest bot message", exc_info=True)

    if not isinstance(caller, User):
        raise StopPropagation
    if is_user_admin_or_trusted(context, group_config.group_id, caller.id):
        raise StopPropagation

    db = get_database()
    if db.is_user_restricted_by_bot(caller.id, group_config.group_id, warning_kind="guest_bot"):
        raise StopPropagation

    record = db.get_or_create_user_warning(caller.id, group_config.group_id, warning_kind="guest_bot")
    user_mention = get_user_mention(caller)

    if record.message_count >= group_config.warning_threshold:
        should_stop = False
        final_count = record.message_count
        async with restriction_lock(group_config.group_id, caller.id):
            if db.is_user_restricted_by_bot(caller.id, group_config.group_id, warning_kind="guest_bot"):
                should_stop = True
            else:
                fresh = db.get_or_create_user_warning(caller.id, group_config.group_id, warning_kind="guest_bot")
                if fresh.message_count < group_config.warning_threshold:
                    should_stop = True
                else:
                    ok = False
                    try:
                        ok = await restrict_chat_member_with_retry(
                            context.bot,
                            chat_id=group_config.group_id,
                            user_id=caller.id,
                            permissions=RESTRICTED_PERMISSIONS,
                        )
                    except TelegramAPIError as e:
                        logger.error("Failed to restrict guest bot caller %s: %s", caller.id, e, exc_info=True)
                    if ok:
                        db.mark_user_restricted(caller.id, group_config.group_id, warning_kind="guest_bot")
                        final_count = fresh.message_count
                    else:
                        # Do not increment on failure: count stays pinned at
                        # threshold so the next guest message retries the
                        # restriction instead of drifting past it forever.
                        should_stop = True
        if should_stop:
            raise StopPropagation
        try:
            await context.bot.send_message(
                chat_id=group_config.group_id,
                message_thread_id=group_config.warning_topic_id,
                text=GUEST_BOT_RESTRICTION.format(
                    user_mention=user_mention,
                    message_count=final_count,
                    rules_link=group_config.rules_link,
                ),
                parse_mode="Markdown",
            )
        except TelegramAPIError:
            logger.error("Failed to send guest bot restriction notice for user %s", caller.id, exc_info=True)
    elif record.message_count == 1:
        try:
            await context.bot.send_message(
                chat_id=group_config.group_id,
                message_thread_id=group_config.warning_topic_id,
                text=GUEST_BOT_WARNING.format(
                    user_mention=user_mention,
                    warning_threshold=group_config.warning_threshold,
                    rules_link=group_config.rules_link,
                ),
                parse_mode="Markdown",
            )
        except TelegramAPIError:
            logger.error("Failed to send guest bot warning for user %s", caller.id, exc_info=True)
        db.increment_message_count(caller.id, group_config.group_id, warning_kind="guest_bot")
    else:
        db.increment_message_count(caller.id, group_config.group_id, warning_kind="guest_bot")

    raise StopPropagation

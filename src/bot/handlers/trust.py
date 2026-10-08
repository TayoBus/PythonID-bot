"""Trusted-user command handlers for anti-spam bypass management.

Trust adds a user to the anti-spam bypass list and clears their probation.
It does NOT unrestrict the user — use the separate "Buka pembatasan bot"
action for that. This separation prevents trust from lifting manual
admin restrictions as a side effect.

Commands /trust, /untrust, /trusted are DM-only and require admin status.
Callback buttons (trust/untrust) are group-scoped: they encode group_id
and verify the caller is an admin of that specific group.
"""

import html
import logging
from datetime import UTC

from aiogram.types import InputRichMessage, Update

from bot.dispatch import HandlerContext, effective_chat
from bot.services.markdown import escape_markdown

from bot.constants import (
    TRUST_ADDED_MESSAGE,
    TRUST_ALREADY_EXISTS_MESSAGE,
    TRUST_CALLBACK_INVALID_MESSAGE,
    TRUST_DM_ONLY_MESSAGE,
    TRUST_LIST_EMPTY_MESSAGE,
    TRUST_LIST_HEADER,
    TRUST_LIST_RICH_COLUMNS,
    TRUST_LIST_RICH_HEADING,
    TRUST_NO_GROUP_PERMISSION_MESSAGE,
    TRUST_NO_PERMISSION_MESSAGE,
    TRUST_REMOVED_MESSAGE,
    TRUST_USER_ID_INVALID_MESSAGE,
    TRUST_USER_ID_REQUIRED_MESSAGE,
    TRUST_USER_NOT_FOUND_MESSAGE,
    WIB,
)
from bot.database.models import TrustedUserData
from bot.database.service import DatabaseService, get_database
from bot.group_config import GroupRegistry, get_group_registry
from bot.services.telegram_utils import (
    edit_callback_message,
    extract_forwarded_user,
    is_user_admin_in_group,
)

logger = logging.getLogger(__name__)


def _add_trusted_cache(context: HandlerContext, user_id: int) -> None:
    trusted = context.state.trusted_user_ids
    if trusted is None:
        trusted = set()
        context.state.trusted_user_ids = trusted
    trusted.add(user_id)


def _remove_trusted_cache(context: HandlerContext, user_id: int) -> None:
    trusted = context.state.trusted_user_ids
    if trusted is None:
        trusted = set()
        context.state.trusted_user_ids = trusted
    trusted.discard(user_id)


def _format_person(full_name: str, user_id: int) -> str:
    """Return a markdown-safe display for a stored person."""
    if full_name:
        # escape_markdown(version=1) already escapes `[`; only `]` still needs it.
        return escape_markdown(full_name, version=1).replace("]", r"\]")
    return f"User {user_id}"


def _format_person_with_username(full_name: str, username: str | None, user_id: int) -> str:
    display = _format_person(full_name, user_id)
    if username:
        display += f" (@{escape_markdown(username, version=1)})"
    return display


def _plain_person(full_name: str, username: str | None, user_id: int) -> str:
    """Return a plain-text display for a stored person (rich HTML path).

    Unlike _format_person_with_username this applies no Markdown escaping;
    the rich renderer applies html.escape() instead.
    """
    display = full_name or f"User {user_id}"
    if username:
        display += f" (@{username})"
    return display


def _rich_user_cell(full_name: str, username: str | None, user_id: int) -> str:
    """Render the User cell: display line plus the numeric ID below it.

    Returns HTML (already escaped). The standalone "User ID" column was
    dropped — four columns forced horizontal scrolling — so the ID is
    folded in here, wrapped in <code> to stay visible and copyable.
    """
    return (
        f"{html.escape(_plain_person(full_name, username, user_id))}"
        f"<br><code>{user_id}</code>"
    )


def _trusted_list_rich_html(rows: list[tuple[str, str, str]]) -> str:
    """Render trusted-user rows as a native rich table (Bot API 10.1+).

    Follows the proven <table bordered striped> pattern: header row plus
    one row per trusted user. ``rows`` are (user_cell_html, added_by, date);
    the user cell is pre-rendered HTML (see _rich_user_cell); the other
    cells are plain text and are html-escaped here.
    """
    header = "".join(f"<th>{html.escape(col)}</th>" for col in TRUST_LIST_RICH_COLUMNS)
    body = "".join(
        f"<tr><td>{user_cell}</td><td>{html.escape(added_by)}</td>"
        f"<td>{html.escape(date)}</td></tr>"
        for user_cell, added_by, date in rows
    )
    return (
        f"<b>{html.escape(TRUST_LIST_RICH_HEADING)}</b>"
        f"<table bordered striped><tr>{header}</tr>{body}</table>"
    )


def _resolve_target_user_id(
    update: Update, args: list[str]
) -> tuple[int | None, str | None]:
    """Resolve the target user ID from CLI args or a forwarded message."""
    if args:
        try:
            return int(args[0]), None
        except ValueError:
            return None, TRUST_USER_ID_INVALID_MESSAGE

    if update.message:
        forwarded = extract_forwarded_user(update.message)
        if forwarded:
            return forwarded[0], None

    return None, TRUST_USER_ID_REQUIRED_MESSAGE


async def trust_user(
    db: DatabaseService,
    registry: GroupRegistry,
    target_user_id: int,
    admin_user_id: int,
    target_user_full_name: str = "",
    target_username: str | None = None,
    admin_full_name: str = "",
    admin_username: str | None = None,
    group_id: int | None = None,
) -> int:
    """Add a trusted user and clear probation.

    Trust does NOT unrestrict the user. Unrestriction is a separate
    action ("Buka pembatasan bot") so that trusting a user doesn't
    inadvertently lift manual admin restrictions.

    If ``group_id`` is provided, probation is cleared only in that group.
    Otherwise, probation is cleared in all monitored groups (legacy behavior
    for the /trust command).

    Returns:
        int: Number of groups where probation was cleared.
    """
    db.add_trusted_user(
        TrustedUserData(
            user_id=target_user_id,
            trusted_by_admin_id=admin_user_id,
            user_full_name=target_user_full_name,
            username=target_username,
            admin_full_name=admin_full_name,
            admin_username=admin_username,
        )
    )

    cleared_probation = 0
    groups_to_check = (
        [registry.get(group_id)] if group_id is not None else registry.all_groups()
    )
    for group_config in groups_to_check:
        if group_config is None:
            continue
        try:
            if db.get_new_user_probation(target_user_id, group_config.group_id):
                db.clear_new_user_probation(target_user_id, group_config.group_id)
                cleared_probation += 1
        except Exception:
            logger.warning(
                f"Probation cleanup failed for user {target_user_id} in group {group_config.group_id}",
                exc_info=True,
            )

    return cleared_probation


async def handle_trust_command(
    update: Update, context: HandlerContext
) -> None:
    """Handle /trust command in bot DM."""
    if not update.message or not update.message.from_user:
        return

    chat = effective_chat(update)
    if chat and chat.type != "private":
        await update.message.reply(TRUST_DM_ONLY_MESSAGE)
        return

    admin_user_id = update.message.from_user.id
    admin_ids = context.state.admin_ids
    if admin_user_id not in admin_ids:
        await update.message.reply(TRUST_NO_PERMISSION_MESSAGE)
        return

    target_user_id, error_message = _resolve_target_user_id(update, context.args)
    if error_message is not None:
        await update.message.reply(error_message)
        return

    target_full_name = ""
    target_username = None
    if update.message.forward_from:
        target_full_name = update.message.forward_from.full_name
        target_username = update.message.forward_from.username

    db = get_database()
    registry = get_group_registry()

    try:
        cleared_count = await trust_user(
            db, registry, target_user_id, admin_user_id,
            target_user_full_name=target_full_name,
            target_username=target_username,
            admin_full_name=update.message.from_user.full_name,
            admin_username=update.message.from_user.username,
        )
        _add_trusted_cache(context, target_user_id)
        await update.message.reply(
            TRUST_ADDED_MESSAGE.format(
                user_id=target_user_id,
                probation_clear_count=cleared_count,
            ),
            parse_mode="Markdown",
        )
    except ValueError:
        await update.message.reply(
            TRUST_ALREADY_EXISTS_MESSAGE.format(user_id=target_user_id),
            parse_mode="Markdown",
        )


async def handle_untrust_command(
    update: Update, context: HandlerContext
) -> None:
    """Handle /untrust command in bot DM."""
    if not update.message or not update.message.from_user:
        return

    chat = effective_chat(update)
    if chat and chat.type != "private":
        await update.message.reply(TRUST_DM_ONLY_MESSAGE)
        return

    admin_user_id = update.message.from_user.id
    admin_ids = context.state.admin_ids
    if admin_user_id not in admin_ids:
        await update.message.reply(TRUST_NO_PERMISSION_MESSAGE)
        return

    target_user_id, error_message = _resolve_target_user_id(update, context.args)
    if error_message is not None:
        await update.message.reply(error_message)
        return

    db = get_database()

    try:
        db.remove_trusted_user(user_id=target_user_id)
        _remove_trusted_cache(context, target_user_id)
        await update.message.reply(
            TRUST_REMOVED_MESSAGE.format(user_id=target_user_id),
            parse_mode="Markdown",
        )
    except ValueError:
        await update.message.reply(
            TRUST_USER_NOT_FOUND_MESSAGE.format(user_id=target_user_id),
            parse_mode="Markdown",
        )


async def handle_trusted_list_command(
    update: Update, context: HandlerContext
) -> None:
    """Handle /trusted command in bot DM."""
    if not update.message or not update.message.from_user:
        return

    chat = effective_chat(update)
    if chat and chat.type != "private":
        await update.message.reply(TRUST_DM_ONLY_MESSAGE)
        return

    admin_user_id = update.message.from_user.id
    admin_ids = context.state.admin_ids
    if admin_user_id not in admin_ids:
        await update.message.reply(TRUST_NO_PERMISSION_MESSAGE)
        return

    db = get_database()
    trusted_users = db.get_trusted_users()

    if not trusted_users:
        await update.message.reply(TRUST_LIST_EMPTY_MESSAGE)
        return

    trusted_lines = []
    rich_rows = []
    for record in trusted_users:
        trusted_at = record.trusted_at
        if trusted_at.tzinfo is None:
            trusted_at = trusted_at.replace(tzinfo=UTC)
        trusted_at_display = trusted_at.astimezone(WIB).strftime("%Y-%m-%d %H:%M WIB")

        user_display = _format_person_with_username(
            record.user_full_name, record.username, record.user_id
        )
        admin_display = _format_person_with_username(
            record.admin_full_name, record.admin_username, record.trusted_by_admin_id
        )

        trusted_lines.append(
            f"• {user_display} (`{record.user_id}`) — oleh {admin_display} "
            f"(`{record.trusted_by_admin_id}`) pada `{trusted_at_display}`"
        )
        rich_rows.append(
            (
                _rich_user_cell(record.user_full_name, record.username, record.user_id),
                _plain_person(
                    record.admin_full_name,
                    record.admin_username,
                    record.trusted_by_admin_id,
                ),
                trusted_at_display,
            )
        )

    # Rich table is the default; any failure degrades to the Markdown list.
    # Broad except is deliberate: the Markdown path must survive whatever
    # broke the rich send (API errors, serialization, client issues).
    try:
        await context.bot.send_rich_message(
            chat_id=update.message.chat.id,
            rich_message=InputRichMessage(
                html=_trusted_list_rich_html(rich_rows)
            ),
            reply_parameters=update.message.as_reply_parameters(),
        )
    except Exception:
        logger.warning(
            "Rich trusted-list send failed, falling back to Markdown",
            exc_info=True,
        )
        await update.message.reply(
            TRUST_LIST_HEADER.format(trusted_lines="\n".join(trusted_lines)),
            parse_mode="Markdown",
        )


async def handle_trust_callback(
    update: Update, context: HandlerContext
) -> None:
    """Handle trust callback button (group-scoped).

    Callback data format: trust:{group_id}:{user_id}
    """
    query = update.callback_query
    if not query or not query.from_user or not query.data:
        return

    await query.answer()

    parts = query.data.split(":")
    try:
        group_id = int(parts[1])
        target_user_id = int(parts[2])
    except (IndexError, ValueError):
        await edit_callback_message(query, TRUST_CALLBACK_INVALID_MESSAGE)
        return

    admin_user_id = query.from_user.id
    if not is_user_admin_in_group(context, group_id, admin_user_id):
        await edit_callback_message(query, TRUST_NO_GROUP_PERMISSION_MESSAGE)
        return

    db = get_database()
    registry = get_group_registry()

    try:
        cleared_count = await trust_user(
            db, registry, target_user_id, admin_user_id,
            admin_full_name=query.from_user.full_name,
            admin_username=query.from_user.username,
            group_id=group_id,
        )
        _add_trusted_cache(context, target_user_id)
        await edit_callback_message(query, 
            TRUST_ADDED_MESSAGE.format(
                user_id=target_user_id,
                probation_clear_count=cleared_count,
            ),
            parse_mode="Markdown",
        )
    except ValueError:
        await edit_callback_message(query, 
            TRUST_ALREADY_EXISTS_MESSAGE.format(user_id=target_user_id),
            parse_mode="Markdown",
        )


async def handle_untrust_callback(
    update: Update, context: HandlerContext
) -> None:
    """Handle untrust callback button (group-scoped).

    Callback data format: untrust:{group_id}:{user_id}
    """
    query = update.callback_query
    if not query or not query.from_user or not query.data:
        return

    await query.answer()

    parts = query.data.split(":")
    try:
        group_id = int(parts[1])
        target_user_id = int(parts[2])
    except (IndexError, ValueError):
        await edit_callback_message(query, TRUST_CALLBACK_INVALID_MESSAGE)
        return

    admin_user_id = query.from_user.id
    if not is_user_admin_in_group(context, group_id, admin_user_id):
        await edit_callback_message(query, TRUST_NO_GROUP_PERMISSION_MESSAGE)
        return

    db = get_database()

    try:
        db.remove_trusted_user(user_id=target_user_id)
        _remove_trusted_cache(context, target_user_id)
        await edit_callback_message(query, 
            TRUST_REMOVED_MESSAGE.format(user_id=target_user_id),
            parse_mode="Markdown",
        )
    except ValueError:
        await edit_callback_message(query, 
            TRUST_USER_NOT_FOUND_MESSAGE.format(user_id=target_user_id),
            parse_mode="Markdown",
        )

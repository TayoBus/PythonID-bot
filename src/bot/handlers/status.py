"""
Status command handler for the PythonID bot.

Provides a DM-only, admin-only ``/status`` command that shows bot
operational state scoped to the groups the caller actually administers:
uptime, per-group config summary (enforcement mode, captcha, disabled
plugins), per-group probation and captcha queue lengths, database file
size, and last job timestamps.
"""

from __future__ import annotations

import html
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

from aiogram.types import InputRichMessage, Update

from bot.dispatch import AppState, HandlerContext, HandlerSpec, command_filter, effective_chat
from bot.services.markdown import escape_markdown

from bot.config import get_settings
from bot.constants import STATUS_RICH_COLUMNS, STATUS_RICH_HEADING, WIB
from bot.database.service import get_database
from bot.group_config import get_group_registry
from bot.services.telegram_utils import get_admin_groups

logger = logging.getLogger(__name__)


def _format_uptime(seconds: float) -> str:
    """Format monotonic seconds into Xd Yh Zm."""
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _format_filesize(path: str) -> str:
    """Return file size as KB or MB."""
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return "N/A"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / 1024:.1f} KB"


async def _check_status_prereqs(
    update: Update, context: HandlerContext
) -> bool:
    """Validate /status prerequisites: message exists, private chat, admin.

    Returns True if all checks pass. Sends error reply and returns False
    on any failure.
    """
    if not update.message or not update.message.from_user:
        logger.warning("handle_status called without message or sender")
        return False

    chat = effective_chat(update)
    if chat and chat.type != "private":
        await update.message.reply(
            "❌ Perintah ini hanya bisa digunakan di chat pribadi dengan bot."
        )
        return False

    admin_user_id = update.message.from_user.id
    admin_ids = context.state.admin_ids
    if admin_user_id not in admin_ids:
        await update.message.reply(
            "❌ Kamu tidak memiliki izin untuk menggunakan perintah ini."
        )
        logger.warning(
            f"Non-admin user {admin_user_id} ({update.message.from_user.full_name}) "
            "attempted to use /status command"
        )
        return False

    return True


async def handle_status(update: Update, context: HandlerContext) -> None:
    """Handle /status command in bot DM — show scoped operational state."""
    if not await _check_status_prereqs(update, context):
        return

    admin_user_id = update.message.from_user.id
    admin_group_ids = set(get_admin_groups(context, admin_user_id))

    data = _gather_status_data(context, admin_group_ids)
    markdown_text = _render_status_markdown(data)
    rich_html = _render_status_rich_html(data)

    # Rich table is the default; any failure degrades to the Markdown message.
    # Broad except is deliberate: the Markdown path must survive whatever
    # broke the rich send (API errors, serialization, client issues).
    try:
        await context.bot.send_rich_message(
            chat_id=update.message.chat.id,
            rich_message=InputRichMessage(html=rich_html),
            reply_parameters=update.message.as_reply_parameters(),
        )
    except Exception:
        logger.warning(
            "Rich status send failed, falling back to Markdown",
            exc_info=True,
        )
        await update.message.reply(markdown_text, parse_mode="Markdown")


@dataclass
class _GroupStatusRow:
    """Per-group status data, scoped to the caller's admin groups."""

    group_id: int
    enforcement: str
    captcha_enabled: bool
    probation_count: int
    pending_count: int
    disabled: list[str] = field(default_factory=list)


@dataclass
class _StatusData:
    """Everything /status displays, gathered once for both renderers."""

    uptime: str
    rows: list[_GroupStatusRow] = field(default_factory=list)
    db_size: str = "N/A"
    refresh_display: str = "belum pernah"
    restrict_display: str = "belum pernah"


def _gather_status_data(
    context: HandlerContext, admin_group_ids: set[int]
) -> _StatusData:
    """Collect /status values once; Markdown and rich renderers share them."""
    start = context.state.start_time
    if start is not None:
        uptime = _format_uptime(time.monotonic() - start)
    else:
        uptime = "N/A"

    registry = get_group_registry()
    effective_map = context.state.plugin_effective_map
    db = get_database()

    all_probations = db.get_all_new_user_probations()
    all_pending = db.get_all_pending_captchas()

    rows: list[_GroupStatusRow] = []
    for gc in registry.all_groups():
        gid = gc.group_id
        if gid not in admin_group_ids:
            continue
        toggles = effective_map.get(gid, {})
        disabled = sorted(k for k, v in toggles.items() if not v)
        rows.append(
            _GroupStatusRow(
                group_id=gid,
                enforcement="Restriksi" if gc.restrict_failed_users else "Peringatan",
                captcha_enabled=gc.captcha_enabled,
                probation_count=sum(1 for p in all_probations if p.group_id == gid),
                pending_count=sum(1 for p in all_pending if p.group_id == gid),
                disabled=disabled,
            )
        )

    db_size = _format_filesize(get_settings().database_path)

    refresh_ts = context.state.data.get("last_admin_refresh")
    if refresh_ts is not None:
        refresh_display = datetime.fromtimestamp(refresh_ts, tz=WIB).strftime(
            "%Y-%m-%d %H:%M:%S WIB"
        )
    else:
        refresh_display = "belum pernah"

    restrict_ts = context.state.data.get("last_auto_restrict")
    if restrict_ts is not None:
        restrict_display = datetime.fromtimestamp(restrict_ts, tz=WIB).strftime(
            "%Y-%m-%d %H:%M:%S WIB"
        )
    else:
        restrict_display = "belum pernah"

    return _StatusData(
        uptime=uptime,
        rows=rows,
        db_size=db_size,
        refresh_display=refresh_display,
        restrict_display=restrict_display,
    )


def _render_status_markdown(data: _StatusData) -> str:
    """Render /status as the pre-existing Markdown message (fallback path).

    Kept byte-identical to the original output; the rich renderer is the
    default and this only runs when the rich send fails.
    """
    lines: list[str] = []

    lines.append(f"*Uptime:* {data.uptime}")

    lines.append("")
    lines.append("*Grup yang kamu admin:*")

    if data.rows:
        for row in data.rows:
            group_parts = [
                f"  • `{escape_markdown(str(row.group_id), version=1)}`"
                f" — _{row.enforcement}_"
            ]
            if row.captcha_enabled:
                group_parts.append(" _CAPTCHA_")
            group_parts.append(
                f"\n    Probation: {row.probation_count}, Captcha: {row.pending_count}"
            )
            if row.disabled:
                disabled_str = ", ".join(row.disabled)
                group_parts.append(
                    f"\n    Plugin nonaktif: {escape_markdown(disabled_str, version=1)}"
                )
            lines.append("".join(group_parts))
    else:
        lines.append("  (Tidak ada grup yang dipantau)")

    lines.append("")
    lines.append(f"*Database:* {data.db_size}")

    lines.append("")
    lines.append("*Jadwal terakhir:*")
    lines.append(f"  • Refresh admin: {data.refresh_display}")
    lines.append(f"  • Auto-restrict: {data.restrict_display}")

    return "\n".join(lines)


def _render_status_rich_html(data: _StatusData) -> str:
    """Render /status as a native rich message (Bot API 10.1+).

    Per-group striped table plus a small key-value section. All cell
    content is html-escaped.
    """
    parts = [f"<b>{html.escape(STATUS_RICH_HEADING)}</b>"]

    if data.rows:
        header = "".join(
            f"<th>{html.escape(col)}</th>" for col in STATUS_RICH_COLUMNS
        )
        body = "".join(
            "<tr>"
            + "".join(
                f"<td>{html.escape(cell)}</td>"
                for cell in (
                    str(row.group_id),
                    row.enforcement,
                    "Ya" if row.captcha_enabled else "—",
                    str(row.probation_count),
                    str(row.pending_count),
                    ", ".join(row.disabled) if row.disabled else "—",
                )
            )
            + "</tr>"
            for row in data.rows
        )
        parts.append(f"<table bordered striped><tr>{header}</tr>{body}</table>")
    else:
        parts.append(f"<i>{html.escape('Tidak ada grup yang dipantau.')}</i>")

    parts.append(
        f"<b>Uptime:</b> {html.escape(data.uptime)}<br>"
        f"<b>Database:</b> {html.escape(data.db_size)}<br>"
        f"<b>Refresh admin:</b> {html.escape(data.refresh_display)}<br>"
        f"<b>Auto-restrict:</b> {html.escape(data.restrict_display)}"
    )
    return "".join(parts)


def get_handlers(state: AppState) -> list[HandlerSpec]:
    """Return list of handler specs for the status command.

    Args:
        state: Shared application state (provides the bot username for
            ``/status`` vs ``/status@botname`` command matching).
    """
    return [
        HandlerSpec(
            plugin_name="status",
            group=0,
            update_kinds=("message", "edited_message"),
            check=command_filter("status", state),
            callback=handle_status,
            label="status_command",
            command="status",
        )
    ]

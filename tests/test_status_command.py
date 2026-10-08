"""Tests for the /status command handler."""

import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.database.models import CaptchaData
from bot.database.service import get_database, init_database, reset_database
from bot.dispatch import AppState, HandlerContext
from bot.group_config import GroupConfig, GroupRegistry
from bot.handlers.status import handle_status


@pytest.fixture(autouse=True)
def temp_db():
    with TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        reset_database()
        init_database(str(db_path))
        yield db_path
        reset_database()


@pytest.fixture
def mock_registry():
    registry = GroupRegistry()
    registry.register(GroupConfig(
        group_id=-1001, warning_topic_id=11,
        captcha_enabled=True,
    ))
    registry.register(GroupConfig(
        group_id=-1002, warning_topic_id=12,
        captcha_enabled=False,
    ))
    return registry


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.database_path = "/tmp/test.db"
    return settings


@pytest.fixture
def mock_update():
    update = MagicMock()
    update.message = MagicMock()
    update.message.from_user = MagicMock()
    update.message.from_user.id = 12345
    update.message.from_user.full_name = "Admin User"
    update.message.reply = AsyncMock()
    update.message.answer = AsyncMock()
    update.message.chat = MagicMock()
    update.message.chat.type = "private"
    return update


@pytest.fixture
def mock_context():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_rich_message = AsyncMock()
    state = AppState()
    state.admin_ids = [12345]
    state.group_admin_ids = {-1001: [12345], -1002: [12345]}
    state.start_time = time.monotonic()
    state.plugin_effective_map = {
        -1001: {"captcha": True, "spam": True},
        -1002: {"captcha": False, "spam": True, "profile_monitor": False},
    }
    return HandlerContext(bot=bot, state=state, args=[])


def _rich_html(mock_context):
    """Return the html sent via send_rich_message (rich default path)."""
    mock_context.bot.send_rich_message.assert_called_once()
    _, kwargs = mock_context.bot.send_rich_message.call_args
    return kwargs["rich_message"].html


class TestHandleStatus:

    async def test_handle_status_non_private_chat_rejected(self, mock_context):
        """Group chat → handler replies with DM-only error."""
        update = MagicMock()
        update.message = MagicMock()
        update.message.from_user = MagicMock()
        update.message.from_user.id = 12345
        update.message.reply = AsyncMock()
        update.message.chat = MagicMock()
        update.message.chat.type = "group"

        await handle_status(update, mock_context)

        update.message.reply.assert_called_once()
        args, _ = update.message.reply.call_args
        assert "chat pribadi" in args[0]

    async def test_handle_status_non_admin_rejected(self, mock_context):
        """Private chat but caller not admin → handler replies with no-permission."""
        mock_context.state.admin_ids = [99999]
        non_admin_update = MagicMock()
        non_admin_update.message = MagicMock()
        non_admin_update.message.from_user = MagicMock()
        non_admin_update.message.from_user.id = 111
        non_admin_update.message.from_user.full_name = "Bad Actor"
        non_admin_update.message.reply = AsyncMock()
        non_admin_update.message.chat = MagicMock()
        non_admin_update.message.chat.type = "private"

        await handle_status(non_admin_update, mock_context)

        non_admin_update.message.reply.assert_called_once()
        args, _ = non_admin_update.message.reply.call_args
        assert "tidak memiliki izin" in args[0]

    async def test_handle_status_admin_success(
        self, mock_update, mock_context, mock_registry, mock_settings,
    ):
        """Admin in private chat gets the rich status table by default."""
        with (
            patch("bot.handlers.status.get_group_registry", return_value=mock_registry),
            patch("bot.handlers.status.get_settings", return_value=mock_settings),
            patch("bot.handlers.status.get_admin_groups", return_value=[-1001, -1002]),
        ):
            await handle_status(mock_update, mock_context)

        html = _rich_html(mock_context)
        assert "Status Bot" in html
        assert "<table bordered striped>" in html
        assert "Uptime:" in html
        assert "Database:" in html
        assert "Refresh admin:" in html
        assert "Auto-restrict:" in html
        # Markdown reply is only the fallback — not used on the happy path.
        mock_update.message.reply.assert_not_called()

    async def test_handle_status_shows_enforcement_mode(
        self, mock_update, mock_context, mock_registry, mock_settings,
    ):
        """Enforcement mode (Restriksi/Peringatan) appears per group."""
        with (
            patch("bot.handlers.status.get_group_registry", return_value=mock_registry),
            patch("bot.handlers.status.get_settings", return_value=mock_settings),
            patch("bot.handlers.status.get_admin_groups", return_value=[-1001, -1002]),
        ):
            await handle_status(mock_update, mock_context)

        html = _rich_html(mock_context)
        assert "Restriksi" in html or "Peringatan" in html

    async def test_handle_status_shows_per_group_captcha_count(
        self, mock_update, mock_context, mock_registry, mock_settings,
    ):
        """Per-group pending captcha counts appear in the rich table."""
        db = get_database()
        db.add_pending_captcha(
            CaptchaData(
                user_id=111, group_id=-1001,
                chat_id=-1001, message_id=1,
                user_full_name="User1",
            )
        )
        db.add_pending_captcha(
            CaptchaData(
                user_id=222, group_id=-1002,
                chat_id=-1002, message_id=2,
                user_full_name="User2",
            )
        )

        with (
            patch("bot.handlers.status.get_group_registry", return_value=mock_registry),
            patch("bot.handlers.status.get_settings", return_value=mock_settings),
            patch("bot.handlers.status.get_admin_groups", return_value=[-1001, -1002]),
        ):
            await handle_status(mock_update, mock_context)

        html = _rich_html(mock_context)
        assert "Pending" in html
        assert "<td>1</td>" in html

    async def test_handle_status_shows_disabled_plugins(
        self, mock_update, mock_context, mock_registry, mock_settings,
    ):
        """Disabled plugins appear in the per-group table."""
        with (
            patch("bot.handlers.status.get_group_registry", return_value=mock_registry),
            patch("bot.handlers.status.get_settings", return_value=mock_settings),
            patch("bot.handlers.status.get_admin_groups", return_value=[-1001, -1002]),
        ):
            await handle_status(mock_update, mock_context)

        html = _rich_html(mock_context)
        assert "profile_monitor" in html

    async def test_handle_status_shows_last_job_timestamps(
        self, mock_update, mock_context, mock_registry, mock_settings,
    ):
        """Timestamps for last jobs appear in the key-value section."""
        mock_context.state.data["last_admin_refresh"] = time.time() - 60
        mock_context.state.data["last_auto_restrict"] = time.time() - 300

        with (
            patch("bot.handlers.status.get_group_registry", return_value=mock_registry),
            patch("bot.handlers.status.get_settings", return_value=mock_settings),
            patch("bot.handlers.status.get_admin_groups", return_value=[-1001, -1002]),
        ):
            await handle_status(mock_update, mock_context)

        html = _rich_html(mock_context)
        assert "Refresh admin:" in html
        assert "Auto-restrict:" in html
        assert "belum pernah" not in html

    async def test_handle_status_job_timestamps_shown_in_wib(
        self, mock_update, mock_context, mock_registry, mock_settings,
    ):
        """Job timestamps convert from epoch seconds to WIB (+7)."""
        # 1728300000 == 2024-10-07 11:20:00 UTC == 18:20:00 WIB.
        mock_context.state.data["last_admin_refresh"] = 1728300000.0
        mock_context.state.data["last_auto_restrict"] = 1728300000.0

        with (
            patch("bot.handlers.status.get_group_registry", return_value=mock_registry),
            patch("bot.handlers.status.get_settings", return_value=mock_settings),
            patch("bot.handlers.status.get_admin_groups", return_value=[-1001]),
        ):
            await handle_status(mock_update, mock_context)

        html = _rich_html(mock_context)
        assert "2024-10-07 18:20:00 WIB" in html
        assert "UTC" not in html

    async def test_handle_status_no_admin_groups(
        self, mock_update, mock_context, mock_registry, mock_settings,
    ):
        """Admin with no group admin rights sees the empty-groups line."""
        with (
            patch("bot.handlers.status.get_group_registry", return_value=mock_registry),
            patch("bot.handlers.status.get_settings", return_value=mock_settings),
            patch("bot.handlers.status.get_admin_groups", return_value=[]),
        ):
            await handle_status(mock_update, mock_context)

        html = _rich_html(mock_context)
        assert "Tidak ada grup yang dipantau" in html

    async def test_handle_status_scoped_to_admin_groups_only(
        self, mock_update, mock_context, mock_registry, mock_settings,
    ):
        """Only groups where caller is admin are shown."""
        with (
            patch("bot.handlers.status.get_group_registry", return_value=mock_registry),
            patch("bot.handlers.status.get_settings", return_value=mock_settings),
            patch("bot.handlers.status.get_admin_groups", return_value=[-1001]),
        ):
            await handle_status(mock_update, mock_context)

        html = _rich_html(mock_context)
        assert "-1001" in html
        assert "-1002" not in html

    async def test_handle_status_fallback_to_markdown_on_rich_failure(
        self, mock_update, mock_context, mock_registry, mock_settings,
    ):
        """Rich send failure degrades to the original Markdown message."""
        mock_context.bot.send_rich_message.side_effect = Exception("rich unsupported")
        db = get_database()
        db.add_pending_captcha(
            CaptchaData(
                user_id=111, group_id=-1001,
                chat_id=-1001, message_id=1,
                user_full_name="User1",
            )
        )
        mock_context.state.data["last_admin_refresh"] = time.time() - 60

        with (
            patch("bot.handlers.status.get_group_registry", return_value=mock_registry),
            patch("bot.handlers.status.get_settings", return_value=mock_settings),
            patch("bot.handlers.status.get_admin_groups", return_value=[-1001, -1002]),
        ):
            await handle_status(mock_update, mock_context)

        mock_update.message.reply.assert_called_once()
        args, kwargs = mock_update.message.reply.call_args
        text = args[0]
        assert kwargs.get("parse_mode") == "Markdown"
        assert "*Uptime:*" in text
        assert "*Grup yang kamu admin:*" in text
        assert "Captcha: 1" in text
        assert "Plugin nonaktif" in text
        assert "Refresh admin:" in text
        assert "Auto-restrict:" in text
        assert "belum pernah" in text  # auto-restrict never ran

    async def test_handle_status_fallback_empty_groups(
        self, mock_update, mock_context, mock_registry, mock_settings,
    ):
        """Fallback path renders the empty-groups line too."""
        mock_context.bot.send_rich_message.side_effect = Exception("rich unsupported")

        with (
            patch("bot.handlers.status.get_group_registry", return_value=mock_registry),
            patch("bot.handlers.status.get_settings", return_value=mock_settings),
            patch("bot.handlers.status.get_admin_groups", return_value=[]),
        ):
            await handle_status(mock_update, mock_context)

        args, _ = mock_update.message.reply.call_args
        assert "Tidak ada grup yang dipantau" in args[0]

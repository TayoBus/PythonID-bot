"""Tests for guest bot message moderation."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Chat, Message, User

from bot.dispatch import AppState, HandlerContext, StopPropagation
from bot.group_config import GroupConfig
from bot.handlers.guest_bot import (
    handle_guest_bot_message,
    is_guest_bot_message,
    is_guest_bot_whitelisted,
)


@pytest.fixture
def mock_user():
    return User(id=123, first_name="Test", is_bot=False, username="testuser")


def _make_guest_message(*, caller_user=None, caller_chat=None, username="guestbot"):
    """Build a real aiogram Message carrying Guest Mode caller fields."""
    bot_user = User(id=999, is_bot=True, first_name="Guest Bot", username=username)
    chat = Chat(id=-1001234567890, type="supergroup", title="Test")
    return Message(
        message_id=1,
        date=datetime.now(),
        chat=chat,
        from_user=bot_user,
        text="guest message",
        guest_bot_caller_user=caller_user,
        guest_bot_caller_chat=caller_chat,
    )


@pytest.fixture
def mock_group_config():
    return GroupConfig(
        group_id=-1001234567890,
        warning_topic_id=123,
        warning_threshold=3,
        guest_bot_whitelist=["allowedbot"],
    )


@pytest.fixture
def mock_update(mock_user):
    update = MagicMock()
    update.edited_message = None
    update.message = _make_guest_message(caller_user=mock_user)
    return update


@pytest.fixture
def mock_delete():
    """Patch Message.delete (aiogram models are frozen) for the test."""
    with patch.object(Message, "delete", new_callable=AsyncMock) as delete_mock:
        yield delete_mock


@pytest.fixture
def mock_context():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.restrict_chat_member = AsyncMock()
    state = AppState()
    state.group_admin_ids = {}
    state.trusted_user_ids = set()
    return HandlerContext(bot=bot, state=state, args=[])


class TestIsGuestBotMessage:
    def test_user_caller(self, mock_user):
        message = _make_guest_message(caller_user=mock_user)
        assert is_guest_bot_message(message) is True

    def test_chat_caller(self):
        caller_chat = Chat(id=-1009, type="channel", title="Channel")
        message = _make_guest_message(caller_chat=caller_chat)
        assert is_guest_bot_message(message) is True

    def test_regular_message(self):
        message = _make_guest_message()
        assert is_guest_bot_message(message) is False


class TestIsGuestBotWhitelisted:
    @pytest.mark.parametrize(
        ("username", "whitelist", "expected"),
        [
            ("allowedbot", ["allowedbot"], True),
            ("otherbot", ["allowedbot"], False),
            ("AllowedBot", ["allowedbot"], True),
            (None, ["allowedbot"], False),
        ],
    )
    def test_whitelist(self, username, whitelist, expected):
        message = _make_guest_message(username=username)
        assert is_guest_bot_whitelisted(message, whitelist) is expected


class TestHandleGuestBotMessage:
    async def test_non_guest_message_does_nothing(self, mock_update, mock_context, mock_group_config, mock_delete):
        mock_update.message = _make_guest_message()
        with patch("bot.handlers.guest_bot.get_group_config_for_update", return_value=mock_group_config):
            await handle_guest_bot_message(mock_update, mock_context)
        mock_delete.assert_not_awaited()

    async def test_whitelisted_message_does_nothing(self, mock_update, mock_context, mock_group_config, mock_delete):
        mock_update.message = _make_guest_message(
            caller_user=User(id=123, is_bot=False, first_name="Test", username="testuser"),
            username="allowedbot",
        )
        with patch("bot.handlers.guest_bot.get_group_config_for_update", return_value=mock_group_config):
            await handle_guest_bot_message(mock_update, mock_context)
        mock_delete.assert_not_awaited()

    async def test_unmonitored_group_returns(self, mock_update, mock_context, mock_delete):
        with patch("bot.handlers.guest_bot.get_group_config_for_update", return_value=None):
            await handle_guest_bot_message(mock_update, mock_context)
        mock_delete.assert_not_awaited()

    async def test_admin_is_deleted_but_not_restricted(self, mock_update, mock_context, mock_group_config, mock_delete):
        with (
            patch("bot.handlers.guest_bot.get_group_config_for_update", return_value=mock_group_config),
            patch("bot.handlers.guest_bot.is_user_admin_or_trusted", return_value=True),
            patch("bot.handlers.guest_bot.get_database") as get_db,
            pytest.raises(StopPropagation),
        ):
            await handle_guest_bot_message(mock_update, mock_context)
        mock_delete.assert_awaited_once()
        get_db.assert_not_called()

    @pytest.mark.parametrize(("count", "sends", "increments"), [(1, 1, True), (2, 0, True)])
    async def test_pre_threshold_violation(
        self, mock_update, mock_context, mock_group_config, mock_delete, count, sends, increments
    ):
        db = MagicMock()
        db.is_user_restricted_by_bot.return_value = False
        db.get_or_create_user_warning.return_value.message_count = count
        with (
            patch("bot.handlers.guest_bot.get_group_config_for_update", return_value=mock_group_config),
            patch("bot.handlers.guest_bot.is_user_admin_or_trusted", return_value=False),
            patch("bot.handlers.guest_bot.get_database", return_value=db),
            pytest.raises(StopPropagation),
        ):
            await handle_guest_bot_message(mock_update, mock_context)
        assert mock_context.bot.send_message.await_count == sends
        assert db.increment_message_count.called is increments

    async def test_threshold_restricts_and_notifies(self, mock_update, mock_context, mock_group_config, mock_delete):
        db = MagicMock()
        db.is_user_restricted_by_bot.return_value = False
        db.get_or_create_user_warning.return_value.message_count = 3
        with (
            patch("bot.handlers.guest_bot.get_group_config_for_update", return_value=mock_group_config),
            patch("bot.handlers.guest_bot.is_user_admin_or_trusted", return_value=False),
            patch("bot.handlers.guest_bot.get_database", return_value=db),
            pytest.raises(StopPropagation),
        ):
            await handle_guest_bot_message(mock_update, mock_context)
        mock_context.bot.restrict_chat_member.assert_awaited_once()
        mock_context.bot.send_message.assert_awaited_once()
        db.mark_user_restricted.assert_called_once_with(
            123, mock_group_config.group_id, warning_kind="guest_bot"
        )

    async def test_threshold_one_restricts_without_warning(self, mock_update, mock_context, mock_group_config, mock_delete):
        """When warning_threshold==1, first violation restricts without sending a separate warning."""
        mock_group_config.warning_threshold = 1
        db = MagicMock()
        db.is_user_restricted_by_bot.return_value = False
        db.get_or_create_user_warning.return_value.message_count = 1
        with (
            patch("bot.handlers.guest_bot.get_group_config_for_update", return_value=mock_group_config),
            patch("bot.handlers.guest_bot.is_user_admin_or_trusted", return_value=False),
            patch("bot.handlers.guest_bot.get_database", return_value=db),
            pytest.raises(StopPropagation),
        ):
            await handle_guest_bot_message(mock_update, mock_context)
        mock_context.bot.restrict_chat_member.assert_awaited_once()
        db.mark_user_restricted.assert_called_once_with(
            123, mock_group_config.group_id, warning_kind="guest_bot"
        )
        db.increment_message_count.assert_not_called()

    async def test_chat_caller_is_deleted_only(self, mock_update, mock_context, mock_group_config, mock_delete):
        mock_update.message = _make_guest_message(
            caller_chat=Chat(id=-1009, type="channel", title="Channel"),
        )
        with (
            patch("bot.handlers.guest_bot.get_group_config_for_update", return_value=mock_group_config),
            patch("bot.handlers.guest_bot.get_database") as get_db,
            pytest.raises(StopPropagation),
        ):
            await handle_guest_bot_message(mock_update, mock_context)
        mock_delete.assert_awaited_once()
        get_db.assert_not_called()

    async def test_delete_failure_continues(self, mock_update, mock_context, mock_group_config, mock_delete):
        mock_delete.side_effect = TelegramBadRequest(
            method=MagicMock(), message="delete failed"
        )
        db = MagicMock()
        db.is_user_restricted_by_bot.return_value = False
        db.get_or_create_user_warning.return_value.message_count = 2
        with (
            patch("bot.handlers.guest_bot.get_group_config_for_update", return_value=mock_group_config),
            patch("bot.handlers.guest_bot.is_user_admin_or_trusted", return_value=False),
            patch("bot.handlers.guest_bot.get_database", return_value=db),
            pytest.raises(StopPropagation),
        ):
            await handle_guest_bot_message(mock_update, mock_context)
        db.increment_message_count.assert_called_once()

    async def test_already_restricted_skips_warning(self, mock_update, mock_context, mock_group_config, mock_delete):
        db = MagicMock()
        db.is_user_restricted_by_bot.return_value = True
        with (
            patch("bot.handlers.guest_bot.get_group_config_for_update", return_value=mock_group_config),
            patch("bot.handlers.guest_bot.is_user_admin_or_trusted", return_value=False),
            patch("bot.handlers.guest_bot.get_database", return_value=db),
            pytest.raises(StopPropagation),
        ):
            await handle_guest_bot_message(mock_update, mock_context)
        mock_delete.assert_awaited_once()
        db.get_or_create_user_warning.assert_not_called()
        db.increment_message_count.assert_not_called()
        db.mark_user_restricted.assert_not_called()

    async def test_ignores_edited_message(self, mock_update, mock_context, mock_group_config):
        """Regression: an edit of an already-handled guest message must not
        re-delete it or count a second strike against the caller."""
        mock_update.edited_message = mock_update.message
        mock_update.message = None
        db = MagicMock()
        with (
            patch("bot.handlers.guest_bot.get_group_config_for_update", return_value=mock_group_config),
            patch("bot.handlers.guest_bot.get_database", return_value=db),
        ):
            await handle_guest_bot_message(mock_update, mock_context)
        assert mock_update.message is None
        db.get_or_create_user_warning.assert_not_called()
        db.increment_message_count.assert_not_called()
        db.mark_user_restricted.assert_not_called()

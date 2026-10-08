"""Tests for the group-ordered dispatch loop (bot/dispatch.py).

Covers the PTB semantics the anti-spam pipeline depends on:
- handlers run in ascending group order
- within a group, only the first matching handler runs
- StopPropagation halts all remaining groups
- other exceptions go to the error handler and the next in-group handler is tried
- update kinds filter per spec
- command args parsing for command specs
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberUpdated,
    ChatMemberLeft,
    ChatMemberMember,
    Message,
    MessageEntity,
    Update,
    User,
)

from bot.dispatch import (
    AppState,
    HandlerContext,
    HandlerSpec,
    StopPropagation,
    callback_data_pattern,
    command_filter,
    dispatch_update,
    effective_chat,
    effective_message,
    effective_user,
    handle_bot_error,
    has_contact,
    has_new_chat_members,
    has_text,
    is_callback_query,
    is_chat_member_update,
    is_command_message,
    is_forwarded,
    is_group_chat,
    is_message_or_edited,
    is_private_chat,
    parse_command_args,
    update_kind,
)


def _make_user(user_id=1):
    return User(id=user_id, is_bot=False, first_name="Test")


def _make_chat(chat_id=-100, chat_type="supergroup"):
    return Chat(id=chat_id, type=chat_type, title="Test")


def _make_message(text="hi", chat=None, user=None, **kwargs):
    return Message(
        message_id=1,
        date=datetime.now(),
        chat=chat or _make_chat(),
        from_user=user or _make_user(),
        text=text,
        **kwargs,
    )


def _make_update(message=None, **kwargs):
    return Update(update_id=1, message=message or _make_message(), **kwargs)


def _cb(name, calls, stop=False, boom=False):
    async def _callback(update, context):
        calls.append(name)
        if stop:
            raise StopPropagation()
        if boom:
            raise RuntimeError("boom")
    return _callback


def _yes(update):
    return True


def _no(update):
    return False


def _spec(name, group, kinds, check, callback, **kwargs):
    return HandlerSpec(
        plugin_name=name, group=group, update_kinds=kinds,
        check=check, callback=callback, label=name, **kwargs,
    )


@pytest.fixture
def bot():
    return MagicMock()


class TestDispatchOrder:
    async def test_groups_run_in_ascending_order(self, bot):
        """Handlers run group -1, 0, 1, ... in order regardless of registration order."""
        calls = []
        state = AppState()
        state.plugin_handlers = {
            "high": {"handler_group": 5, "handlers": [_spec("high", 5, ("message",), _yes, _cb("high", calls))]},
            "low": {"handler_group": -1, "handlers": [_spec("low", -1, ("message",), _yes, _cb("low", calls))]},
            "mid": {"handler_group": 0, "handlers": [_spec("mid", 0, ("message",), _yes, _cb("mid", calls))]},
        }
        await dispatch_update(_make_update(), bot, state)
        assert calls == ["low", "mid", "high"]

    async def test_first_match_wins_within_group(self, bot):
        """Only the first matching handler in a group runs."""
        calls = []
        state = AppState()
        state.plugin_handlers = {
            "p": {"handler_group": 0, "handlers": [
                _spec("p", 0, ("message",), _no, _cb("first", calls)),
                _spec("p", 0, ("message",), _yes, _cb("second", calls)),
                _spec("p", 0, ("message",), _yes, _cb("third", calls)),
            ]},
        }
        await dispatch_update(_make_update(), bot, state)
        assert calls == ["second"]

    async def test_no_match_in_group_continues_to_next_group(self, bot):
        """A group with no matching handler does not block later groups."""
        calls = []
        state = AppState()
        state.plugin_handlers = {
            "p": {"handler_group": 0, "handlers": [_spec("p", 0, ("message",), _no, _cb("g0", calls))]},
            "q": {"handler_group": 1, "handlers": [_spec("q", 1, ("message",), _yes, _cb("g1", calls))]},
        }
        await dispatch_update(_make_update(), bot, state)
        assert calls == ["g1"]

    async def test_stop_propagation_halts_all_remaining_groups(self, bot):
        """StopPropagation prevents every later group from running."""
        calls = []
        state = AppState()
        state.plugin_handlers = {
            "p": {"handler_group": 0, "handlers": [_spec("p", 0, ("message",), _yes, _cb("g0", calls, stop=True))]},
            "q": {"handler_group": 1, "handlers": [_spec("q", 1, ("message",), _yes, _cb("g1", calls))]},
            "r": {"handler_group": 2, "handlers": [_spec("r", 2, ("message",), _yes, _cb("g2", calls))]},
        }
        await dispatch_update(_make_update(), bot, state)
        assert calls == ["g0"]

    async def test_stop_propagation_from_later_group(self, bot):
        """StopPropagation raised in group 1 still halts group 2+."""
        calls = []
        state = AppState()
        state.plugin_handlers = {
            "p": {"handler_group": 0, "handlers": [_spec("p", 0, ("message",), _yes, _cb("g0", calls))]},
            "q": {"handler_group": 1, "handlers": [_spec("q", 1, ("message",), _yes, _cb("g1", calls, stop=True))]},
            "r": {"handler_group": 2, "handlers": [_spec("r", 2, ("message",), _yes, _cb("g2", calls))]},
        }
        await dispatch_update(_make_update(), bot, state)
        assert calls == ["g0", "g1"]

    async def test_handler_exception_goes_to_error_handler_and_continues(self, bot):
        """A failing handler routes to the error handler; the next in-group handler is tried."""
        calls = []
        seen = []

        async def fake_error(update, exc):
            seen.append(exc)

        state = AppState()
        state.plugin_handlers = {
            "p": {"handler_group": 0, "handlers": [
                _spec("p", 0, ("message",), _yes, _cb("boom", calls, boom=True)),
                _spec("p", 0, ("message",), _yes, _cb("next", calls)),
            ]},
            "q": {"handler_group": 1, "handlers": [_spec("q", 1, ("message",), _yes, _cb("g1", calls))]},
        }
        with patch("bot.dispatch.handle_bot_error", side_effect=fake_error):
            await dispatch_update(_make_update(), bot, state)
        assert calls == ["boom", "next", "g1"]
        assert len(seen) == 1
        assert isinstance(seen[0], RuntimeError)

    async def test_filter_exception_skips_handler(self, bot):
        """A raising filter is treated as non-match, not a dispatch failure."""
        calls = []

        def bad_filter(update):
            raise ValueError("filter boom")

        state = AppState()
        state.plugin_handlers = {
            "p": {"handler_group": 0, "handlers": [
                _spec("p", 0, ("message",), bad_filter, _cb("bad", calls)),
                _spec("p", 0, ("message",), _yes, _cb("good", calls)),
            ]},
        }
        await dispatch_update(_make_update(), bot, state)
        assert calls == ["good"]

    async def test_update_kind_filters_specs(self, bot):
        """A message update does not trigger callback_query-only specs."""
        calls = []
        state = AppState()
        state.plugin_handlers = {
            "p": {"handler_group": 0, "handlers": [
                _spec("p", 0, ("callback_query",), _yes, _cb("cb", calls)),
                _spec("p", 0, ("message",), _yes, _cb("msg", calls)),
            ]},
        }
        await dispatch_update(_make_update(), bot, state)
        assert calls == ["msg"]

    async def test_callback_query_dispatch(self, bot):
        """Callback query updates reach callback_query specs."""
        calls = []
        user = _make_user()
        message = _make_message(chat=_make_chat(), user=user)
        query = CallbackQuery(id="1", from_user=user, chat_instance="x", data="d", message=message)
        update = Update(update_id=2, callback_query=query)
        state = AppState()
        state.plugin_handlers = {
            "p": {"handler_group": 0, "handlers": [
                _spec("p", 0, ("callback_query",), _yes, _cb("cb", calls)),
            ]},
        }
        await dispatch_update(update, bot, state)
        assert calls == ["cb"]

    async def test_chat_member_dispatch(self, bot):
        """chat_member updates reach chat_member specs."""
        calls = []
        user = _make_user()
        chat = _make_chat()
        cm = ChatMemberUpdated(
            chat=chat, from_user=user, date=datetime.now(),
            old_chat_member=ChatMemberLeft(user=user, status="left"),
            new_chat_member=ChatMemberMember(user=user, status="member"),
        )
        update = Update(update_id=3, chat_member=cm)
        state = AppState()
        state.plugin_handlers = {
            "p": {"handler_group": 0, "handlers": [
                _spec("p", 0, ("chat_member",), _yes, _cb("cm", calls)),
            ]},
        }
        await dispatch_update(update, bot, state)
        assert calls == ["cm"]

    async def test_unknown_update_kind_ignored(self, bot):
        """Updates without a handled kind are ignored silently."""
        calls = []
        state = AppState()
        state.plugin_handlers = {
            "p": {"handler_group": 0, "handlers": [_spec("p", 0, ("message",), _yes, _cb("x", calls))]},
        }
        await dispatch_update(Update(update_id=9), bot, state)
        assert calls == []

    async def test_command_args_parsed_for_command_specs(self, bot):
        """context.args is parsed from the message text for command specs."""
        seen = {}

        async def _callback(update, context):
            seen["args"] = context.args

        message = _make_message(
            text="/warn 123 spamming",
            entities=[MessageEntity(type="bot_command", offset=0, length=5)],
        )
        state = AppState()
        state.bot_username = "testbot"
        state.plugin_handlers = {
            "w": {"handler_group": 0, "handlers": [
                _spec("w", 0, ("message",), _yes, _callback, command="warn"),
            ]},
        }
        await dispatch_update(Update(update_id=4, message=message), bot, state)
        assert seen["args"] == ["123", "spamming"]

    async def test_context_carries_bot_and_state(self, bot):
        """HandlerContext exposes the bot and state it was built with."""
        seen = {}

        async def _callback(update, context):
            seen["bot"] = context.bot
            seen["state"] = context.state
            seen["args"] = context.args

        state = AppState()
        state.plugin_handlers = {
            "p": {"handler_group": 0, "handlers": [_spec("p", 0, ("message",), _yes, _callback)]},
        }
        await dispatch_update(_make_update(), bot, state)
        assert seen["bot"] is bot
        assert seen["state"] is state
        assert seen["args"] is None


class TestCommandFilter:
    def test_matches_plain_command(self):
        state = AppState()
        state.bot_username = "testbot"
        f = command_filter("warn", state)
        update = _make_update(_make_message(
            text="/warn 123",
            entities=[MessageEntity(type="bot_command", offset=0, length=5)],
        ))
        assert f(update) is True

    def test_matches_command_with_own_bot_username(self):
        state = AppState()
        state.bot_username = "testbot"
        f = command_filter("warn", state)
        update = _make_update(_make_message(
            text="/warn@testbot 123",
            entities=[MessageEntity(type="bot_command", offset=0, length=13)],
        ))
        assert f(update) is True

    def test_rejects_command_for_other_bot(self):
        state = AppState()
        state.bot_username = "testbot"
        f = command_filter("warn", state)
        update = _make_update(_make_message(
            text="/warn@otherbot 123",
            entities=[MessageEntity(type="bot_command", offset=0, length=14)],
        ))
        assert f(update) is False

    def test_rejects_different_command(self):
        state = AppState()
        state.bot_username = "testbot"
        f = command_filter("warn", state)
        update = _make_update(_make_message(
            text="/ban 123",
            entities=[MessageEntity(type="bot_command", offset=0, length=4)],
        ))
        assert f(update) is False

    def test_rejects_plain_text(self):
        state = AppState()
        state.bot_username = "testbot"
        f = command_filter("warn", state)
        assert f(_make_update(_make_message(text="hello"))) is False

    def test_username_match_is_case_insensitive(self):
        state = AppState()
        state.bot_username = "TestBot"
        f = command_filter("warn", state)
        update = _make_update(_make_message(
            text="/warn@testbot",
            entities=[MessageEntity(type="bot_command", offset=0, length=13)],
        ))
        assert f(update) is True


class TestCallbackDataPattern:
    def test_matches_pattern(self):
        f = callback_data_pattern(r"^verify:-?\d+:\d+$")
        user = _make_user()
        message = _make_message()
        query = CallbackQuery(id="1", from_user=user, chat_instance="x",
                              data="verify:-100:123", message=message)
        assert f(Update(update_id=1, callback_query=query)) is True

    def test_rejects_non_matching(self):
        f = callback_data_pattern(r"^verify:-?\d+:\d+$")
        user = _make_user()
        message = _make_message()
        query = CallbackQuery(id="1", from_user=user, chat_instance="x",
                              data="unverify:-100:123", message=message)
        assert f(Update(update_id=1, callback_query=query)) is False

    def test_no_callback_query_no_match(self):
        f = callback_data_pattern(r"^verify:")
        assert f(_make_update()) is False


class TestEffectiveHelpers:
    def test_effective_message_prefers_message(self):
        update = _make_update()
        assert effective_message(update) is update.message

    def test_effective_chat_from_message(self):
        update = _make_update()
        assert effective_chat(update) is update.message.chat

    def test_effective_chat_from_callback_query(self):
        user = _make_user()
        message = _make_message()
        query = CallbackQuery(id="1", from_user=user, chat_instance="x", data="d", message=message)
        update = Update(update_id=1, callback_query=query)
        assert effective_chat(update) is message.chat

    def test_effective_chat_from_chat_member(self):
        user = _make_user()
        chat = _make_chat()
        cm = ChatMemberUpdated(
            chat=chat, from_user=user, date=datetime.now(),
            old_chat_member=ChatMemberLeft(user=user, status="left"),
            new_chat_member=ChatMemberMember(user=user, status="member"),
        )
        update = Update(update_id=1, chat_member=cm)
        assert effective_chat(update) is chat

    def test_effective_user_from_message(self):
        update = _make_update()
        assert effective_user(update) is update.message.from_user

    def test_effective_user_from_callback_query(self):
        user = _make_user()
        message = _make_message()
        query = CallbackQuery(id="1", from_user=user, chat_instance="x", data="d", message=message)
        update = Update(update_id=1, callback_query=query)
        assert effective_user(update) is user

    def test_update_kind(self):
        assert update_kind(_make_update()) == "message"
        user = _make_user()
        message = _make_message()
        query = CallbackQuery(id="1", from_user=user, chat_instance="x", data="d", message=message)
        assert update_kind(Update(update_id=1, callback_query=query)) == "callback_query"
        assert update_kind(Update(update_id=1)) is None

    def test_is_group_chat(self):
        assert is_group_chat(_make_update()) is True
        private = _make_update(_make_message(chat=_make_chat(chat_type="private")))
        assert is_group_chat(private) is False

    def test_is_command_message(self):
        cmd = _make_update(_make_message(
            text="/warn", entities=[MessageEntity(type="bot_command", offset=0, length=5)]))
        assert is_command_message(cmd) is True
        assert is_command_message(_make_update()) is False

    def test_parse_command_args(self):
        assert parse_command_args(_make_message(text="/warn 123 spamming")) == ["123", "spamming"]
        assert parse_command_args(_make_message(text="/warn")) == []


class TestHandleBotError:
    async def test_network_error_logged_as_warning(self):
        with patch("bot.dispatch.logger") as mock_logger:
            await handle_bot_error(
                _make_update(),
                TelegramNetworkError(method=MagicMock(), message="timeout"),
            )
            mock_logger.warning.assert_called_once()
            mock_logger.error.assert_not_called()

    async def test_other_error_logged_as_error_with_exc_info(self):
        exc = RuntimeError("boom")
        with patch("bot.dispatch.logger") as mock_logger:
            await handle_bot_error(_make_update(), exc)
            mock_logger.error.assert_called_once()
            assert mock_logger.error.call_args.kwargs.get("exc_info") is exc

    async def test_none_update_accepted(self):
        with patch("bot.dispatch.logger") as mock_logger:
            await handle_bot_error(None, RuntimeError("boom"))
            mock_logger.error.assert_called_once()


class TestHandlerContext:
    def test_bot_data_view_reads_state_fields(self):
        state = AppState()
        state.admin_ids = [1, 2]
        state.group_admin_ids = {-100: [1]}
        ctx = HandlerContext(bot=MagicMock(), state=state)
        assert ctx.bot_data["admin_ids"] == [1, 2]
        assert ctx.bot_data["group_admin_ids"] == {-100: [1]}
        assert ctx.bot_data.get("plugin_effective_map") == {}
        assert ctx.bot_data.get("start_time") is None

    def test_bot_data_view_writes_through_to_state(self):
        state = AppState()
        ctx = HandlerContext(bot=MagicMock(), state=state)
        ctx.bot_data["admin_ids"] = [9]
        assert state.admin_ids == [9]
        ctx.bot_data["custom_key"] = "x"
        assert state.data["custom_key"] == "x"
        assert ctx.bot_data["custom_key"] == "x"

    def test_bot_data_view_setdefault(self):
        state = AppState()
        ctx = HandlerContext(bot=MagicMock(), state=state)
        result = ctx.bot_data.setdefault("my_deque", {})
        assert result == {}
        assert state.data["my_deque"] == {}

    def test_create_task_schedules_coroutine(self):
        async def _main():
            state = AppState()
            ctx = HandlerContext(bot=MagicMock(), state=state)
            done = []

            async def _work():
                done.append(True)

            task = ctx.create_task(_work())
            await task
            assert done == [True]

        import asyncio
        asyncio.run(_main())

    def test_create_task_logs_exception(self):
        async def _main():
            state = AppState()
            ctx = HandlerContext(bot=MagicMock(), state=state)

            async def _boom():
                raise RuntimeError("bg boom")

            with patch("bot.dispatch.logger") as mock_logger:
                task = ctx.create_task(_boom())
                try:
                    await task
                except RuntimeError:
                    pass
                assert mock_logger.error.called

        import asyncio
        asyncio.run(_main())


class TestFilterPredicates:
    def test_is_message_or_edited(self):
        assert is_message_or_edited(_make_update()) is True
        edited = Update(update_id=2, edited_message=_make_message())
        assert is_message_or_edited(edited) is True
        assert is_message_or_edited(Update(update_id=3)) is False

    def test_is_callback_query(self):
        user = _make_user()
        message = _make_message()
        query = CallbackQuery(id="1", from_user=user, chat_instance="x",
                              data="d", message=message)
        assert is_callback_query(Update(update_id=1, callback_query=query)) is True
        nodata = CallbackQuery(id="2", from_user=user, chat_instance="x", message=message)
        assert is_callback_query(Update(update_id=2, callback_query=nodata)) is False
        assert is_callback_query(_make_update()) is False

    def test_is_chat_member_update(self):
        user = _make_user()
        chat = _make_chat()
        cm = ChatMemberUpdated(
            chat=chat, from_user=user, date=datetime.now(),
            old_chat_member=ChatMemberLeft(user=user, status="left"),
            new_chat_member=ChatMemberMember(user=user, status="member"),
        )
        assert is_chat_member_update(Update(update_id=1, chat_member=cm)) is True
        assert is_chat_member_update(_make_update()) is False

    def test_is_private_chat(self):
        private = _make_update(_make_message(chat=_make_chat(chat_type="private")))
        assert is_private_chat(private) is True
        assert is_private_chat(_make_update()) is False
        assert is_private_chat(Update(update_id=9)) is False

    def test_has_text(self):
        assert has_text(_make_update()) is True
        assert has_text(_make_update(_make_message(text=None))) is False
        assert has_text(Update(update_id=9)) is False

    def test_has_contact(self):
        from aiogram.types import Contact
        msg = _make_message()
        assert has_contact(_make_update(msg)) is False
        msg_with_contact = Message(
            message_id=2, date=datetime.now(), chat=_make_chat(),
            from_user=_make_user(),
            contact=Contact(phone_number="+1", first_name="T", user_id=1),
        )
        assert has_contact(_make_update(msg_with_contact)) is True

    def test_is_forwarded(self):
        assert is_forwarded(_make_update()) is False
        assert is_forwarded(Update(update_id=9)) is False

    def test_has_new_chat_members(self):
        msg = _make_message()
        assert has_new_chat_members(_make_update(msg)) is False
        msg_with_members = Message(
            message_id=3, date=datetime.now(), chat=_make_chat(),
            from_user=_make_user(), new_chat_members=[_make_user(2)],
        )
        assert has_new_chat_members(_make_update(msg_with_members)) is True


class TestStateViewExtras:
    def test_delitem_field_resets_to_none(self):
        state = AppState()
        state.admin_ids = [1]
        ctx = HandlerContext(bot=MagicMock(), state=state)
        del ctx.bot_data["admin_ids"]
        assert state.admin_ids is None

    def test_delitem_custom_key(self):
        state = AppState()
        ctx = HandlerContext(bot=MagicMock(), state=state)
        ctx.bot_data["custom"] = 1
        del ctx.bot_data["custom"]
        assert "custom" not in state.data

    def test_iter_and_len(self):
        state = AppState()
        state.admin_ids = [1]
        ctx = HandlerContext(bot=MagicMock(), state=state)
        ctx.bot_data["custom"] = 2
        keys = set(ctx.bot_data)
        assert "admin_ids" in keys
        assert "custom" in keys
        assert len(ctx.bot_data) == len(keys)

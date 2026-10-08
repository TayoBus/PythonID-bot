"""Regression tests for the aiogram dispatcher wiring in bot.main.

Background: the aiogram migration initially registered the single update
entrypoint with ``@dp.update()``. That handler never ran — ``Dispatcher``
registers its own internal ``_listen_update`` on ``dp.update`` first and the
observer stops at the first matching handler — so the bot was deaf from the
deploy moment (every update logged "not handled"). Additionally the shared
state kwarg was named ``state``, which aiogram's ``FSMContextMiddleware``
shadows with its own ``FSMContext`` on update handlers.

These tests wire a real ``Dispatcher`` exactly like production
(``build_dispatcher()`` + ``feed_update``) and assert the entrypoint is
reached with the real ``AppState``.
"""

from datetime import UTC, datetime
from unittest.mock import patch

from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User

import bot.main as main_module
from bot.dispatch import AppState
from bot.main import build_dispatcher


def _make_bot() -> Bot:
    return Bot(token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")


def _make_message_update(update_id: int) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=-1001234567890, type="supergroup"),
            from_user=User(id=111, is_bot=False, first_name="Uji"),
            text="/status",
        ),
    )


def _make_callback_update(update_id: int) -> Update:
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id="cb1",
            from_user=User(id=111, is_bot=False, first_name="Uji"),
            chat_instance="ci1",
            data="verify:1:2",
        ),
    )


async def test_message_update_reaches_dispatch_with_app_state():
    bot = _make_bot()
    try:
        app_state = AppState(bot=bot)
        dp = build_dispatcher()
        update = _make_message_update(update_id=4242)

        calls: list = []
        with patch.object(main_module, "dispatch_update") as mock_dispatch:
            mock_dispatch.side_effect = lambda u, b, s: calls.append((u, b, s))
            await dp.feed_update(bot, update, app_state=app_state)

        assert len(calls) == 1
        received_update, received_bot, received_state = calls[0]
        assert received_update.update_id == 4242
        assert received_bot is bot
        # Must be our AppState, not aiogram's FSMContext injected as `state`.
        assert received_state is app_state
    finally:
        await bot.session.close()


async def test_callback_query_update_reaches_dispatch():
    bot = _make_bot()
    try:
        app_state = AppState(bot=bot)
        dp = build_dispatcher()
        update = _make_callback_update(update_id=777)

        calls: list = []
        with patch.object(main_module, "dispatch_update") as mock_dispatch:
            mock_dispatch.side_effect = lambda u, b, s: calls.append((u, b, s))
            await dp.feed_update(bot, update, app_state=app_state)

        assert len(calls) == 1
        assert calls[0][0].update_id == 777
        assert calls[0][2] is app_state
    finally:
        await bot.session.close()


async def test_build_dispatcher_registers_entrypoint_on_sub_observers():
    """The entrypoint must live on the concrete observers, never dp.update."""
    dp = build_dispatcher()
    # dp.update keeps only aiogram's internal _listen_update.
    assert len(dp.update.handlers) == 1
    assert len(dp.message.handlers) == 1
    assert len(dp.edited_message.handlers) == 1
    assert len(dp.callback_query.handlers) == 1
    assert len(dp.chat_member.handlers) == 1

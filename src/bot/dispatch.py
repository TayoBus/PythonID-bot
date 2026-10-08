"""Group-ordered update dispatcher for the aiogram migration.

This module preserves the PTB handler-group semantics that the
anti-spam pipeline depends on (correctness-critical):

- Handlers run in ascending group order (-1, 0..7).
- Within a group, only the FIRST matching handler runs.
- A handler raising :class:`StopPropagation` (the replacement for PTB's
  ``ApplicationHandlerStop``) halts all remaining groups.
- Any other exception is routed to the error handler and processing
  continues with the next handler in the same group (matching PTB's
  ``dispatch_error`` behavior).

aiogram's Router pipeline provides neither first-match-wins nor
cross-group stop semantics, so dispatch is a small manual loop driven
by :class:`HandlerSpec` registrations collected from the plugin system
(``MANIFEST_ORDER`` in ``bot.plugins.definitions`` remains the single
source of truth for group order).

Shared runtime state lives in :class:`AppState`, an explicit object
injected into handlers via ``dp.start_polling(..., state=state)`` --
the replacement for PTB's ``application.bot_data``. Handlers receive a
:class:`HandlerContext` carrying the aiogram ``Bot``, the ``AppState``,
and parsed command ``args``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Iterator

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)


class StopPropagation(Exception):
    """Stop processing the current update entirely.

    Replacement for PTB's ``ApplicationHandlerStop``. Raised by a
    handler after it has handled an update to prevent all remaining
    handler groups from running (e.g. topic_guard, guest_bot_block,
    spam enforcers, duplicate captcha callbacks).
    """


@dataclass
class AppState:
    """Explicit shared runtime state (replaces PTB ``application.bot_data``).

    Injected into the aiogram dispatcher via
    ``dp.start_polling(bot, state=state)`` and handed to every handler
    inside :class:`HandlerContext`.
    """

    bot: Bot | None = None
    """aiogram Bot instance (set in main before polling starts)."""

    scheduler: AsyncIOScheduler | None = None
    """APScheduler scheduler for repeating jobs and one-shot timeouts."""

    bot_username: str | None = None
    """Bot username without @, resolved at startup for command matching."""

    plugin_handlers: dict[str, dict] = field(default_factory=dict)
    """Plugin name -> {"handler_group": int, "handlers": list[HandlerSpec]}."""

    plugin_effective_map: dict[int, dict[str, bool]] = field(default_factory=dict)
    """Per-group plugin toggle map for runtime gating (guard_plugin)."""

    group_admin_ids: dict[int, list[int]] = field(default_factory=dict)
    """Per-group cached human admin IDs."""

    admin_ids: list[int] = field(default_factory=list)
    """Union of all cached admin IDs."""

    trusted_user_ids: set[int] | None = None
    """Cached trusted user IDs; lazily loaded from DB when None."""

    start_time: float | None = None
    """monotonic() timestamp recorded at startup for /status uptime."""

    data: dict[str, Any] = field(default_factory=dict)
    """Misc dynamic state (duplicate-spam windows, bio cache, alert dedup,
    last-job timestamps). Replaces ad-hoc ``bot_data`` keys."""


class _StateView(MutableMapping):
    """Dict-compatible read/write-through view over :class:`AppState`.

    Kept so existing ``context.bot_data[...]`` access patterns (and their
    tests) keep working while production code moves to the explicit
    ``context.state.<field>`` attributes. Well-known keys map to typed
    ``AppState`` fields; anything else falls through to ``state.data``.
    """

    _FIELDS = frozenset(
        {
            "plugin_handlers",
            "plugin_effective_map",
            "group_admin_ids",
            "admin_ids",
            "trusted_user_ids",
            "start_time",
        }
    )

    def __init__(self, state: AppState) -> None:
        self._state = state

    def __getitem__(self, key: str) -> Any:
        if key in self._FIELDS:
            value = getattr(self._state, key)
            if value is None:
                raise KeyError(key)
            return value
        return self._state.data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self._FIELDS:
            setattr(self._state, key, value)
        else:
            self._state.data[key] = value

    def __delitem__(self, key: str) -> None:
        if key in self._FIELDS:
            setattr(self._state, key, None)
        else:
            del self._state.data[key]

    def __iter__(self) -> Iterator[str]:
        for key in self._FIELDS:
            if getattr(self._state, key) is not None:
                yield key
        yield from self._state.data

    def __len__(self) -> int:
        return sum(1 for _ in self.__iter__())


@dataclass
class HandlerContext:
    """Per-update handler context (replaces PTB ``CallbackContext``).

    A fresh instance is built for every dispatched update.
    """

    bot: Bot
    """aiogram Bot instance."""

    state: AppState
    """Shared application state."""

    args: list[str] | None = None
    """Parsed command arguments (set for command handlers only)."""

    @property
    def bot_data(self) -> _StateView:
        """Dict-compatible view over :attr:`state` (migration shim)."""
        return _StateView(self.state)

    def create_task(self, coro: Awaitable[Any]) -> asyncio.Task:
        """Schedule a background coroutine, logging unhandled exceptions.

        Replacement for PTB ``Application.create_task``: keeps the update
        pipeline non-blocking while making sure background failures are
        logged instead of silently dropped.
        """
        task = asyncio.ensure_future(coro)

        def _done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error("Background task failed", exc_info=exc)

        task.add_done_callback(_done)
        return task


# ---------------------------------------------------------------------------
# Update introspection helpers (replacements for PTB's update.effective_*)
# ---------------------------------------------------------------------------


def effective_message(update: Update) -> Message | None:
    """Return the message or edited message of an update, if any."""
    return update.message or update.edited_message


def effective_chat(update: Update) -> Chat | None:
    """Return the chat an update belongs to, if any."""
    message = effective_message(update)
    if message is not None:
        return message.chat
    query = update.callback_query
    if query is not None and query.message is not None:
        return query.message.chat
    if update.chat_member is not None:
        return update.chat_member.chat
    return None


def effective_user(update: Update) -> User | None:
    """Return the user an update originates from, if any."""
    message = effective_message(update)
    if message is not None:
        return message.from_user
    if update.callback_query is not None:
        return update.callback_query.from_user
    if update.chat_member is not None:
        return update.chat_member.from_user
    return None


def update_kind(update: Update) -> str | None:
    """Classify an update into one of the handled kinds.

    Only the update types listed in ``allowed_updates`` are expected;
    anything else returns None and is ignored.
    """
    if update.message is not None:
        return "message"
    if update.edited_message is not None:
        return "edited_message"
    if update.callback_query is not None:
        return "callback_query"
    if update.chat_member is not None:
        return "chat_member"
    return None


# ---------------------------------------------------------------------------
# Filter predicates (replacements for PTB filters)
# ---------------------------------------------------------------------------

FilterPredicate = Callable[[Update], bool]


def is_message_or_edited(update: Update) -> bool:
    """Match message and edited_message updates."""
    return update.message is not None or update.edited_message is not None


def is_callback_query(update: Update) -> bool:
    """Match callback_query updates with data."""
    return update.callback_query is not None and bool(update.callback_query.data)


def is_chat_member_update(update: Update) -> bool:
    """Match chat_member updates."""
    return update.chat_member is not None


def _message_or_none(update: Update) -> Message | None:
    return update.message or update.edited_message


def is_group_chat(update: Update) -> bool:
    """Match updates from group/supergroup chats."""
    chat = effective_chat(update)
    return chat is not None and chat.type in ("group", "supergroup")


def is_private_chat(update: Update) -> bool:
    """Match updates from private chats."""
    chat = effective_chat(update)
    return chat is not None and chat.type == "private"


def is_command_message(update: Update) -> bool:
    """Match messages whose first entity is a bot_command at offset 0.

    Mirrors PTB ``filters.COMMAND``.
    """
    message = _message_or_none(update)
    if message is None:
        return False
    entities = message.entities or []
    return bool(
        entities
        and entities[0].type == "bot_command"
        and entities[0].offset == 0
    )


def has_text(update: Update) -> bool:
    """Match messages carrying text."""
    message = _message_or_none(update)
    return message is not None and bool(message.text)


def has_contact(update: Update) -> bool:
    """Match messages carrying a contact card."""
    message = _message_or_none(update)
    return message is not None and message.contact is not None


def is_forwarded(update: Update) -> bool:
    """Match forwarded messages (Bot API 7+ forward_origin)."""
    message = _message_or_none(update)
    return message is not None and message.forward_origin is not None


def has_new_chat_members(update: Update) -> bool:
    """Match messages announcing new chat members (join events)."""
    message = _message_or_none(update)
    return message is not None and bool(message.new_chat_members)


def is_guest_bot_message(update: Update) -> bool:
    """Match Telegram Guest Mode messages (Bot API 10.1 caller fields)."""
    message = _message_or_none(update)
    return (
        message is not None
        and (
            message.guest_bot_caller_user is not None
            or message.guest_bot_caller_chat is not None
        )
    )


def callback_data_pattern(pattern: str) -> FilterPredicate:
    """Build a predicate matching callback query data against a regex.

    Uses ``re.match`` (anchored at the start), mirroring PTB's
    ``CallbackQueryHandler(pattern=...)``.
    """
    compiled = re.compile(pattern)

    def _check(update: Update) -> bool:
        query: CallbackQuery | None = update.callback_query
        return query is not None and bool(query.data) and compiled.match(query.data) is not None

    _check.__name__ = f"callback_data_pattern({pattern!r})"
    return _check


def command_filter(command: str, state: AppState) -> FilterPredicate:
    """Build a predicate matching ``/command`` and ``/command@botname``.

    Mirrors PTB ``CommandHandler(command)``: the message must carry a
    ``bot_command`` entity at offset 0, and an ``@username`` suffix must
    match this bot's username (resolved at startup into
    ``state.bot_username``).
    """

    def _check(update: Update) -> bool:
        message = _message_or_none(update)
        if message is None or not message.text:
            return False
        entities = message.entities or []
        if (
            not entities
            or entities[0].type != "bot_command"
            or entities[0].offset != 0
        ):
            return False
        first_token = message.text.split()[0]
        if not first_token.startswith("/"):
            return False
        name = first_token[1:].split("@")
        if name[0] != command:
            return False
        if len(name) > 1:
            username = state.bot_username
            return username is not None and name[1].lower() == username.lower()
        return True

    _check.__name__ = f"command_filter({command!r})"
    return _check


def parse_command_args(message: Message) -> list[str]:
    """Split command arguments the way PTB's CommandHandler did."""
    return (message.text or "").split()[1:]


# ---------------------------------------------------------------------------
# Handler registration spec
# ---------------------------------------------------------------------------


@dataclass
class HandlerSpec:
    """One handler registration (replaces a PTB ``BaseHandler`` + group).

    Attributes:
        plugin_name: Manifest plugin name (for logging/gating).
        group: Handler group (-1..7); dispatch runs groups in ascending order.
        update_kinds: Update kinds this handler accepts
            ("message", "edited_message", "callback_query", "chat_member").
        check: Sync predicate deciding whether the handler matches an update.
        callback: Async ``(update, context)`` handler function.
        label: Human-readable label for logging.
        command: Command name when this is a command handler; the dispatch
            loop parses ``context.args`` for matching updates.
    """

    plugin_name: str
    group: int
    update_kinds: tuple[str, ...]
    check: FilterPredicate
    callback: Callable[[Update, HandlerContext], Awaitable[None]]
    label: str = ""
    command: str | None = None


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def handle_bot_error(update: Update | None, exc: BaseException) -> None:
    """Log handler errors (replaces PTB ``add_error_handler`` callback).

    Network-level failures (PTB ``TimedOut``/``NetworkError`` map to
    aiogram's ``TelegramNetworkError``) are transient and logged at
    warning level; everything else is logged as an error with traceback.
    """
    if isinstance(exc, TelegramNetworkError):
        logger.warning(f"Telegram network error: {exc}")
        return
    logger.error("Unhandled exception in update handler:", exc_info=exc)


# ---------------------------------------------------------------------------
# Dispatch loop
# ---------------------------------------------------------------------------


def _grouped_specs(state: AppState) -> list[tuple[int, list[HandlerSpec]]]:
    """Return handler specs grouped and ordered for dispatch.

    Groups ascend (-1, 0..7); within a group, specs keep registration
    order (``register_all`` inserts in ``MANIFEST_ORDER``) so the first
    match wins.
    """
    groups: dict[int, list[HandlerSpec]] = {}
    for meta in state.plugin_handlers.values():
        for spec in meta["handlers"]:
            groups.setdefault(spec.group, []).append(spec)
    return sorted(groups.items(), key=lambda item: item[0])


async def dispatch_update(update: Update, bot: Bot, state: AppState) -> None:
    """Dispatch one update through the handler groups (PTB semantics).

    See the module docstring for the exact ordering/stop rules.
    """
    kind = update_kind(update)
    if kind is None:
        return

    context = HandlerContext(bot=bot, state=state)

    for _group, specs in _grouped_specs(state):
        for spec in specs:
            if kind not in spec.update_kinds:
                continue
            try:
                matched = spec.check(update)
            except Exception:
                logger.warning(
                    f"Filter error in handler '{spec.label}', skipping",
                    exc_info=True,
                )
                continue
            if not matched:
                continue
            if spec.command is not None:
                message = _message_or_none(update)
                context.args = parse_command_args(message) if message else []
            try:
                await spec.callback(update, context)
            except StopPropagation:
                return
            except Exception as exc:  # noqa: BLE001 - PTB parity: route to error handler, try next
                await handle_bot_error(update, exc)
                continue
            # First matching handler wins within a group.
            break

"""Tests for rich message text/URL extraction and rich spam detection."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import (
    Chat,
    Message,
    RichBlockButtons,
    RichBlockParagraph,
    RichBlockSectionHeading,
    RichBlockTable,
    RichBlockTableCell,
    RichMessage,
    RichMessageButton,
    RichTextBold,
    RichTextCustomEmoji,
    RichTextUrl,
    User,
)

from bot.dispatch import AppState, HandlerContext, StopPropagation
from bot.group_config import GroupConfig
from bot.handlers.ai_spam_monitor import _classifiable_text
from bot.handlers.anti_spam import (
    handle_inline_keyboard_spam,
    has_non_whitelisted_rich_message_urls,
)
from bot.services.rich_message import extract_rich_text, extract_rich_urls

SPAM_URL = "https://t.me/base6d1ef16_bot?startapp=138762"


def _spam_blocks():
    """Rich blocks mirroring the 2026-10-17 adult-spam incident."""
    return [
        RichBlockSectionHeading(text="HOT 2026 DROP", size=1),
        RichBlockParagraph(
            text=[
                RichTextBold(text="FRESH LEAKS"),
                " ",
                RichTextCustomEmoji(
                    custom_emoji_id="5375191900686927814", alternative_text="🍑"
                ),
            ]
        ),
        RichBlockTable(
            cells=[
                [
                    RichBlockTableCell(
                        text=RichTextUrl(text="BIKINI SHOWS", url=SPAM_URL),
                        align="center",
                        valign="middle",
                    ),
                    RichBlockTableCell(
                        text="EXCLUSIVE PICS", align="center", valign="middle"
                    ),
                ]
            ]
        ),
        RichBlockButtons(
            buttons=[
                RichMessageButton(text="OPEN LINK", url=SPAM_URL),
                RichMessageButton(text="ENTER NOW", url=SPAM_URL),
            ]
        ),
    ]


def _make_message(blocks=None, text=None):
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=-100123456, type="supergroup"),
        text=text,
        rich_message=RichMessage(blocks=blocks) if blocks is not None else None,
    )


class TestExtractRichText:
    def test_extracts_heading_paragraph_table_and_buttons(self):
        text = extract_rich_text(RichMessage(blocks=_spam_blocks()))
        for expected in (
            "HOT 2026 DROP",
            "FRESH LEAKS",
            "🍑",
            "BIKINI SHOWS",
            "EXCLUSIVE PICS",
            "OPEN LINK",
            "ENTER NOW",
        ):
            assert expected in text

    def test_none_returns_empty(self):
        assert extract_rich_text(None) == ""

    def test_empty_blocks_returns_empty(self):
        assert extract_rich_text(RichMessage(blocks=[])) == ""

    def test_plain_string_blocks(self):
        text = extract_rich_text(
            RichMessage(blocks=[RichBlockParagraph(text="hello world")])
        )
        assert text == "hello world"

    def test_walk_failure_returns_empty(self):
        class _Boom:
            @property
            def url(self):
                raise RuntimeError("boom")

        assert extract_rich_text(_Boom()) == ""


class TestExtractRichUrls:
    def test_extracts_button_and_text_link_urls_deduped(self):
        urls = extract_rich_urls(RichMessage(blocks=_spam_blocks()))
        assert urls == [SPAM_URL]

    def test_none_returns_empty(self):
        assert extract_rich_urls(None) == []

    def test_no_urls_returns_empty(self):
        assert (
            extract_rich_urls(
                RichMessage(blocks=[RichBlockParagraph(text="no links here")])
            )
            == []
        )

    def test_walk_failure_returns_empty(self):
        class _Boom:
            @property
            def url(self):
                raise RuntimeError("boom")

        assert extract_rich_urls(_Boom()) == []


class TestHasNonWhitelistedRichMessageUrls:
    def test_spam_rich_message_detected(self):
        assert has_non_whitelisted_rich_message_urls(_make_message(_spam_blocks())) is True

    def test_whitelisted_rich_urls_ignored(self):
        blocks = [
            RichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="Docs", url="https://docs.python.org/3/library/"
                    )
                ]
            )
        ]
        assert has_non_whitelisted_rich_message_urls(_make_message(blocks)) is False

    def test_no_rich_message_ignored(self):
        assert has_non_whitelisted_rich_message_urls(_make_message()) is False

    def test_rich_without_urls_ignored(self):
        blocks = [RichBlockParagraph(text="just text, no links")]
        assert has_non_whitelisted_rich_message_urls(_make_message(blocks)) is False


class TestClassifiableTextRich:
    def test_rich_content_included(self):
        text = _classifiable_text(_make_message(_spam_blocks()))
        assert "HOT 2026 DROP" in text
        assert "BIKINI SHOWS" in text
        assert SPAM_URL in text

    def test_plain_text_unchanged(self):
        assert _classifiable_text(_make_message(text="hello")) == "hello"

    def test_caption_unchanged(self):
        msg = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=1, type="private"),
            caption="a caption",
        )
        assert _classifiable_text(msg) == "a caption"

    def test_empty_message_stays_empty(self):
        assert _classifiable_text(_make_message()) == ""

    def test_text_and_rich_combined(self):
        text = _classifiable_text(_make_message(_spam_blocks(), text="intro"))
        assert text.startswith("intro")
        assert "HOT 2026 DROP" in text


class TestHandleInlineKeyboardSpamRich:
    """Rich-message spam takes the auto-delete path at group=1."""

    @pytest.fixture
    def mock_update(self):
        update = MagicMock()
        update.message = MagicMock(spec=Message)
        update.message.from_user = MagicMock(spec=User)
        update.message.from_user.id = 12345
        update.message.from_user.is_bot = False
        update.message.from_user.full_name = "Spam User"
        update.message.from_user.username = "spamuser"
        update.message.delete = AsyncMock()
        update.message.reply_markup = None
        update.edited_message = None
        return update

    @pytest.fixture
    def mock_context(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.restrict_chat_member = AsyncMock()
        state = AppState()
        state.group_admin_ids = {}
        state.trusted_user_ids = set()
        return HandlerContext(bot=bot, state=state, args=[])

    @pytest.fixture
    def group_config(self):
        return GroupConfig(
            group_id=-100123456,
            warning_topic_id=123,
            rules_link="https://example.com/rules",
        )

    async def test_rich_spam_deleted_restricted_notified(
        self, mock_update, mock_context, group_config
    ):
        mock_update.message.rich_message = RichMessage(blocks=_spam_blocks())

        with patch(
            "bot.handlers.anti_spam.get_group_config_for_update",
            return_value=group_config,
        ):
            with pytest.raises(StopPropagation):
                await handle_inline_keyboard_spam(mock_update, mock_context)

        mock_update.message.delete.assert_called_once()
        mock_context.bot.restrict_chat_member.assert_called_once()
        mock_context.bot.send_message.assert_called_once()
        sent_text = mock_context.bot.send_message.call_args.kwargs["text"]
        assert "rich text" in sent_text

    async def test_whitelisted_rich_urls_ignored(
        self, mock_update, mock_context, group_config
    ):
        mock_update.message.rich_message = RichMessage(
            blocks=[
                RichBlockButtons(
                    buttons=[
                        RichMessageButton(
                            text="Docs", url="https://docs.python.org/3/library/"
                        )
                    ]
                )
            ]
        )

        with patch(
            "bot.handlers.anti_spam.get_group_config_for_update",
            return_value=group_config,
        ):
            await handle_inline_keyboard_spam(mock_update, mock_context)

        mock_update.message.delete.assert_not_called()
        mock_context.bot.restrict_chat_member.assert_not_called()

    async def test_no_rich_message_ignored(self, mock_update, mock_context, group_config):
        mock_update.message.rich_message = None

        with patch(
            "bot.handlers.anti_spam.get_group_config_for_update",
            return_value=group_config,
        ):
            await handle_inline_keyboard_spam(mock_update, mock_context)

        mock_update.message.delete.assert_not_called()

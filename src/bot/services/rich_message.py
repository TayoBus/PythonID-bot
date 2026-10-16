"""Extract searchable text and URLs from Telegram rich messages.

Rich messages (``Message.rich_message``, Bot API 10.1+/aiogram 3.29+)
carry their payload in page blocks instead of ``message.text``. Spam
filters that only read ``text``/``caption`` are blind to them — a rich
spam message arrives with empty text while its links and buttons hide
in the block tree. This module walks that tree and returns plain text
and URLs for the spam detectors.

The walker is duck-typed on purpose: aiogram exposes ~25 block types
and a ~30-variant rich-text union, and a newly added variant must not
silently become invisible to the filters again.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Attributes that hold nested blocks, cells, buttons, or list items.
_CONTAINER_ATTRS = ("blocks", "cells", "buttons", "items")
# Text-bearing attributes besides the generic ``text``.
_TEXT_ATTRS = ("caption", "summary", "alternative_text")


def _walk(node: Any, texts: list[str], urls: list[str]) -> None:
    """Recursively collect text fragments and URLs from a rich node."""
    if node is None:
        return
    if isinstance(node, str):
        stripped = node.strip()
        if stripped:
            texts.append(stripped)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _walk(item, texts, urls)
        return

    url = getattr(node, "url", None)
    if isinstance(url, str) and url.strip():
        urls.append(url.strip())
    for nested in ("login_url", "web_app"):
        nested_url = getattr(getattr(node, nested, None), "url", None)
        if isinstance(nested_url, str) and nested_url.strip():
            urls.append(nested_url.strip())

    for attr in ("text",) + _TEXT_ATTRS:
        value = getattr(node, attr, None)
        if value is not None:
            _walk(value, texts, urls)

    for attr in _CONTAINER_ATTRS:
        value = getattr(node, attr, None)
        if value:
            _walk(value, texts, urls)


def extract_rich_text(rich_message: Any | None) -> str:
    """Return searchable plain text from a rich message, or "" when none.

    Covers headings, paragraphs, table cells, button labels, and custom
    emoji alternative text. URLs are intentionally excluded here — use
    :func:`extract_rich_urls` for those.
    """
    if rich_message is None:
        return ""
    texts: list[str] = []
    try:
        _walk(rich_message, texts, [])
    except Exception:
        logger.warning("extract_rich_text: failed to walk rich message", exc_info=True)
        return ""
    return " ".join(texts)


def extract_rich_urls(rich_message: Any | None) -> list[str]:
    """Return deduplicated URLs from a rich message's buttons and text links."""
    if rich_message is None:
        return []
    urls: list[str] = []
    try:
        _walk(rich_message, [], urls)
    except Exception:
        logger.warning("extract_rich_urls: failed to walk rich message", exc_info=True)
        return []
    return list(dict.fromkeys(urls))

"""Tests for the captcha plugin handler registration."""
from unittest.mock import patch

from bot.dispatch import AppState, HandlerSpec
from bot.plugins.builtin.captcha import register_captcha


def _make_spec(label: str) -> HandlerSpec:
    async def _callback(update, context):
        pass

    return HandlerSpec(
        plugin_name="captcha",
        group=0,
        update_kinds=("message",),
        check=lambda update: True,
        callback=_callback,
        label=label,
    )


def test_register_captcha_clones_handlers():
    """Test that register_captcha rebuilds specs before wrapping."""
    state = AppState()

    spec1 = _make_spec("one")
    spec2 = _make_spec("two")
    fixed_handlers = [spec1, spec2]

    with patch("bot.plugins.builtin.captcha.captcha.get_handlers", return_value=fixed_handlers):
        registered = register_captcha(state)

    # Registered specs must be different objects (rebuilt, not mutated)
    for reg in registered:
        assert reg is not spec1 and reg is not spec2, (
            "Spec should be rebuilt, not the original"
        )
    # Wrapped callbacks must differ from the originals
    assert registered[0].callback is not spec1.callback
    assert registered[1].callback is not spec2.callback
    # Non-callback fields are preserved
    assert registered[0].label == "one"
    assert registered[0].group == 0


def test_register_captcha_does_not_mutate_original_handlers():
    """Test that register_captcha rebuilds specs instead of mutating originals."""
    state = AppState()

    spec1 = _make_spec("one")
    spec2 = _make_spec("two")
    original_cb1 = spec1.callback
    original_cb2 = spec2.callback
    fixed_handlers = [spec1, spec2]

    with patch("bot.plugins.builtin.captcha.captcha.get_handlers", return_value=fixed_handlers):
        register_captcha(state)

    # Original spec callbacks must be unchanged
    assert spec1.callback is original_cb1, "Original spec callback should not be mutated"
    assert spec2.callback is original_cb2, "Original spec callback should not be mutated"

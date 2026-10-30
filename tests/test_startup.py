from __future__ import annotations

import io

import pytest

from agent_cli.startup import HINT, TAGLINE, WORDMARK, render_startup, supports_color


def test_plain_startup_contains_wordmark_tagline_and_hint() -> None:
    screen = render_startup(color=False)
    for line in (*WORDMARK, TAGLINE, HINT):
        assert line in screen
    assert "\033[" not in screen


def test_startup_is_compact() -> None:
    assert len(render_startup(color=False).splitlines()) <= 5


def test_color_startup_styles_the_wordmark() -> None:
    assert f"\033[1;36m{WORDMARK[0]}\033[0m" in render_startup(color=True)


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_color_requires_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert supports_color(TTY())
    assert not supports_color(io.StringIO())


def test_no_color_disables_styling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert not supports_color(TTY())

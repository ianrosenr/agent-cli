from __future__ import annotations

import io
from collections.abc import Callable

import pytest
from rich.console import Console, RenderableType

from agent_cli.commands import Command, CommandContext, build_registry
from agent_cli.ui import (
    THEME,
    error,
    home,
    info,
    make_console,
    plain,
    success,
    supports_color,
    table,
    warning,
)
from agent_cli.ui.console import PIPE_WIDTH
from agent_cli.ui.home import HINT, TAGLINE, WORDMARK


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def colored(renderable: RenderableType, width: int = 100) -> str:
    """``renderable`` rendered for a color terminal, ANSI codes included."""
    buffer = TTY()
    console = Console(
        file=buffer,
        width=width,
        theme=THEME,
        color_system="truecolor",
        force_terminal=True,
        markup=False,
        emoji=False,
        highlight=False,
    )
    console.print(renderable)
    return buffer.getvalue()


# -- Home ----------------------------------------------------------------------------------------


def test_home_shows_wordmark_tagline_hint_and_registry_commands() -> None:
    registry = build_registry()
    screen = plain(home(registry))
    for line in (*WORDMARK, TAGLINE, HINT):
        assert line in screen
    available = [c for c in registry if c.planned_in is None]
    commands = screen.splitlines()[len(WORDMARK) + 2].split()
    assert commands == [f"/{command.name}" for command in available]
    assert "/review" not in screen  # planned commands are not offered on the home screen


def test_home_reflects_newly_registered_commands() -> None:
    registry = build_registry()
    registry.register(Command("extra", "An extra command.", lambda context, args: ""))
    assert "/extra" in plain(home(registry))


def test_home_stays_compact() -> None:
    assert len(plain(home(build_registry())).splitlines()) <= 6


def test_home_has_an_ascii_wordmark_for_non_unicode_terminals() -> None:
    screen = plain(home(build_registry(), unicode=False))
    assert "AGENT CLI" in screen
    assert not any(glyph in screen for glyph in "▄▀█")


def test_home_styles_the_wordmark_on_color_terminals() -> None:
    screen = colored(home(build_registry()))
    assert "\033[" in screen and WORDMARK[0] in screen


# -- Messages ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("render", "label"), [(success, "Done"), (warning, "Warning"), (error, "Error"), (info, "Note")]
)
def test_messages_name_their_kind_in_words(
    render: Callable[..., RenderableType], label: str
) -> None:
    text = plain(render("Something happened.", "A detail."))
    assert text.splitlines()[0] == f"{label}  Something happened."
    # The detail aligns under the message, not under the label.
    assert text.splitlines()[1] == " " * (len(label) + 2) + "A detail."


def test_error_is_colored_on_a_color_terminal_and_plain_otherwise() -> None:
    message = error("No compatible provider path is available.", "Pinned model: claude-sonnet")
    assert "\033[" in colored(message)
    assert "\033[" not in plain(message)


def test_text_is_never_interpreted_as_markup() -> None:
    shown = "[bold]not bold[/bold] [pending] :smile:"
    assert plain(error(shown)) == f"Error  {shown}"
    assert shown in colored(error(shown))


# -- Tables and width ----------------------------------------------------------------------------


def test_tables_align_headers_and_cells() -> None:
    lines = plain(table(("Name", "Tokens"), [("alpha", "12"), ("beta", "3")], right={1})).split(
        "\n"
    )
    assert lines[0].split() == ["Name", "Tokens"]
    assert lines[1].split() == ["alpha", "12"]
    assert lines[2].endswith(" 3") and lines[1].endswith("12")  # right-aligned numbers


def test_narrow_terminals_wrap_cells_without_losing_content() -> None:
    path = "/a/very/long/path/that/cannot/possibly/fit/in/a/narrow/terminal/SKILL.md"
    text = plain(table(("Skill", "Source"), [("testing", path)]), width=30)
    assert all(len(line) <= 30 for line in text.splitlines())
    assert "".join(line.strip() for line in text.splitlines()).count("SKILL.md") == 1
    assert "…" not in text


def test_messages_wrap_on_narrow_terminals() -> None:
    text = plain(
        warning("No low-cost classifier is configured.", "Using deterministic routing."), 24
    )
    assert all(len(line) <= 24 for line in text.splitlines())
    assert text.startswith("Warning")


# -- Console -------------------------------------------------------------------------------------


def test_color_requires_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert supports_color(TTY())
    assert not supports_color(io.StringIO())


def test_no_color_disables_color_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    stream = TTY()
    assert not supports_color(stream)
    make_console(stream, width=80).print(error("Broken."))
    assert "\033[3" not in stream.getvalue()  # no foreground colors
    assert "Error  Broken." in stream.getvalue()


def test_piped_output_is_plain_and_does_not_fold_at_80_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COLUMNS", raising=False)
    stream = io.StringIO()
    console = make_console(stream)
    assert console.width == PIPE_WIDTH
    assert not console.is_terminal
    long = "x" * 150
    console.print(info(long))
    assert "\033[" not in stream.getvalue()
    assert long in stream.getvalue()


def test_every_theme_style_parses() -> None:
    console = Console(theme=THEME)
    for name in THEME.styles:
        console.get_style(name)


@pytest.mark.parametrize(
    "line", ["/help", "/status", "/providers", "/models", "/budget", "/skills", "/autopilot"]
)
def test_command_output_renders_on_a_color_terminal(line: str) -> None:
    """Every style a command uses resolves; a missing one would raise while rendering."""
    context = CommandContext(registry=build_registry())
    rendered = colored(context.registry.dispatch(line, context))
    assert plain(context.registry.dispatch(line, context)).split()[0] in rendered

"""The home screen shown before the first prompt: compact, and fast to draw."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from rich.console import Group, RenderableType
from rich.text import Text

from agent_cli import __version__
from agent_cli.ui.render import muted, styled

if TYPE_CHECKING:
    from agent_cli.commands import Command

WORDMARK = (
    "▄▀█ █▀▀ █▀▀ █▄ █ ▀█▀   █▀▀ █   █",
    "█▀█ █▄█ ██▄ █ ▀█  █    █▄▄ █▄▄ █",
)
ASCII_WORDMARK = ("AGENT CLI",)
MARGIN = "  "
TAGLINE = "orchestration control plane"
HINT = f"v{__version__} · describe a task at the prompt · /help for details · /exit to quit"


def home(commands: Iterable[Command], unicode: bool = True) -> RenderableType:
    """Wordmark, tagline, version hint, and the available commands from the registry."""
    wordmark = WORDMARK if unicode else ASCII_WORDMARK
    lines = [
        *(styled(line, "title") for line in wordmark),
        Text(TAGLINE),
        muted(HINT),
        command_list(commands),
    ]
    return Group(*(Text(MARGIN) + line for line in lines), Text(""))


def command_list(commands: Iterable[Command]) -> Text:
    """Implemented commands only, in registry order; wraps on narrow terminals."""
    from agent_cli.commands.registry import SLASH

    line = Text()
    for command in commands:
        if command.planned_in is not None:
            continue
        if line:
            line.append("  ")
        line.append(SLASH + command.name, style="command")
    return line

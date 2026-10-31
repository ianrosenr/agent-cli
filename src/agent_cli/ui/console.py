"""The one Rich ``Console`` the shell prints through, and a plain-text renderer for tests.

Markup, emoji codes, and automatic highlighting are always off: text from users, models, and
task state is shown exactly as written (``[pending]`` stays ``[pending]``), and styling comes
only from the semantic styles in :mod:`agent_cli.ui.styles`.
"""

from __future__ import annotations

import io
import os
import sys
from typing import TextIO

from rich.console import Console, RenderableType

from agent_cli.ui.styles import THEME

PIPE_WIDTH = 200
"""Width when output is not a terminal and ``COLUMNS`` is unset: wide enough that piped rows,
paths, and messages stay on one line instead of folding at Rich's default of 80."""


def supports_color(stream: TextIO) -> bool:
    """Respect NO_COLOR and only style output written to a terminal."""
    return "NO_COLOR" not in os.environ and stream.isatty()


def make_console(stream: TextIO | None = None, width: int | None = None) -> Console:
    """A console for ``stream`` (stdout by default): styled on a color terminal, plain text
    everywhere else, with no cursor control or live display when output is piped."""
    stream = sys.stdout if stream is None else stream
    color = supports_color(stream)
    if width is None and not stream.isatty() and "COLUMNS" not in os.environ:
        width = PIPE_WIDTH
    return Console(
        file=stream,
        width=width,
        theme=THEME,
        color_system="auto" if color else None,
        no_color=not color,
        force_terminal=color,
        markup=False,
        emoji=False,
        highlight=False,
    )


def plain(renderable: RenderableType, width: int = PIPE_WIDTH) -> str:
    """``renderable`` as plain text without ANSI codes, trailing spaces, or trailing blank
    lines."""
    buffer = io.StringIO()
    console = Console(
        file=buffer,
        width=width,
        theme=THEME,
        color_system=None,
        no_color=True,
        force_terminal=False,
        markup=False,
        emoji=False,
        highlight=False,
        legacy_windows=False,
    )
    console.print(renderable)
    return "\n".join(line.rstrip() for line in buffer.getvalue().splitlines()).rstrip("\n")


def is_unicode(console: Console) -> bool:
    """Whether ``console`` can show the block-character wordmark."""
    return console.encoding.lower().replace("-", "").startswith("utf")

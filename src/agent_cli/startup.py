"""The startup screen shown before the first prompt."""

from __future__ import annotations

import os
from typing import TextIO

from agent_cli import __version__

WORDMARK = (
    "▄▀█ █▀▀ █▀▀ █▄ █ ▀█▀   █▀▀ █   █",
    "█▀█ █▄█ ██▄ █ ▀█  █    █▄▄ █▄▄ █",
)
TAGLINE = "orchestration control plane"
HINT = f"v{__version__} · describe a task, or /help for commands · /exit to quit"

ACCENT = "\033[1;36m"
DIM = "\033[2m"
RESET = "\033[0m"


def render_startup(color: bool) -> str:
    """Return the compact wordmark, tagline, and hint, optionally ANSI-styled."""

    def style(text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if color else text

    lines = [style(line, ACCENT) for line in WORDMARK]
    lines += [TAGLINE, style(HINT, DIM), ""]
    return "\n".join(f"  {line}" if line else line for line in lines)


def supports_color(stream: TextIO) -> bool:
    """Respect NO_COLOR and only style output written to a terminal."""
    return "NO_COLOR" not in os.environ and stream.isatty()

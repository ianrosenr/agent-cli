"""Test helpers for shell output, which is Rich renderables rendered to plain text."""

from __future__ import annotations

from collections.abc import Callable

from rich.console import RenderableType

from agent_cli.ui import plain


def recorder() -> tuple[list[str], Callable[[RenderableType], None]]:
    """A ``write_line`` that records each output as plain text, and the list it records to."""
    output: list[str] = []

    def write_line(renderable: RenderableType) -> None:
        output.append(plain(renderable))

    return output, write_line

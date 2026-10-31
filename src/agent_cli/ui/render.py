"""Reusable renderables: messages, headings, sections, and label/value fields.

Every helper returns a Rich renderable built from :class:`rich.text.Text`, never markup
strings, so text from users and models cannot be mistaken for styling.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

type Kind = Literal["success", "warning", "error", "info"]

LABELS: dict[Kind, str] = {
    "success": "Done",
    "warning": "Warning",
    "error": "Error",
    "info": "Note",
}
"""Each message kind is named in words, so it never depends on color."""
INDENT = 2


def message(kind: Kind, text: str, *details: str) -> RenderableType:
    """``Label  text`` with any ``details`` aligned beneath the text, dimmed."""
    body = Text(text)
    for detail in details:
        body.append("\n")
        body.append(detail, style="muted")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row(Text(LABELS[kind], style=f"label.{kind}"), body)
    return grid


def success(text: str, *details: str) -> RenderableType:
    return message("success", text, *details)


def warning(text: str, *details: str) -> RenderableType:
    return message("warning", text, *details)


def error(text: str, *details: str) -> RenderableType:
    return message("error", text, *details)


def info(text: str, *details: str) -> RenderableType:
    return message("info", text, *details)


def heading(text: str) -> Text:
    return Text(text, style="heading")


def styled(text: str, style: str) -> Text:
    return Text(text, style=style)


def muted(text: str) -> Text:
    return Text(text, style="muted")


def indented(renderable: RenderableType, level: int = 1) -> RenderableType:
    return Padding(renderable, (0, 0, 0, INDENT * level), expand=False)


def blocks(*parts: RenderableType | None) -> Group:
    """``parts`` separated by one blank line; ``None`` parts are skipped."""
    shown = [part for part in parts if part is not None]
    spaced: list[RenderableType] = []
    for index, part in enumerate(shown):
        if index:
            spaced.append(Text(""))
        spaced.append(part)
    return Group(*spaced)


def section(title: str, *body: RenderableType, note: str = "") -> Group:
    """A heading, with an optional dimmed note beside it, followed directly by its body."""
    title_line = heading(title)
    if note:
        title_line.append(f"  {note}", style="muted")
    return Group(title_line, *body)


def bullets(items: Iterable[str], style: str | None = None) -> RenderableType:
    """``- item`` lines with wrapped text hanging under the item, not the dash."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, style="muted")
    grid.add_column(overflow="fold", style=style or "")
    for item in items:
        grid.add_row("-", Text(item))
    return grid


def fields(
    rows: Iterable[tuple[str, RenderableType]], indent: int = 0, label_style: str = "label"
) -> RenderableType:
    """Aligned ``label  value`` rows, labels dimmed."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True, style=label_style)
    grid.add_column(overflow="fold")
    for label, value in rows:
        grid.add_row(Text(label), Text(value) if isinstance(value, str) else value)
    return indented(grid, indent) if indent else grid

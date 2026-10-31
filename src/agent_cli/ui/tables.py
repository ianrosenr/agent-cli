"""Borderless, indented tables: hierarchy through alignment and a bold header, not boxes."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from rich.console import RenderableType
from rich.table import Table
from rich.text import Text

from agent_cli.ui.render import indented


def table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str | Text]],
    styles: Sequence[str | None] = (),
    right: Collection[int] = (),
    indent: int = 1,
    header: bool = True,
) -> RenderableType:
    """A compact table. ``styles`` names a semantic style per column; ``right`` lists the
    columns aligned right (numbers). Cells wrap rather than truncate on narrow terminals."""
    grid = Table(
        box=None,
        show_edge=False,
        pad_edge=False,
        padding=(0, 1),
        header_style="heading",
        show_header=header,
    )
    for index, title in enumerate(headers):
        style = styles[index] if index < len(styles) else None
        grid.add_column(
            title,
            style=style or "",
            justify="right" if index in right else "left",
            overflow="fold",
        )
    for row in rows:
        grid.add_row(*(cell if isinstance(cell, Text) else Text(cell) for cell in row))
    return indented(grid, indent) if indent else grid

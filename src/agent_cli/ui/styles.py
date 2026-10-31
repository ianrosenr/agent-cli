"""Semantic styles: the only place Agent CLI chooses colors.

Renderers name what a piece of text *is* (a command, a model, an error); this theme decides how
that looks. Meaning never depends on color alone: every styled state also carries a word or a
position, so output stays readable under ``NO_COLOR`` and in pipes.
"""

from __future__ import annotations

from rich.theme import Theme

STYLES: dict[str, str] = {
    "title": "bold cyan",
    "heading": "bold",
    "muted": "dim",
    "label": "dim",
    "success": "green",
    "warning": "yellow",
    "error": "red",
    "accent": "cyan",
    "command": "cyan",
    "value": "bold",
    "path": "blue",
    "model": "magenta",
    "provider": "blue",
    "role": "cyan",
    "money": "green",
    "label.success": "bold green",
    "label.warning": "bold yellow",
    "label.error": "bold red",
    "label.info": "bold cyan",
    "step": "bold cyan",
    "status.done": "green",
    "status.active": "cyan",
    "status.blocked": "red",
    "status.pending": "dim",
}

THEME = Theme(STYLES, inherit=False)

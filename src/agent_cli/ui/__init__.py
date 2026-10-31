"""Terminal rendering: the presentation boundary between Agent CLI and the terminal.

Rich owns rendered output here; prompt_toolkit keeps owning input. Services and status views
build renderables with these helpers instead of writing styles or markup themselves.
"""

from agent_cli.ui.console import is_unicode, make_console, plain, supports_color
from agent_cli.ui.home import home
from agent_cli.ui.render import (
    blocks,
    bullets,
    error,
    fields,
    heading,
    indented,
    info,
    message,
    muted,
    section,
    styled,
    success,
    warning,
)
from agent_cli.ui.styles import THEME
from agent_cli.ui.tables import table

__all__ = [
    "THEME",
    "blocks",
    "bullets",
    "error",
    "fields",
    "heading",
    "home",
    "indented",
    "info",
    "is_unicode",
    "make_console",
    "message",
    "muted",
    "plain",
    "section",
    "styled",
    "success",
    "supports_color",
    "table",
    "warning",
]

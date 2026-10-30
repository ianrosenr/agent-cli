"""Slash-command registration and dispatch."""

from agent_cli.commands.builtin import build_registry
from agent_cli.commands.registry import (
    Command,
    CommandContext,
    CommandHandler,
    CommandRegistry,
    ExitShell,
)

__all__ = [
    "Command",
    "CommandContext",
    "CommandHandler",
    "CommandRegistry",
    "ExitShell",
    "build_registry",
]

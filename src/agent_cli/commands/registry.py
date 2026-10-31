"""Command definitions, the registry, and slash-command dispatch.

This module must stay free of provider, harness, model-routing, repository-inspection, and
orchestration dependencies. Later capabilities reach handlers through ``CommandContext``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.console import RenderableType

from agent_cli.ui import error

if TYPE_CHECKING:
    from agent_cli.adapters import AdapterRegistry
    from agent_cli.config import ConfigService
    from agent_cli.state import TaskStore

SLASH = "/"

type CommandHandler = Callable[[CommandContext, str], RenderableType]
"""Handler signature: receives the context and the raw argument text, returns output: plain
text, or a renderable built with :mod:`agent_cli.ui`."""


@dataclass(frozen=True, slots=True)
class Command:
    """The single authoritative definition of a slash command."""

    name: str
    description: str
    handler: CommandHandler
    planned_in: str | None = None
    """Roadmap issue that will implement the command, while it is still a placeholder."""

    def __post_init__(self) -> None:
        if not self.name or self.name.startswith(SLASH) or any(c.isspace() for c in self.name):
            raise ValueError(f"Invalid command name: {self.name!r}")


@dataclass(frozen=True, slots=True)
class CommandContext:
    """What a handler may use. Later issues add application services here."""

    registry: CommandRegistry
    config: ConfigService | None = None
    """Configuration service; ``None`` when the shell runs without configuration."""
    read_line: Callable[[str], str] = input
    """Prompts for a line of input, for commands that ask follow-up questions."""
    write_line: Callable[[RenderableType], None] = print
    """Shows a line of output before the command returns its final response."""
    adapters: AdapterRegistry | None = None
    """Harness and provider adapters; ``None`` when none are available in this session."""
    tasks: TaskStore | None = None
    """Canonical task state; ``None`` outside a repository, where no task state exists."""
    orchestrate: Callable[[str], RenderableType] | None = None
    """Plans one line of ordinary input; ``None`` where orchestration cannot run."""
    reload_adapters: Callable[[], None] | None = None
    """Rebuilds ``adapters`` from the current configuration, after setup changes it."""


class ExitShell(Exception):
    """Raised by a handler to end the interactive shell."""


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    name: str
    args: str


class CommandRegistry:
    """Holds command definitions and dispatches slash-command input to them."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, command: Command) -> None:
        if command.name in self._commands:
            raise ValueError(f"Command already registered: {SLASH}{command.name}")
        self._commands[command.name] = command

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def __iter__(self) -> Iterator[Command]:
        return iter(self._commands.values())

    def __len__(self) -> int:
        return len(self._commands)

    def dispatch(self, line: str, context: CommandContext) -> RenderableType:
        """Run the command named in ``line``, which must be slash-command input."""
        parsed = parse_command(line)
        command = self.get(parsed.name)
        if command is None:
            return error(
                f"Unknown command: {SLASH}{parsed.name}.",
                f"Type {SLASH}help for available commands.",
            )
        return command.handler(context, parsed.args)


def is_command(line: str) -> bool:
    return line.startswith(SLASH)


def parse_command(line: str) -> ParsedCommand:
    """Split ``/name args`` into its name and argument text."""
    if not is_command(line):
        raise ValueError(f"Not a slash command: {line!r}")
    name, _, args = line[len(SLASH) :].partition(" ")
    return ParsedCommand(name=name, args=args.strip())

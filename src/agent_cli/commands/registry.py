"""Command definitions, the registry, and slash-command dispatch.

This module must stay free of provider, harness, model-routing, repository-inspection, and
orchestration dependencies. Later capabilities reach handlers through ``CommandContext``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

SLASH = "/"

type CommandHandler = Callable[[CommandContext, str], str]
"""Handler signature: receives the context and the raw argument text, returns output."""


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

    def dispatch(self, line: str, context: CommandContext) -> str:
        """Run the command named in ``line``, which must be slash-command input."""
        parsed = parse_command(line)
        command = self.get(parsed.name)
        if command is None:
            return (
                f"Unknown command: {SLASH}{parsed.name}. Type {SLASH}help for available commands."
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

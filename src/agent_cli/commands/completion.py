"""Slash-command completion for the interactive prompt.

Suggestions come from the same ``CommandRegistry`` that dispatches commands and lists them in
``/help``, so registering a command is all it takes to make it discoverable here.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent

from agent_cli.commands.registry import SLASH, Command, CommandRegistry, is_command


def matching_commands(registry: CommandRegistry, partial: str) -> list[Command]:
    """Commands whose name starts with ``partial``, then those that merely contain it, each
    group in registry order. Case-insensitive; an empty ``partial`` matches every command."""
    needle = partial.lower()
    prefix = [command for command in registry if command.name.lower().startswith(needle)]
    infix = [
        command for command in registry if command not in prefix and needle in command.name.lower()
    ]
    return prefix + infix


def unique_completion(registry: CommandRegistry, text: str) -> str | None:
    """The full command when ``text`` is a partial command name with exactly one match, else
    ``None``. Complete names, arguments, and ordinary text are left alone."""
    if not is_command(text) or " " in text:
        return None
    name = text[len(SLASH) :]
    if registry.get(name) is not None:
        return None
    matches = matching_commands(registry, name)
    return f"{SLASH}{matches[0].name}" if len(matches) == 1 else None


def command_key_bindings(registry: CommandRegistry) -> KeyBindings:
    """Enter on a partial command with a single match runs that command, so ``/rev`` is enough
    for ``/review`` without pressing Tab first."""
    bindings = KeyBindings()

    @bindings.add("enter")
    def accept(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        completed = unique_completion(registry, buffer.text)
        if completed is not None:
            buffer.text = completed
        buffer.validate_and_handle()

    return bindings


class CommandCompleter(Completer):
    """Completes the command name while the line is ``/`` plus a partial name, and hands the
    argument text to the command's own ``complete_args`` once a space follows the name.
    Ordinary text never gets suggestions."""

    def __init__(self, registry: CommandRegistry) -> None:
        self.registry = registry

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterator[Completion]:
        text = document.text_before_cursor
        if not is_command(text):
            return
        name, space, args = text[len(SLASH) :].partition(" ")
        if space:
            yield from self._argument_completions(name, args)
            return
        for command in matching_commands(self.registry, name):
            yield Completion(
                f"{SLASH}{command.name}",
                start_position=-len(text),
                display_meta=command.description,
            )

    def _argument_completions(self, name: str, args: str) -> Iterable[Completion]:
        command = self.registry.get(name)
        if command is None or command.complete_args is None:
            return ()
        return (
            Completion(candidate, start_position=-len(args))
            for candidate in command.complete_args(args)
        )

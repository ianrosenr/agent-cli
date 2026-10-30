from __future__ import annotations

import pytest

from agent_cli.commands import (
    Command,
    CommandContext,
    CommandRegistry,
    ExitShell,
    build_registry,
)
from agent_cli.commands.registry import parse_command

REQUIRED_COMMANDS = {
    "help",
    "exit",
    "startup",
    "status",
    "budget",
    "skills",
    "providers",
    "models",
    "autopilot",
    "review",
    "diff",
}


@pytest.fixture
def context() -> CommandContext:
    return CommandContext(registry=build_registry())


def test_registry_contains_every_required_command() -> None:
    assert {command.name for command in build_registry()} == REQUIRED_COMMANDS


def test_help_lists_every_registered_command_and_description(context: CommandContext) -> None:
    output = context.registry.dispatch("/help", context)
    for command in context.registry:
        line = next(line for line in output.splitlines() if f"/{command.name} " in line)
        assert command.description in line


def test_help_reflects_newly_registered_commands(context: CommandContext) -> None:
    context.registry.register(Command("extra", "An extra command.", lambda ctx, args: ""))
    assert "/extra" in context.registry.dispatch("/help", context)


def test_dispatch_calls_handler_with_args() -> None:
    calls: list[str] = []

    def handler(context: CommandContext, args: str) -> str:
        calls.append(args)
        return "ran"

    registry = CommandRegistry()
    registry.register(Command("echo", "Echo.", handler))
    context = CommandContext(registry=registry)

    assert registry.dispatch("/echo  hello world ", context) == "ran"
    assert calls == ["hello world"]


@pytest.mark.parametrize("name", sorted(REQUIRED_COMMANDS - {"help", "exit"}))
def test_placeholder_commands_say_not_implemented(context: CommandContext, name: str) -> None:
    output = context.registry.dispatch(f"/{name}", context)
    assert output.startswith("Not implemented yet:")
    assert "V1-" in output


@pytest.mark.parametrize("line", ["/nope", "/", "/HELP"])
def test_unknown_command_is_reported(context: CommandContext, line: str) -> None:
    output = context.registry.dispatch(line, context)
    assert output.startswith(f"Unknown command: {line}.")
    assert "/help" in output


def test_exit_command_signals_shell_exit(context: CommandContext) -> None:
    with pytest.raises(ExitShell):
        context.registry.dispatch("/exit", context)


def test_duplicate_registration_is_rejected() -> None:
    registry = CommandRegistry()
    registry.register(Command("a", "A.", lambda ctx, args: ""))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(Command("a", "Again.", lambda ctx, args: ""))


@pytest.mark.parametrize("name", ["", "/help", "two words"])
def test_invalid_command_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="Invalid command name"):
        Command(name, "Bad.", lambda ctx, args: "")


def test_parse_command_splits_name_and_args() -> None:
    parsed = parse_command("/review  --strict now")
    assert (parsed.name, parsed.args) == ("review", "--strict now")


def test_parse_command_rejects_non_command_input() -> None:
    with pytest.raises(ValueError):
        parse_command("help")

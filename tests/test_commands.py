from __future__ import annotations

import pytest

from agent_cli.commands import (
    Command,
    CommandContext,
    CommandRegistry,
    ExitShell,
    build_registry,
)
from agent_cli.commands.builtin import help_command
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
        line = next(line for line in output.splitlines() if line.startswith(f"  /{command.name} "))
        assert command.description in line


def test_help_explains_how_to_use_the_cli_before_listing_commands(
    context: CommandContext,
) -> None:
    output = context.registry.dispatch("/help", context)
    assert output.startswith("Agent CLI help")
    assert output.index("> prompt") < output.index("Commands:")


def test_help_separates_working_commands_from_planned_ones(context: CommandContext) -> None:
    output = context.registry.dispatch("/help", context)
    available, planned = output.split("Coming in later issues:")
    assert "/help" in available and "/exit" in available
    for command in context.registry:
        if command.planned_in is not None:
            assert f"/{command.name}" not in available
            line = next(
                line for line in planned.splitlines() if line.startswith(f"  /{command.name} ")
            )
            assert line.endswith(f"  {command.planned_in}")


def test_help_omits_planned_section_when_nothing_is_planned() -> None:
    registry = CommandRegistry()
    registry.register(Command("help", "Help.", help_command))
    context = CommandContext(registry=registry)
    assert "Coming in later issues" not in registry.dispatch("/help", context)


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


IMPLEMENTED_COMMANDS = {"help", "exit", "startup", "providers"}


@pytest.mark.parametrize("name", sorted(REQUIRED_COMMANDS - IMPLEMENTED_COMMANDS))
def test_placeholder_commands_say_not_implemented(context: CommandContext, name: str) -> None:
    command = context.registry.get(name)
    assert command is not None and command.planned_in is not None
    output = context.registry.dispatch(f"/{name}", context)
    assert output.startswith("Not implemented yet:")
    assert output.endswith(f"arrives in {command.planned_in}.")


@pytest.mark.parametrize("line", ["/nope", "/", "/HELP"])
def test_unknown_command_is_reported(context: CommandContext, line: str) -> None:
    output = context.registry.dispatch(line, context)
    assert output.startswith(f"Unknown command: {line}.")
    assert "/help" in output


@pytest.mark.parametrize("name", sorted(IMPLEMENTED_COMMANDS))
def test_implemented_commands_are_not_placeholders(context: CommandContext, name: str) -> None:
    command = context.registry.get(name)
    assert command is not None and command.planned_in is None


def test_startup_is_listed_as_available_in_help(context: CommandContext) -> None:
    available = context.registry.dispatch("/help", context).split("Coming in later issues:")[0]
    assert "/startup" in available


def test_status_remains_owned_by_v1_05(context: CommandContext) -> None:
    command = context.registry.get("status")
    assert command is not None and command.planned_in == "V1-05"
    assert context.registry.dispatch("/status", context).endswith("arrives in V1-05.")


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

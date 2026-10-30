"""Built-in slash commands.

Apart from ``/help``, these are placeholders that establish the user-facing shell contract.
Each names the roadmap issue that will implement it (see docs/BUILD_GUIDE.md, Build Order).
"""

from __future__ import annotations

from agent_cli.commands.registry import (
    SLASH,
    Command,
    CommandContext,
    CommandHandler,
    CommandRegistry,
    ExitShell,
)


def help_command(context: CommandContext, args: str) -> str:
    commands = sorted(context.registry, key=lambda command: command.name)
    width = max(len(command.name) for command in commands) + len(SLASH)
    lines = ["Available commands:"]
    lines += [f"  {SLASH + command.name:<{width}}  {command.description}" for command in commands]
    return "\n".join(lines)


def exit_command(context: CommandContext, args: str) -> str:
    raise ExitShell


def placeholder(capability: str, issue: str) -> CommandHandler:
    def handler(context: CommandContext, args: str) -> str:
        return f"Not implemented yet: {capability} arrives in {issue}."

    return handler


BUILTIN_COMMANDS: tuple[Command, ...] = (
    Command("help", "List available commands.", help_command),
    Command("exit", "Exit Agent CLI.", exit_command),
    Command(
        "startup",
        "Guided setup for harnesses, providers, and budgets.",
        placeholder("configuration and guided setup", "V1-03"),
    ),
    Command(
        "providers",
        "Show configured providers and harnesses.",
        placeholder("provider and harness adapters", "V1-04"),
    ),
    Command(
        "status",
        "Show current task state.",
        placeholder("persistent task state", "V1-05"),
    ),
    Command(
        "skills",
        "Show available skills and routing.",
        placeholder("skill discovery and routing", "V1-06"),
    ),
    Command(
        "models",
        "Show the model registry and role thresholds.",
        placeholder("the model registry", "V1-07"),
    ),
    Command(
        "budget",
        "Show budget limits and spend.",
        placeholder("budget guardrails", "V1-08"),
    ),
    Command(
        "autopilot",
        "Choose the implementation mode.",
        placeholder("implementation modes", "V1-09"),
    ),
    Command(
        "diff",
        "Show the diff produced by delegated work.",
        placeholder("worktrees and worker diffs", "V1-11"),
    ),
    Command(
        "review",
        "Run code review on delegated work.",
        placeholder("code review", "V1-12"),
    ),
)


def build_registry() -> CommandRegistry:
    registry = CommandRegistry()
    for command in BUILTIN_COMMANDS:
        registry.register(command)
    return registry

"""Built-in slash commands.

Apart from ``/help``, ``/exit``, and ``/startup``, these are placeholders that establish the
user-facing shell contract. Each names the roadmap issue that will implement it (see
docs/BUILD_GUIDE.md, Build Order).
"""

from __future__ import annotations

from agent_cli.commands.registry import (
    SLASH,
    Command,
    CommandContext,
    CommandRegistry,
    ExitShell,
)

HELP_INTRO = """\
Agent CLI help

Describe what you want done at the > prompt, in plain language:

  > add expiration support to our cache layer

Agent CLI will plan the work, route it to the right model and coding agent, review the
result, and show you the diff. Task handling arrives in V1-09; until then it replies with
a placeholder.

Commands start with / and control Agent CLI itself. Ctrl-C clears the current line;
Ctrl-D or /exit quits."""


def help_command(context: CommandContext, args: str) -> str:
    commands = list(context.registry)
    width = max(len(command.name) for command in commands) + len(SLASH)
    described = max(len(command.description) for command in commands)

    def row(command: Command) -> str:
        name = SLASH + command.name
        if command.planned_in is None:
            return f"  {name:<{width}}  {command.description}"
        return f"  {name:<{width}}  {command.description:<{described}}  {command.planned_in}"

    available = [row(command) for command in commands if command.planned_in is None]
    planned = [row(command) for command in commands if command.planned_in is not None]
    sections = [HELP_INTRO, "Commands:\n" + "\n".join(available)]
    if planned:
        sections.append("Coming in later issues:\n" + "\n".join(planned))
    return "\n\n".join(sections)


def exit_command(context: CommandContext, args: str) -> str:
    raise ExitShell


def startup_command(context: CommandContext, args: str) -> str:
    if context.config is None:
        return "Configuration is unavailable in this session."
    from agent_cli.guided_setup import GuidedSetup

    return GuidedSetup(context.config, context.read_line, context.write_line).run()


def planned(name: str, description: str, capability: str, issue: str) -> Command:
    """A placeholder command for a capability that a later roadmap issue implements."""

    def handler(context: CommandContext, args: str) -> str:
        return f"Not implemented yet: {capability} arrives in {issue}."

    return Command(name, description, handler, planned_in=issue)


BUILTIN_COMMANDS: tuple[Command, ...] = (
    Command("help", "Show this help.", help_command),
    Command("exit", "Exit Agent CLI.", exit_command),
    Command("startup", "Guided setup for budgets, workers, review, and Git.", startup_command),
    planned(
        "providers",
        "Show configured providers and harnesses.",
        "provider and harness adapters",
        "V1-04",
    ),
    planned("status", "Show current task state.", "persistent task state", "V1-05"),
    planned("skills", "Show available skills and routing.", "skill discovery and routing", "V1-06"),
    planned(
        "models", "Show the model registry and role thresholds.", "the model registry", "V1-07"
    ),
    planned("budget", "Show budget limits and spend.", "budget guardrails", "V1-08"),
    planned("autopilot", "Choose the implementation mode.", "implementation modes", "V1-09"),
    planned(
        "diff", "Show the diff produced by delegated work.", "worktrees and worker diffs", "V1-11"
    ),
    planned("review", "Run code review on delegated work.", "code review", "V1-12"),
)


def build_registry() -> CommandRegistry:
    registry = CommandRegistry()
    for command in BUILTIN_COMMANDS:
        registry.register(command)
    return registry

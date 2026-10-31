"""Built-in slash commands.

Apart from ``/help``, ``/exit``, ``/startup``, ``/providers``, ``/status``, ``/skills``,
``/models``, ``/budget``, ``/autopilot``, and ``/cancel``, these are placeholders that establish the
user-facing shell contract. Each names the roadmap issue that will implement it (see
docs/BUILD_GUIDE.md, Build Order).
"""

from __future__ import annotations

from agent_cli.adapters import AdapterRegistry, adapter_status
from agent_cli.commands.registry import (
    SLASH,
    Command,
    CommandContext,
    CommandRegistry,
    ExitShell,
)
from agent_cli.config import Config, ConfigError, ConfigService, Layer
from agent_cli.guided_setup import GuidedSetup
from agent_cli.state import TaskStateError, status_lines
from agent_cli.state.status import NO_ACTIVE_TASK
from agent_cli.terms import ImplementationMode

HELP_INTRO = """\
Agent CLI help

Describe what you want done at the > prompt, in plain language:

  > add expiration support to our cache layer

Agent CLI inspects the repository, plans the work as role-assigned subtasks, and writes
short specs for review. When a decision only you can make is open, it asks one question,
and your next line is the answer. Implementation, review, and diffs arrive in later issues.

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


def adapters_of(context: CommandContext) -> AdapterRegistry:
    return context.adapters if context.adapters is not None else AdapterRegistry()


def startup_command(context: CommandContext, args: str) -> str:
    if context.config is None:
        return "Configuration is unavailable in this session."
    adapters = adapters_of(context)
    return GuidedSetup(
        context.config,
        context.read_line,
        context.write_line,
        lambda config: adapter_status(adapters, config),
    ).run()


def providers_command(context: CommandContext, args: str) -> str:
    config = Config()
    if context.config is not None:
        try:
            config = context.config.resolve()
        except ConfigError as error:
            return f"Cannot read configuration: {error}"
    return "\n".join(adapter_status(adapters_of(context), config))


def status_command(context: CommandContext, args: str) -> str:
    if context.tasks is None:
        return NO_ACTIVE_TASK
    try:
        return "\n".join(status_lines(context.tasks))
    except TaskStateError as error:
        return f"Cannot read task state: {error}"


def skills_command(context: CommandContext, args: str) -> str:
    # Imported on use: skill routing (and the repository layer it reads) stays out of startup.
    from agent_cli.skills import skills_lines

    return "\n".join(skills_lines(context.config, context.tasks))


def models_command(context: CommandContext, args: str) -> str:
    # Imported on use, like /skills: model routing stays out of startup.
    from agent_cli.models import models_lines

    return "\n".join(models_lines(context.config, context.adapters))


def budget_command(context: CommandContext, args: str) -> str:
    # Imported on use, like /skills: the spend ledger stays out of startup.
    from agent_cli.budget import budget_lines

    return "\n".join(budget_lines(context.config, context.tasks))


NO_QUESTION = "There is no open question to cancel."


def cancel_command(context: CommandContext, args: str) -> str:
    """Withdraw the open planning question, so the next line starts a new request."""
    tasks = context.tasks
    if tasks is None or not tasks.exists():
        return NO_QUESTION
    try:
        session = tasks.latest_session()
        question = None if session is None else tasks.pending_planning_question(session.id)
        if question is None:
            return NO_QUESTION
        tasks.cancel_planning_question(question.id)
    except TaskStateError as error:
        return f"Cannot update task state: {error}"
    return f"Canceled the question and its request ({question.task_id}). Type a new request."


AUTOPILOT_SETTINGS: dict[str, ImplementationMode] = {
    "on": "autopilot",
    "off": "checkpoint",
    "plan-only": "plan-only",
}
AUTOPILOT_LAYERS = {"global": Layer.GLOBAL, "project": Layer.PROJECT}
MODE_DESCRIPTIONS: dict[ImplementationMode, str] = {
    "autopilot": "make every change an approved spec authorizes without asking each time",
    "checkpoint": "ask before each batch of changes",
    "plan-only": "change no files; explain the plan instead",
}
AUTOPILOT_USAGE = "Usage: /autopilot [status | on | off | plan-only] [global | project]"


def autopilot_command(context: CommandContext, args: str) -> str:
    """Show or save ``implementation.mode``. The workflow that applies it is V1-10's."""
    if context.config is None:
        return "Configuration is unavailable in this session."
    words = args.split()
    if not words or words == ["status"]:
        return autopilot_status(context.config)
    setting, *scope = words
    if (
        setting not in AUTOPILOT_SETTINGS
        or len(scope) > 1
        or not set(scope) <= AUTOPILOT_LAYERS.keys()
    ):
        return AUTOPILOT_USAGE
    layer = AUTOPILOT_LAYERS[scope[0]] if scope else Layer.GLOBAL
    if context.config.paths.file_for(layer) is None:
        return "There is no project here: run Agent CLI inside a Git repository, or use global."
    mode = AUTOPILOT_SETTINGS[setting]
    try:
        path = context.config.save(layer, {"implementation": {"mode": mode}})
        effective, source = implementation_mode(context.config)
    except ConfigError as error:
        return f"Cannot save configuration: {error}"
    lines = [f"Implementation mode set to {mode} in {path}."]
    if effective != mode:
        lines.append(f"The {source} still sets {effective}, which takes precedence here.")
    return "\n".join(lines)


def autopilot_status(config: ConfigService) -> str:
    try:
        mode, source = implementation_mode(config)
    except ConfigError as error:
        return f"Cannot read configuration: {error}"
    width = max(len(name) for name in MODE_DESCRIPTIONS)
    return "\n".join(
        [
            f"Implementation mode: {mode} (from {source})",
            "",
            *(f"  {name:<{width}}  {text}" for name, text in MODE_DESCRIPTIONS.items()),
            "",
            "Change it with /autopilot on, off (checkpoint), or plan-only; add global or "
            "project to choose where it is saved (default: global).",
        ]
    )


def implementation_mode(config: ConfigService) -> tuple[ImplementationMode, str]:
    """The effective mode and the layer that sets it."""
    mode = config.resolve().implementation.mode
    layers = {
        Layer.SESSION: config.session,
        Layer.PROJECT: config.read(Layer.PROJECT),
        Layer.GLOBAL: config.read(Layer.GLOBAL),
    }
    for layer, data in layers.items():
        if "mode" in data.get("implementation", {}):
            return mode, config.source(layer)
    return mode, "defaults"


def planned(name: str, description: str, capability: str, issue: str) -> Command:
    """A placeholder command for a capability that a later roadmap issue implements."""

    def handler(context: CommandContext, args: str) -> str:
        return f"Not implemented yet: {capability} arrives in {issue}."

    return Command(name, description, handler, planned_in=issue)


BUILTIN_COMMANDS: tuple[Command, ...] = (
    Command("help", "Show this help.", help_command),
    Command("exit", "Exit Agent CLI.", exit_command),
    Command("startup", "Guided setup for budgets, workers, review, and Git.", startup_command),
    Command(
        "providers", "Show configured and detected harnesses and providers.", providers_command
    ),
    Command("status", "Show current task state.", status_command),
    Command("skills", "Show available skills and routing.", skills_command),
    Command("models", "Show the model registry and role thresholds.", models_command),
    Command("budget", "Show paid-API spend, reservations, and limits.", budget_command),
    Command("autopilot", "Show or choose the implementation mode.", autopilot_command),
    Command("cancel", "Cancel the open planning question and its request.", cancel_command),
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

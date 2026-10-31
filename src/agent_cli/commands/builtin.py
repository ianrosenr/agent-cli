"""Built-in slash commands.

Apart from ``/help``, ``/exit``, ``/startup``, ``/login``, ``/providers``, ``/status``, ``/skills``,
``/models``, ``/budget``, ``/autopilot``, and ``/cancel``, these are placeholders that establish the
user-facing shell contract. Each names the roadmap issue that will implement it (see
docs/BUILD_GUIDE.md, Build Order).
"""

from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text

from agent_cli.adapters import AdapterRegistry, providers_view
from agent_cli.commands.registry import (
    SLASH,
    Command,
    CommandContext,
    CommandRegistry,
    ExitShell,
)
from agent_cli.config import Config, ConfigError, ConfigService, Layer
from agent_cli.guided_setup import GuidedSetup
from agent_cli.login import HarnessLoginService
from agent_cli.state import TaskStateError, status_view
from agent_cli.state.status import NO_ACTIVE_TASK
from agent_cli.terms import ImplementationMode
from agent_cli.ui import (
    blocks,
    error,
    fields,
    heading,
    muted,
    section,
    styled,
    success,
    table,
    warning,
)

HELP_TITLE = "Agent CLI help"
HELP_EXAMPLE = "  > add expiration support to our cache layer"
HELP_INTRO = "Describe what you want done at the > prompt, in plain language:"
HELP_BODY = """\
Agent CLI inspects the repository, plans the work as role-assigned subtasks, and writes
short specs for review. When a decision only you can make is open, it asks one question,
and your next line is the answer. Implementation, review, and diffs arrive in later issues.

Commands start with / and control Agent CLI itself. Ctrl-C clears the current line;
Ctrl-D or /exit quits."""


def help_command(context: CommandContext, args: str) -> RenderableType:
    available = [command for command in context.registry if command.planned_in is None]
    planned = [command for command in context.registry if command.planned_in is not None]
    return blocks(
        heading(HELP_TITLE),
        Text(HELP_INTRO),
        styled(HELP_EXAMPLE, "accent"),
        Text(HELP_BODY),
        section("Commands", command_table(available)),
        section("Coming in later issues", command_table(planned)) if planned else None,
    )


def command_table(commands: list[Command]) -> RenderableType:
    rows = [
        (SLASH + command.name, command.description, command.planned_in or "")
        for command in commands
    ]
    return table(("", "", ""), rows, styles=("command", None, "muted"), header=False)


def exit_command(context: CommandContext, args: str) -> str:
    raise ExitShell


def adapters_of(context: CommandContext) -> AdapterRegistry:
    return context.adapters if context.adapters is not None else AdapterRegistry()


UNAVAILABLE = "Configuration is unavailable in this session."


def startup_command(context: CommandContext, args: str) -> RenderableType:
    if context.config is None:
        return error(UNAVAILABLE)
    adapters = adapters_of(context)
    setup = GuidedSetup(
        context.config,
        context.read_line,
        context.write_line,
        lambda config: providers_view(adapters, config),
    )
    result = setup.run()
    if not setup.saved:
        return result
    if context.reload_adapters is not None:
        context.reload_adapters()
    try:
        config = context.config.resolve()
    except ConfigError as problem:
        return blocks(result, error("Cannot read saved configuration.", str(problem)))
    if "subscription" not in config.access.preference:
        return result
    login = HarnessLoginService(
        adapters_of(context),
        config,
        context.read_line,
        context.write_line,
    ).offer()
    return blocks(result, login) if login is not None else result


def login_command(context: CommandContext, args: str) -> RenderableType:
    if context.config is None:
        return error(UNAVAILABLE)
    try:
        config = context.config.resolve()
    except ConfigError as problem:
        return error("Cannot read configuration.", str(problem))
    if "subscription" not in config.access.preference:
        return info(
            "Subscription access is disabled.",
            "Run /startup to add subscription to your access preference.",
        )
    return HarnessLoginService(
        adapters_of(context),
        config,
        context.read_line,
        context.write_line,
    ).run(args)


def providers_command(context: CommandContext, args: str) -> RenderableType:
    config = Config()
    if context.config is not None:
        try:
            config = context.config.resolve()
        except ConfigError as problem:
            return error("Cannot read configuration.", str(problem))
    return providers_view(adapters_of(context), config)


def status_command(context: CommandContext, args: str) -> RenderableType:
    if context.tasks is None:
        return NO_ACTIVE_TASK
    try:
        return status_view(context.tasks)
    except TaskStateError as problem:
        return error("Cannot read task state.", str(problem))


def skills_command(context: CommandContext, args: str) -> RenderableType:
    # Imported on use: skill routing (and the repository layer it reads) stays out of startup.
    from agent_cli.skills import skills_view

    return skills_view(context.config, context.tasks)


def models_command(context: CommandContext, args: str) -> RenderableType:
    # Imported on use, like /skills: model routing stays out of startup.
    from agent_cli.models import models_view

    return models_view(context.config, context.adapters)


def budget_command(context: CommandContext, args: str) -> RenderableType:
    # Imported on use, like /skills: the spend ledger stays out of startup.
    from agent_cli.budget import budget_view

    return budget_view(context.config, context.tasks)


NO_QUESTION = "There is no open question to cancel."


def cancel_command(context: CommandContext, args: str) -> RenderableType:
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
    except TaskStateError as problem:
        return error("Cannot update task state.", str(problem))
    return success(
        f"Canceled the question and its request ({question.task_id}).", "Type a new request."
    )


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


def autopilot_command(context: CommandContext, args: str) -> RenderableType:
    """Show or save ``implementation.mode``. The workflow that applies it is V1-10's."""
    if context.config is None:
        return error(UNAVAILABLE)
    words = args.split()
    if not words or words == ["status"]:
        return autopilot_status(context.config)
    setting, *scope = words
    if (
        setting not in AUTOPILOT_SETTINGS
        or len(scope) > 1
        or not set(scope) <= AUTOPILOT_LAYERS.keys()
    ):
        return error("Unrecognized arguments.", AUTOPILOT_USAGE)
    layer = AUTOPILOT_LAYERS[scope[0]] if scope else Layer.GLOBAL
    if context.config.paths.file_for(layer) is None:
        return error(
            "There is no project here.",
            "Run Agent CLI inside a Git repository, or use global.",
        )
    mode = AUTOPILOT_SETTINGS[setting]
    try:
        path = context.config.save(layer, {"implementation": {"mode": mode}})
        effective, source = implementation_mode(context.config)
    except ConfigError as problem:
        return error("Cannot save configuration.", str(problem))
    saved = success(f"Implementation mode set to {mode} in {path}.")
    if effective == mode:
        return saved
    return blocks(
        saved,
        warning(f"The {source} still sets {effective}, which takes precedence here."),
    )


AUTOPILOT_HINT = (
    "Change it with /autopilot on, off (checkpoint), or plan-only; add global or "
    "project to choose where it is saved (default: global)."
)


def autopilot_status(config: ConfigService) -> RenderableType:
    try:
        mode, source = implementation_mode(config)
    except ConfigError as problem:
        return error("Cannot read configuration.", str(problem))
    current = Text("Implementation mode: ")
    current.append(mode, style="value")
    current.append(f" (from {source})", style="muted")
    modes = [(name, text) for name, text in MODE_DESCRIPTIONS.items()]
    return blocks(
        current,
        fields(modes, indent=1, label_style="command"),
        muted(AUTOPILOT_HINT),
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

    def handler(context: CommandContext, args: str) -> RenderableType:
        return Text(f"Not implemented yet: {capability} arrives in {issue}.", style="muted")

    return Command(name, description, handler, planned_in=issue)


BUILTIN_COMMANDS: tuple[Command, ...] = (
    Command("help", "Show this help.", help_command),
    Command("exit", "Exit Agent CLI.", exit_command),
    Command("startup", "Guided setup for access, harnesses, budgets, review, and Git.", startup_command),
    Command("login", "Sign into enabled subscription harnesses.", login_command),
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

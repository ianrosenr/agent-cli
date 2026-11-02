"""Process startup and the interactive shell loop.

Ordinary text goes to one continuous supervisor conversation. The supervisor answers directly or
delegates scout/ship work; slash commands remain secondary controls.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from rich.console import Group, RenderableType
from rich.text import Text

from agent_cli import __version__
from agent_cli.adapters import AdapterRegistry
from agent_cli.commands import CommandContext, CommandRegistry, ExitShell, build_registry
from agent_cli.commands.registry import is_command
from agent_cli.config import ConfigError, ConfigPaths, ConfigService
from agent_cli.gateway import NO_REPOSITORY
from agent_cli.runtime import default_adapters, provider_estimators, session_adapters
from agent_cli.state import TaskStore
from agent_cli.ui import blocks, error, home, info, is_unicode, make_console

if TYPE_CHECKING:
    from agent_cli.gateway.service import SupervisorService
    from agent_cli.orchestration import OrchestrationService
    from agent_cli.orchestration.domain import TaskBrief

PROMPT = "> "
NO_ORCHESTRATION = NO_REPOSITORY

type ReadLine = Callable[[str], str]
type WriteLine = Callable[[RenderableType], None]


def handle_input(line: str, context: CommandContext) -> RenderableType | None:
    """Return the response to one line of input, or ``None`` when there is nothing to show."""
    text = line.strip()
    if not text:
        return None
    if is_command(text):
        return context.registry.dispatch(text, context)
    if context.respond is None:
        return NO_ORCHESTRATION
    return context.respond(text)


def run_shell(
    registry: CommandRegistry,
    read_line: ReadLine,
    write_line: WriteLine,
    startup: RenderableType,
    config: ConfigService | None = None,
    adapters: AdapterRegistry | None = None,
    tasks: TaskStore | None = None,
    reload_adapters: Callable[[], str | None] | None = None,
    auto_startup: bool = False,
) -> int:
    """Show startup and read input. Ctrl-C exits idle shell or cancels foreground work."""
    context = CommandContext(
        registry=registry,
        config=config,
        read_line=read_line,
        write_line=write_line,
        adapters=adapters,
        tasks=tasks,
        respond=gateway(config, adapters, tasks, read_line, write_line),
        reload_adapters=reload_adapters,
    )
    write_line(startup)
    if auto_startup:
        onboarding = context.registry.dispatch("/startup", context)
        write_line(Group(onboarding, Text("")))
    while True:
        try:
            line = read_line(PROMPT)
        except (KeyboardInterrupt, EOFError):
            return 0
        try:
            response = handle_input(line, context)
        except ExitShell:
            return 0
        except KeyboardInterrupt:
            write_line(info("Canceled current operation."))
            continue
        if response is not None:
            write_line(Group(response, Text("")))  # a blank line before the next prompt


def orchestrator(
    config: ConfigService | None,
    adapters: AdapterRegistry | None,
    tasks: TaskStore | None,
    read_line: ReadLine | None = None,
    write_line: WriteLine | None = None,
) -> Callable[[str, TaskBrief | None], RenderableType] | None:
    """The orchestration entry point, built on first use so planning stays out of startup."""
    if config is None or tasks is None:
        return None
    service: OrchestrationService | None = None

    def ensure_service() -> OrchestrationService:
        nonlocal service
        if service is None:
            from agent_cli.orchestration import OrchestrationService

            registry = adapters or AdapterRegistry()
            login_harnesses = None
            if read_line is not None and write_line is not None:
                from agent_cli.login import HarnessLoginService

                def login_harnesses(harness_ids: tuple[str, ...]) -> bool:
                    try:
                        settings = config.resolve()
                    except ConfigError:
                        return False
                    if "subscription" not in settings.access.preference:
                        return False
                    return HarnessLoginService(registry, settings, read_line, write_line).repair(
                        harness_ids
                    )

            worker_notify = (
                (lambda message: write_line(info("Background work finished.", message)))
                if write_line is not None
                else None
            )
            service = OrchestrationService(
                tasks,
                config,
                registry,
                estimator=provider_estimators(registry),
                login_harnesses=login_harnesses,
                worker_notify=worker_notify,
            )
        return service

    def orchestrate(text: str, handoff: TaskBrief | None = None) -> RenderableType:
        return ensure_service().handle(text, handoff)

    # Resume already persisted queues at startup, without creating a new task.
    if tasks.exists():
        queued = any(
            task.parent_id is not None and task.status.value == "pending"
            for session in tasks.sessions()
            if session.open
            for task in tasks.tasks(session.id)
        )
        if queued:
            ensure_service().resume_queued()
    return orchestrate


def gateway(
    config: ConfigService | None,
    adapters: AdapterRegistry | None,
    tasks: TaskStore | None,
    read_line: ReadLine | None = None,
    write_line: WriteLine | None = None,
) -> Callable[[str], RenderableType] | None:
    """The root-prompt supervisor: answer directly or delegate scout/ship repository work.
    Built on first use so semantic intake stays out of startup."""
    if config is None:
        return None
    registry = adapters or AdapterRegistry()
    orchestrate = orchestrator(config, registry, tasks, read_line, write_line)
    service: SupervisorService | None = None

    def respond(text: str) -> RenderableType:
        nonlocal service
        if service is None:
            from agent_cli.gateway.service import SupervisorService

            service = SupervisorService(
                config,
                registry,
                orchestrate,
                tasks,
                estimator=provider_estimators(registry),
            )
        return service.handle(text)

    return respond


def reloader(config: ConfigService, adapters: AdapterRegistry) -> Callable[[], str | None]:
    """Atomically rebuild adapters from current files; invalid edits keep the old runtime."""

    def reload() -> str | None:
        from agent_cli.adapters.factories import AdapterFactoryError

        try:
            resolved = config.resolve()
            candidate = default_adapters(resolved)
        except (ConfigError, AdapterFactoryError) as problem:
            return str(problem)
        adapters.reset(candidate)
        return None

    return reload


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agent-cli",
        description="A local-first orchestration and routing control plane for coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point for ``agent-cli`` and ``python -m agent_cli``."""
    parse_args(argv)
    config = ConfigService(ConfigPaths.discover())
    console = make_console()
    try:
        registry = build_registry()
        problem = blocks(config_problem(config), adapter_problem(config))
        root = config.paths.project_root
        tasks = None if root is None else TaskStore.for_repository(root)
        recovery = None
        if tasks is not None:
            from agent_cli.workers import reconcile_orphaned_workers

            reconciled = reconcile_orphaned_workers(tasks)
            if reconciled:
                recovery = info(
                    f"Recovered {reconciled} interrupted background worker(s).",
                    "They were marked failed and were not replayed.",
                )
        adapters = session_adapters(config)
        interactive = sys.stdin.isatty()
        first_run = needs_onboarding(config, interactive)
        maintenance = None
        if interactive and not first_run and problem is None:
            from agent_cli.harness_lifecycle import HarnessLifecycleService

            settings = config.resolve()
            cache = config.paths.global_file.parent / "state" / "harness-updates.json"
            maintenance = HarnessLifecycleService(
                adapters, settings, cache_file=cache
            ).refresh_latest()
        startup = blocks(
            home(registry, unicode=is_unicode(console)),
            problem,
            recovery,
            maintenance,
        )
        return run_shell(
            registry,
            terminal_reader(registry),
            console.print,
            startup,
            config,
            adapters,
            tasks,
            reloader(config, adapters),
            first_run,
        )
    except KeyboardInterrupt:
        return 130


def needs_onboarding(config: ConfigService, interactive: bool) -> bool:
    """First interactive launch only: any existing file means the user has configured Agent CLI."""
    if not interactive:
        return False
    paths = (config.paths.global_file, config.paths.project_file)
    return not any(path is not None and path.exists() for path in paths)


def adapter_problem(config: ConfigService) -> RenderableType | None:
    """Report unknown adapter types/profiles without partially constructing runtime."""
    from agent_cli.adapters.factories import AdapterFactoryError

    try:
        default_adapters(config.resolve())
    except ConfigError:
        return None
    except AdapterFactoryError as problem:
        return error("Adapter configuration error.", str(problem))
    return None


def config_problem(config: ConfigService) -> RenderableType | None:
    """Report invalid configuration up front instead of silently ignoring it."""
    try:
        config.resolve()
    except ConfigError as problem:
        return error("Configuration error.", str(problem))
    return None


PROMPT_STYLES = {
    "prompt": "ansicyan bold",
    "question": "bold",
    "default": "ansicyan",
    "choices": "ansibrightblack",
}
QUESTION = re.compile(r"^(?P<question>.*?)(?P<default> \[[^\]]*\])?(?P<choices> \([^)]*\))?: $")


def prompt_fragments(prompt: str) -> list[tuple[str, str]]:
    """``prompt`` as prompt_toolkit style fragments: the shell prompt in the accent color, and a
    setup question with its ``[default]`` and ``(choices)`` set apart."""
    if prompt == PROMPT:
        return [("class:prompt", prompt)]
    match = QUESTION.match(prompt)
    if match is None:
        return [("", prompt)]
    fragments = [("class:question", match["question"])]
    if match["default"]:
        fragments.append(("class:default", match["default"]))
    if match["choices"]:
        fragments.append(("class:choices", match["choices"]))
    return [*fragments, ("", ": ")]


def terminal_reader(registry: CommandRegistry) -> ReadLine:
    """Use prompt_toolkit on a terminal; plain ``input`` when stdin is piped. prompt_toolkit
    honors ``NO_COLOR`` for the prompt styles itself. The shell prompt completes slash commands
    from ``registry``; follow-up questions get no completion."""
    if not sys.stdin.isatty():
        return input
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.styles import Style

    from agent_cli.commands.completion import CommandCompleter, command_key_bindings

    session: PromptSession[str] = PromptSession(style=Style.from_dict(PROMPT_STYLES))
    completer = CommandCompleter(registry)
    bindings = command_key_bindings(registry)

    def read_line(prompt: str) -> str:
        # Set on the session: ``prompt(completer=None)`` would keep the previous completer.
        shell = prompt == PROMPT
        session.completer = completer if shell else None
        session.key_bindings = bindings if shell else None
        with patch_stdout():
            return session.prompt(FormattedText(prompt_fragments(prompt)))

    return read_line

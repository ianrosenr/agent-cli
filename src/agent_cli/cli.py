"""Process startup and the interactive shell loop.

Ordinary text is the primary input and goes to orchestration; slash commands are secondary
controls, recognized only when input begins with ``/``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from agent_cli import __version__
from agent_cli.adapters import AdapterRegistry
from agent_cli.commands import CommandContext, CommandRegistry, ExitShell, build_registry
from agent_cli.commands.registry import is_command
from agent_cli.config import ConfigError, ConfigPaths, ConfigService
from agent_cli.startup import render_startup, supports_color
from agent_cli.state import TaskStore

if TYPE_CHECKING:
    from agent_cli.orchestration import OrchestrationService

PROMPT = "> "
NO_ORCHESTRATION = (
    "Agent CLI plans work inside a Git repository. Start it from your project's directory."
)

type ReadLine = Callable[[str], str]
type WriteLine = Callable[[str], None]


def handle_input(line: str, context: CommandContext) -> str | None:
    """Return the response to one line of input, or ``None`` when there is nothing to show."""
    text = line.strip()
    if not text:
        return None
    if is_command(text):
        return context.registry.dispatch(text, context)
    if context.orchestrate is None:
        return NO_ORCHESTRATION
    return context.orchestrate(text)


def run_shell(
    registry: CommandRegistry,
    read_line: ReadLine,
    write_line: WriteLine,
    startup: str,
    config: ConfigService | None = None,
    adapters: AdapterRegistry | None = None,
    tasks: TaskStore | None = None,
) -> int:
    """Show ``startup``, then read and answer input until EOF. Ctrl-C discards the line."""
    context = CommandContext(
        registry=registry,
        config=config,
        read_line=read_line,
        write_line=write_line,
        adapters=adapters,
        tasks=tasks,
        orchestrate=orchestrator(config, adapters, tasks),
    )
    write_line(startup)
    while True:
        try:
            line = read_line(PROMPT)
        except KeyboardInterrupt:
            continue
        except EOFError:
            return 0
        try:
            response = handle_input(line, context)
        except ExitShell:
            return 0
        if response is not None:
            write_line(response)


def orchestrator(
    config: ConfigService | None, adapters: AdapterRegistry | None, tasks: TaskStore | None
) -> Callable[[str], str] | None:
    """The orchestration entry point, built on first use so planning stays out of startup."""
    if config is None or tasks is None:
        return None
    service: OrchestrationService | None = None

    def orchestrate(text: str) -> str:
        nonlocal service
        if service is None:
            from agent_cli.orchestration import OrchestrationService

            service = OrchestrationService(tasks, config, adapters or AdapterRegistry())
        return service.handle(text)

    return orchestrate


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
    try:
        startup = render_startup(color=supports_color(sys.stdout))
        problem = config_problem(config)
        if problem:
            startup += f"\n{problem}\n"
        root = config.paths.project_root
        tasks = None if root is None else TaskStore.for_repository(root)
        return run_shell(
            build_registry(), terminal_reader(), print, startup, config, default_adapters(), tasks
        )
    except KeyboardInterrupt:
        return 130


def default_adapters() -> AdapterRegistry:
    """The harness and provider adapters available to every session.

    Real adapters are registered here as they are added (Pi is the first candidate). Adapter
    construction must stay cheap: detection happens later, through ``readiness()``.
    """
    return AdapterRegistry()


def config_problem(config: ConfigService) -> str | None:
    """Report invalid configuration up front instead of silently ignoring it."""
    try:
        config.resolve()
    except ConfigError as error:
        return f"Configuration error: {error}"
    return None


def terminal_reader() -> ReadLine:
    """Use prompt_toolkit on a terminal; plain ``input`` when stdin is piped."""
    if not sys.stdin.isatty():
        return input
    from prompt_toolkit import PromptSession

    session: PromptSession[str] = PromptSession()
    return session.prompt

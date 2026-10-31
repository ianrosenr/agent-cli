"""Process startup and the interactive shell loop.

Ordinary text is the primary input and goes to orchestration; slash commands are secondary
controls, recognized only when input begins with ``/``.
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
from agent_cli.state import TaskStore
from agent_cli.ui import blocks, error, home, is_unicode, make_console

if TYPE_CHECKING:
    from agent_cli.orchestration import OrchestrationService

PROMPT = "> "
NO_ORCHESTRATION = (
    "Agent CLI plans work inside a Git repository. Start it from your project's directory."
)

type ReadLine = Callable[[str], str]
type WriteLine = Callable[[RenderableType], None]


def handle_input(line: str, context: CommandContext) -> RenderableType | None:
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
    startup: RenderableType,
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
            write_line(Group(response, Text("")))  # a blank line before the next prompt


def orchestrator(
    config: ConfigService | None, adapters: AdapterRegistry | None, tasks: TaskStore | None
) -> Callable[[str], RenderableType] | None:
    """The orchestration entry point, built on first use so planning stays out of startup."""
    if config is None or tasks is None:
        return None
    service: OrchestrationService | None = None

    def orchestrate(text: str) -> RenderableType:
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
    console = make_console()
    try:
        registry = build_registry()
        startup = blocks(home(registry, unicode=is_unicode(console)), config_problem(config))
        root = config.paths.project_root
        tasks = None if root is None else TaskStore.for_repository(root)
        return run_shell(
            registry, terminal_reader(), console.print, startup, config, default_adapters(), tasks
        )
    except KeyboardInterrupt:
        return 130


def default_adapters() -> AdapterRegistry:
    """The harness and provider adapters available to every session.

    Real adapters are registered here as they are added (Pi is the first candidate). Adapter
    construction must stay cheap: detection happens later, through ``readiness()``.
    """
    return AdapterRegistry()


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


def terminal_reader() -> ReadLine:
    """Use prompt_toolkit on a terminal; plain ``input`` when stdin is piped. prompt_toolkit
    honors ``NO_COLOR`` for the prompt styles itself."""
    if not sys.stdin.isatty():
        return input
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.styles import Style

    session: PromptSession[str] = PromptSession(style=Style.from_dict(PROMPT_STYLES))

    def read_line(prompt: str) -> str:
        return session.prompt(FormattedText(prompt_fragments(prompt)))

    return read_line

"""Process startup and the interactive shell loop."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from agent_cli import __version__
from agent_cli.commands import CommandContext, CommandRegistry, ExitShell, build_registry
from agent_cli.commands.registry import is_command

PROMPT = "agent-cli> "
BANNER = "Agent CLI. Type /help for commands, /exit or Ctrl-D to quit."
NATURAL_LANGUAGE_PLACEHOLDER = "Orchestration is not implemented yet."

type ReadLine = Callable[[str], str]
type WriteLine = Callable[[str], None]


def handle_input(line: str, context: CommandContext) -> str | None:
    """Return the response to one line of input, or ``None`` when there is nothing to show."""
    text = line.strip()
    if not text:
        return None
    if is_command(text):
        return context.registry.dispatch(text, context)
    return NATURAL_LANGUAGE_PLACEHOLDER


def run_shell(registry: CommandRegistry, read_line: ReadLine, write_line: WriteLine) -> int:
    """Read and answer input until EOF. Ctrl-C discards the current line."""
    context = CommandContext(registry=registry)
    write_line(BANNER)
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
    try:
        return run_shell(build_registry(), terminal_reader(), print)
    except KeyboardInterrupt:
        return 130


def terminal_reader() -> ReadLine:
    """Use prompt_toolkit on a terminal; plain ``input`` when stdin is piped."""
    if not sys.stdin.isatty():
        return input
    from prompt_toolkit import PromptSession

    session: PromptSession[str] = PromptSession()
    return session.prompt

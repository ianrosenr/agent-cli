from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_cli import cli
from agent_cli.commands import CommandContext, build_registry
from agent_cli.config import Config, ConfigPaths, ConfigService
from agent_cli.startup import TAGLINE, WORDMARK


@pytest.fixture
def context() -> CommandContext:
    return CommandContext(registry=build_registry())


@pytest.mark.parametrize("line", ["", "   ", "\t\n"])
def test_empty_input_is_ignored(context: CommandContext, line: str) -> None:
    assert cli.handle_input(line, context) is None


def test_natural_language_returns_placeholder(context: CommandContext) -> None:
    output = cli.handle_input("add a health endpoint", context)
    assert output == cli.NATURAL_LANGUAGE_PLACEHOLDER


def test_natural_language_mentioning_slash_is_not_dispatched(context: CommandContext) -> None:
    output = cli.handle_input("explain what /help does", context)
    assert output == cli.NATURAL_LANGUAGE_PLACEHOLDER


@pytest.mark.parametrize("line", ["help", "please /help", "status/budget"])
def test_slash_commands_only_start_with_slash(context: CommandContext, line: str) -> None:
    assert cli.handle_input(line, context) == cli.NATURAL_LANGUAGE_PLACEHOLDER


def test_slash_input_is_trimmed_and_dispatched(context: CommandContext) -> None:
    output = cli.handle_input("  /help  ", context)
    assert output is not None
    assert output.startswith("Agent CLI help")


def scripted(*events: str | type[BaseException]) -> cli.ReadLine:
    """A ``read_line`` that yields lines or raises exceptions, ending with EOF."""
    queue: Iterator[str | type[BaseException]] = iter(events)

    def read_line(prompt: str) -> str:
        event = next(queue, EOFError)
        if isinstance(event, str):
            return event
        raise event

    return read_line


STARTUP = "<startup screen>"


def test_startup_screen_is_shown_before_the_first_prompt() -> None:
    events: list[str] = []

    def read_line(prompt: str) -> str:
        events.append(f"prompt {prompt!r}")
        raise EOFError

    cli.run_shell(build_registry(), read_line, events.append, STARTUP)

    assert events == [STARTUP, f"prompt {cli.PROMPT!r}"]


def test_first_input_is_a_task_with_no_mode_selection() -> None:
    output: list[str] = []
    cli.run_shell(build_registry(), scripted("add a health endpoint"), output.append, STARTUP)

    assert output == [STARTUP, cli.NATURAL_LANGUAGE_PLACEHOLDER]


def test_shell_dispatches_and_exits_cleanly_on_eof() -> None:
    output: list[str] = []
    code = cli.run_shell(
        build_registry(), scripted("", "/review", "/bogus", "hi"), output.append, STARTUP
    )

    assert code == 0
    assert output[0] == STARTUP
    assert output[1].startswith("Not implemented yet:")
    assert output[2].startswith("Unknown command: /bogus.")
    assert output[3] == cli.NATURAL_LANGUAGE_PLACEHOLDER
    assert len(output) == 4


def test_shell_continues_after_ctrl_c() -> None:
    output: list[str] = []
    code = cli.run_shell(
        build_registry(), scripted(KeyboardInterrupt, "hi"), output.append, STARTUP
    )

    assert code == 0
    assert output == [STARTUP, cli.NATURAL_LANGUAGE_PLACEHOLDER]


def test_shell_exits_on_exit_command() -> None:
    output: list[str] = []
    code = cli.run_shell(build_registry(), scripted("/exit", "hi"), output.append, STARTUP)

    assert code == 0
    assert output == [STARTUP]


def test_main_runs_shell_through_prompt_toolkit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeSession:
        def prompt(self, message: str) -> str:
            raise EOFError

    monkeypatch.setattr("prompt_toolkit.PromptSession", FakeSession)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert cli.main([]) == 0
    assert capsys.readouterr().out.lstrip().startswith(WORDMARK[0])


def test_piped_input_runs_without_a_terminal() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "agent_cli"],
        input="/help\n\nhello\n",
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.lstrip().startswith(WORDMARK[0])
    assert TAGLINE in result.stdout
    assert "\033[" not in result.stdout
    assert "Agent CLI help" in result.stdout
    assert cli.NATURAL_LANGUAGE_PLACEHOLDER in result.stdout
    assert result.stderr == ""


def test_python_dash_m_uses_the_same_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "agent_cli", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "agent-cli 0.1.0"


def test_shell_passes_its_io_and_config_to_commands(tmp_path: Path) -> None:
    service = ConfigService(ConfigPaths(tmp_path / "config.yaml", None))
    output: list[str] = []
    code = cli.run_shell(
        build_registry(), scripted("/startup", *[""] * 12), output.append, STARTUP, service
    )
    assert code == 0
    assert output[1].startswith("Agent CLI setup")
    assert output[-1] == f"Settings saved to {tmp_path / 'config.yaml'}."
    assert service.resolve() == Config()


def test_main_reports_invalid_configuration_at_startup(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = isolated_home / ".agent-cli" / "config.yaml"
    config.parent.mkdir()
    config.write_text("agents:\n  max_parallel: 2\n")
    monkeypatch.chdir(isolated_home)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(EOFError))
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert f"Configuration error: global config ({config}): agents: unknown key" in out


def test_startup_imports_only_the_shell_and_command_layer() -> None:
    code = (
        "import sys; import agent_cli.cli; "
        "print('\\n'.join(m for m in sys.modules if m.startswith('agent_cli')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert set(result.stdout.split()) == {
        "agent_cli",
        "agent_cli.adapters",
        "agent_cli.adapters.domain",
        "agent_cli.adapters.harness",
        "agent_cli.adapters.provider",
        "agent_cli.adapters.registry",
        "agent_cli.adapters.status",
        "agent_cli.cli",
        "agent_cli.commands",
        "agent_cli.commands.builtin",
        "agent_cli.commands.registry",
        "agent_cli.config",
        "agent_cli.config.model",
        "agent_cli.config.schema",
        "agent_cli.config.service",
        "agent_cli.guided_setup",
        "agent_cli.startup",
        "agent_cli.state",
        "agent_cli.state.migrations",
        "agent_cli.state.models",
        "agent_cli.state.paths",
        "agent_cli.state.status",
        "agent_cli.state.task_store",
        "agent_cli.terms",
    }

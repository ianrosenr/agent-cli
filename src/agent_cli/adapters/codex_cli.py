"""Codex CLI compatibility surface backed by the generic CLI harness adapter."""

from __future__ import annotations

from collections.abc import Mapping

from agent_cli.adapters.cli_harness import (
    CliHarnessAdapter,
    CommandRunner,
    DirectModelRunner,
    GitRunner,
    InteractiveRunner,
    Process,
    ProcessLauncher,
    launch_process,
    run_command,
    run_direct_model,
    run_git,
    run_interactive,
)
from agent_cli.adapters.cli_profiles import CODEX_CLI
from agent_cli.config.model import HarnessConfig

HARNESS_ID = CODEX_CLI.id
DISPLAY_NAME = CODEX_CLI.display_name
DEFAULT_COMMAND = CODEX_CLI.default_command
SUBSCRIPTION_PROVIDER_ID = CODEX_CLI.provider_id
BILLING_OVERRIDE_ENV = CODEX_CLI.env_remove


def CodexCliHarnessAdapter(
    config: HarnessConfig | None = None,
    runner: CommandRunner = run_command,
    launcher: ProcessLauncher = launch_process,
    git_runner: GitRunner = run_git,
    environ: Mapping[str, str] | None = None,
    interactive_runner: InteractiveRunner = run_interactive,
    direct_model_runner: DirectModelRunner = run_direct_model,
) -> CliHarnessAdapter:
    """Compatibility constructor; behavior lives in the generic adapter plus profile data."""
    kwargs = {}
    if environ is not None:
        kwargs["environ"] = environ
    return CliHarnessAdapter(
        HARNESS_ID,
        CODEX_CLI,
        config or HarnessConfig(profile=HARNESS_ID),
        runner,
        launcher,
        git_runner,
        interactive_runner=interactive_runner,
        direct_model_runner=direct_model_runner,
        **kwargs,
    )

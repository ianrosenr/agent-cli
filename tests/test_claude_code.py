from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import pytest

from agent_cli.adapters import (
    AdapterError,
    ErrorKind,
    ReadinessState,
    Selection,
    WorkerRequest,
    WorkerStatus,
)
from agent_cli.adapters.claude_code import (
    BILLING_OVERRIDE_ENV,
    HARNESS_ID,
    SUBSCRIPTION_PROVIDER_ID,
    ClaudeCodeHarnessAdapter,
    Process,
)
from agent_cli.config import Config
from agent_cli.config.model import HarnessConfig
from agent_cli.runtime import default_adapters


@dataclass
class FakeCommands:
    auth: dict[str, Any] = field(
        default_factory=lambda: {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "subscriptionType": "Max",
        }
    )
    version: str = "2.1.307 (Claude Code)\n"
    missing: bool = False
    auth_returncode: int = 0
    calls: list[tuple[tuple[str, ...], dict[str, str], float]] = field(default_factory=list)

    def __call__(
        self,
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, dict(env), timeout))
        if self.missing:
            raise FileNotFoundError(argv[0])
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, self.version, "")
        return subprocess.CompletedProcess(
            argv,
            self.auth_returncode,
            json.dumps(self.auth),
            "",
        )


@dataclass
class FakeProcess:
    returncode: int | None = 0
    terminated: bool = False
    killed: bool = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("claude", timeout or 0)
        return self.returncode


@dataclass
class FakeLauncher:
    output: str = field(
        default_factory=lambda: json.dumps(
            {
                "result": "implemented the requested change",
                "session_id": "session-123",
                "is_error": False,
                "usage": {"input_tokens": 120, "output_tokens": 30},
            }
        )
    )
    error: str = ""
    returncode: int | None = 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    process: FakeProcess | None = None

    def __call__(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str],
        stdin: IO[str],
        stdout: IO[str],
        stderr: IO[str],
    ) -> Process:
        prompt = stdin.read()
        stdout.write(self.output)
        stdout.flush()
        stderr.write(self.error)
        stderr.flush()
        self.process = FakeProcess(self.returncode)
        self.calls.append({"argv": argv, "cwd": cwd, "env": dict(env), "prompt": prompt})
        return self.process


@dataclass
class FakeGit:
    fail: bool = False

    def __call__(self, argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        code = 1 if self.fail else 0
        if "--name-only" in argv:
            stdout = "src/example.py\n"
        elif "ls-files" in argv:
            stdout = "new.txt\n"
        else:
            stdout = "diff --git a/src/example.py b/src/example.py\n"
        return subprocess.CompletedProcess(argv, code, stdout, "")


def request(tmp_path: Path, **changes: Any) -> WorkerRequest:
    values: dict[str, Any] = {
        "task_id": "task-1",
        "role": "implementer",
        "spec": "Change only the cache expiration behavior.",
        "working_directory": tmp_path,
        "repository": "agent-cli",
        "skills": ("testing policy", "interfaces policy"),
        "model": Selection.pin("claude-sonnet-5-5"),
        "provider": Selection.pin(SUBSCRIPTION_PROVIDER_ID),
        "harness": Selection.pin(HARNESS_ID),
        "mode": "autopilot",
    }
    values.update(changes)
    return WorkerRequest(**values)


def adapter(
    commands: FakeCommands | None = None,
    launcher: FakeLauncher | None = None,
    git: FakeGit | None = None,
    config: HarnessConfig | None = None,
) -> ClaudeCodeHarnessAdapter:
    return ClaudeCodeHarnessAdapter(
        config or HarnessConfig(),
        commands or FakeCommands(),
        launcher or FakeLauncher(),
        git or FakeGit(),
        {"PATH": "/usr/bin"},
    )


def test_missing_executable_is_not_detected() -> None:
    readiness = adapter(commands=FakeCommands(missing=True)).readiness()
    assert readiness.state is ReadinessState.NOT_DETECTED


def test_subscription_login_is_ready() -> None:
    commands = FakeCommands()
    harness = adapter(commands=commands)

    readiness = harness.readiness()

    assert readiness.state is ReadinessState.READY
    assert "subscription auth (Max)" in readiness.reason
    assert harness.identity().version == "2.1.307 (Claude Code)"
    assert len(commands.calls) == 2


@pytest.mark.parametrize("name", sorted(BILLING_OVERRIDE_ENV))
def test_billing_override_fails_closed_before_running_claude(name: str) -> None:
    commands = FakeCommands()
    harness = ClaudeCodeHarnessAdapter(
        HarnessConfig(),
        commands,
        FakeLauncher(),
        FakeGit(),
        {"PATH": "/usr/bin", name: "configured"},
    )

    readiness = harness.readiness()

    assert readiness.state is ReadinessState.MISCONFIGURED
    assert f"unset {name}" in readiness.reason
    assert commands.calls == []


@pytest.mark.parametrize(
    "method",
    ["api_key", "api_key_helper", "third_party", "none"],
)
def test_non_subscription_auth_is_rejected(method: str) -> None:
    commands = FakeCommands(auth={"loggedIn": True, "authMethod": method})
    readiness = adapter(commands=commands).readiness()
    assert readiness.state is ReadinessState.AUTH_REQUIRED
    assert "subscription" in readiness.reason


def test_oauth_without_subscription_metadata_is_rejected() -> None:
    commands = FakeCommands(auth={"loggedIn": True, "authMethod": "oauth_token"})
    readiness = adapter(commands=commands).readiness()
    assert readiness.state is ReadinessState.AUTH_REQUIRED


def test_oauth_with_subscription_metadata_is_ready() -> None:
    commands = FakeCommands(
        auth={
            "loggedIn": True,
            "authMethod": "oauth_token",
            "subscriptionType": "Pro",
        }
    )
    readiness = adapter(commands=commands).readiness()
    assert readiness.state is ReadinessState.READY
    assert "subscription auth (Pro)" in readiness.reason


def test_signed_out_is_auth_required() -> None:
    commands = FakeCommands(
        auth={"loggedIn": False, "authMethod": "none"},
        auth_returncode=1,
    )
    readiness = adapter(commands=commands).readiness()
    assert readiness.state is ReadinessState.AUTH_REQUIRED
    assert "claude auth login" in readiness.reason


def test_start_uses_stdin_context_cwd_and_exact_selections(
    tmp_path: Path,
) -> None:
    launcher = FakeLauncher()
    harness = adapter(launcher=launcher)
    sent = request(tmp_path)

    handle = harness.start(sent)
    [call] = launcher.calls

    assert handle.harness_id == HARNESS_ID
    assert call["cwd"] == tmp_path
    assert sent.spec in call["prompt"]
    assert "testing policy" in call["prompt"]
    assert sent.spec not in call["argv"]
    assert call["argv"][-2:] == (
        "--model",
        "claude-sonnet-5-5",
    )
    permission_index = call["argv"].index("--permission-mode")
    assert call["argv"][permission_index + 1] == "auto"
    assert call["env"] == {"PATH": "/usr/bin"}

    assert harness.status(handle) is WorkerStatus.COMPLETED
    result = harness.result(handle)
    assert result.status is WorkerStatus.COMPLETED
    assert result.summary == "implemented the requested change"
    assert result.usage is not None
    assert (
        result.usage.input_tokens,
        result.usage.output_tokens,
    ) == (120, 30)
    assert result.usage.cost_usd is None
    assert result.usage.reference == "session-123"
    assert result.changes is not None
    assert result.changes.files == ("new.txt", "src/example.py")


def test_plan_only_uses_plan_permission_mode(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    harness = adapter(launcher=launcher)
    harness.start(request(tmp_path, mode="plan-only", model=None))
    argv = launcher.calls[0]["argv"]
    index = argv.index("--permission-mode")
    assert argv[index + 1] == "plan"
    assert "--model" not in argv


def test_checkpoint_batch_uses_auto_after_agent_cli_approves_it(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    harness = adapter(launcher=launcher)
    harness.start(request(tmp_path, mode="checkpoint"))
    argv = launcher.calls[0]["argv"]
    index = argv.index("--permission-mode")
    assert argv[index + 1] == "auto"


def test_wrong_provider_pin_is_never_substituted(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    harness = adapter(launcher=launcher)
    with pytest.raises(AdapterError) as raised:
        harness.start(
            request(
                tmp_path,
                provider=Selection.pin("anthropic"),
            )
        )
    assert raised.value.kind is ErrorKind.INCOMPATIBLE
    assert launcher.calls == []


def test_interrupt_terminates_a_running_worker(tmp_path: Path) -> None:
    launcher = FakeLauncher(returncode=None)
    harness = adapter(launcher=launcher)
    handle = harness.start(request(tmp_path))
    assert harness.status(handle) is WorkerStatus.RUNNING

    harness.interrupt(handle)

    assert launcher.process is not None
    assert launcher.process.terminated
    assert harness.status(handle) is WorkerStatus.CANCELED
    assert harness.result(handle).status is WorkerStatus.CANCELED


def test_process_failure_does_not_leak_vendor_output(
    tmp_path: Path,
) -> None:
    launcher = FakeLauncher(
        output="",
        error="secret task text and raw vendor diagnostics",
        returncode=7,
    )
    harness = adapter(launcher=launcher)
    result = harness.result(harness.start(request(tmp_path)))
    assert result.status is WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.kind is ErrorKind.HARNESS_ERROR
    assert "secret task text" not in result.error.message
    assert "raw vendor diagnostics" not in result.error.message


def test_invalid_json_is_a_normalized_failure(tmp_path: Path) -> None:
    harness = adapter(launcher=FakeLauncher(output="not-json"))
    result = harness.result(harness.start(request(tmp_path)))
    assert result.status is WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.kind is ErrorKind.HARNESS_ERROR


def test_diff_failure_is_normalized(tmp_path: Path) -> None:
    harness = adapter(git=FakeGit(fail=True))
    handle = harness.start(request(tmp_path))
    with pytest.raises(AdapterError) as raised:
        harness.diff(handle)
    assert raised.value.kind is ErrorKind.HARNESS_ERROR


def test_custom_command_is_used_for_detection_and_launch(
    tmp_path: Path,
) -> None:
    commands = FakeCommands()
    launcher = FakeLauncher()
    harness = adapter(
        commands=commands,
        launcher=launcher,
        config=HarnessConfig(command="/opt/claude/bin/claude"),
    )
    harness.start(request(tmp_path))
    assert commands.calls[0][0][0] == "/opt/claude/bin/claude"
    assert launcher.calls[0]["argv"][0] == "/opt/claude/bin/claude"


def test_runtime_registers_claude_code_without_persisting_auth() -> None:
    harness = default_adapters(Config()).harness(HARNESS_ID)
    assert isinstance(harness, ClaudeCodeHarnessAdapter)

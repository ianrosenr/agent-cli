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
from agent_cli.adapters.codex_cli import (
    BILLING_OVERRIDE_ENV,
    HARNESS_ID,
    SUBSCRIPTION_PROVIDER_ID,
    CodexCliHarnessAdapter,
    Process,
)
from agent_cli.config import Config
from agent_cli.config.model import HarnessConfig
from agent_cli.runtime import default_adapters


@dataclass
class FakeCommands:
    version: str = "codex-cli 0.120.0\n"
    login_status: str = "Logged in using ChatGPT\n"
    missing: bool = False
    login_returncode: int = 0
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
        return subprocess.CompletedProcess(argv, self.login_returncode, "", self.login_status)


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
            raise subprocess.TimeoutExpired("codex", timeout or 0)
        return self.returncode


def codex_output(
    summary: str = "implemented the requested change",
    input_tokens: int = 120,
    output_tokens: int = 30,
) -> str:
    events = [
        {"type": "thread.started", "thread_id": "thread-123"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": summary},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": 20,
                "output_tokens": output_tokens,
            },
        },
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


@dataclass
class FakeLauncher:
    output: str = field(default_factory=codex_output)
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
        "model": Selection.pin("gpt-5.3-codex"),
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
) -> CodexCliHarnessAdapter:
    return CodexCliHarnessAdapter(
        config or HarnessConfig(),
        commands or FakeCommands(),
        launcher or FakeLauncher(),
        git or FakeGit(),
        {"PATH": "/usr/bin"},
    )


def test_missing_executable_is_not_detected() -> None:
    readiness = adapter(commands=FakeCommands(missing=True)).readiness()
    assert readiness.state is ReadinessState.NOT_DETECTED


def test_chatgpt_login_is_ready_and_auth_route_is_forced() -> None:
    commands = FakeCommands()
    harness = adapter(commands=commands)

    readiness = harness.readiness()

    assert readiness.state is ReadinessState.READY
    assert "ChatGPT subscription auth" in readiness.reason
    assert harness.identity().version == "codex-cli 0.120.0"
    assert len(commands.calls) == 2
    auth_argv = commands.calls[1][0]
    assert 'forced_login_method="chatgpt"' in auth_argv
    assert 'model_provider="openai"' in auth_argv
    assert 'openai_base_url=""' in auth_argv
    assert auth_argv[-2:] == ("login", "status")


@pytest.mark.parametrize("name", sorted(BILLING_OVERRIDE_ENV))
def test_billing_override_fails_closed_before_running_codex(name: str) -> None:
    commands = FakeCommands()
    harness = CodexCliHarnessAdapter(
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
    ("status", "returncode"),
    [
        ("Not logged in\n", 1),
        ("API key configured\n", 0),
        ("Logged in using access token\n", 0),
    ],
)
def test_non_chatgpt_auth_is_rejected(status: str, returncode: int) -> None:
    readiness = adapter(
        commands=FakeCommands(login_status=status, login_returncode=returncode)
    ).readiness()
    assert readiness.state is ReadinessState.AUTH_REQUIRED
    assert "ChatGPT" in readiness.reason


def test_start_uses_stdin_context_cwd_exact_model_and_workspace_sandbox(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    harness = adapter(launcher=launcher)
    sent = request(tmp_path)

    handle = harness.start(sent)
    [call] = launcher.calls
    argv = call["argv"]

    assert handle.harness_id == HARNESS_ID
    assert call["cwd"] == tmp_path
    assert sent.spec in call["prompt"]
    assert "testing policy" in call["prompt"]
    assert sent.spec not in argv
    assert argv[0] == "codex"
    assert 'forced_login_method="chatgpt"' in argv
    assert 'model_provider="openai"' in argv
    assert 'openai_base_url=""' in argv
    exec_index = argv.index("exec")
    assert argv[exec_index : exec_index + 3] == ("exec", "--json", "--ephemeral")
    approval_index = argv.index("--ask-for-approval")
    assert argv[approval_index + 1] == "never"
    sandbox_index = argv.index("--sandbox")
    assert argv[sandbox_index + 1] == "workspace-write"
    model_index = argv.index("--model")
    assert argv[model_index + 1] == "gpt-5.3-codex"
    assert argv[-1] == "-"

    assert harness.status(handle) is WorkerStatus.COMPLETED
    result = harness.result(handle)
    assert result.status is WorkerStatus.COMPLETED
    assert result.summary == "implemented the requested change"
    assert result.usage is not None
    assert (result.usage.input_tokens, result.usage.output_tokens) == (120, 30)
    assert result.usage.cost_usd is None
    assert result.usage.reference == "thread-123"
    assert result.changes is not None
    assert result.changes.files == ("new.txt", "src/example.py")


def test_plan_only_uses_read_only_sandbox(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    harness = adapter(launcher=launcher)
    harness.start(request(tmp_path, mode="plan-only", model=None))
    argv = launcher.calls[0]["argv"]
    index = argv.index("--sandbox")
    assert argv[index + 1] == "read-only"
    assert "--model" not in argv


def test_checkpoint_batch_uses_workspace_write_after_agent_cli_approves_it(
    tmp_path: Path,
) -> None:
    launcher = FakeLauncher()
    harness = adapter(launcher=launcher)
    harness.start(request(tmp_path, mode="checkpoint"))
    argv = launcher.calls[0]["argv"]
    index = argv.index("--sandbox")
    assert argv[index + 1] == "workspace-write"


def test_wrong_provider_pin_is_never_substituted(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    harness = adapter(launcher=launcher)
    with pytest.raises(AdapterError) as raised:
        harness.start(request(tmp_path, provider=Selection.pin("openai-api")))
    assert raised.value.kind is ErrorKind.INCOMPATIBLE
    assert launcher.calls == []


def test_jsonl_uses_last_agent_message_and_sums_completed_turn_usage(tmp_path: Path) -> None:
    events = [
        {"type": "thread.started", "thread_id": "thread-abc"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "first"},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "final"},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 20, "output_tokens": 3},
        },
    ]
    harness = adapter(
        launcher=FakeLauncher(output="\n".join(json.dumps(event) for event in events))
    )
    result = harness.result(harness.start(request(tmp_path)))
    assert result.summary == "final"
    assert result.usage is not None
    assert (result.usage.input_tokens, result.usage.output_tokens) == (30, 5)
    assert result.usage.reference == "thread-abc"


def test_partial_usage_is_left_unknown(tmp_path: Path) -> None:
    events = [
        {"type": "thread.started", "thread_id": "thread-partial"},
        {
            "type": "turn.completed",
            "usage": {"output_tokens": 7},
        },
    ]
    harness = adapter(
        launcher=FakeLauncher(output="\n".join(json.dumps(event) for event in events))
    )
    result = harness.result(harness.start(request(tmp_path)))
    assert result.usage is not None
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens == 7


@pytest.mark.parametrize(
    "output",
    [
        "not-json\n",
        json.dumps({"type": "error", "message": "secret raw diagnostic"}),
        json.dumps({"type": "turn.failed", "error": {"message": "secret"}}),
        json.dumps({"type": "thread.started", "thread_id": "thread-no-finish"}),
    ],
)
def test_bad_or_failed_jsonl_is_normalized_without_vendor_details(
    tmp_path: Path, output: str
) -> None:
    harness = adapter(launcher=FakeLauncher(output=output))
    result = harness.result(harness.start(request(tmp_path)))
    assert result.status is WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.kind is ErrorKind.HARNESS_ERROR
    assert "secret" not in result.error.message


def test_process_failure_does_not_leak_vendor_output(tmp_path: Path) -> None:
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


def test_interrupt_timeout_is_normalized(tmp_path: Path) -> None:
    class StubbornProcess(FakeProcess):
        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired("codex", timeout or 0)

    launcher = FakeLauncher(returncode=None)
    harness = adapter(launcher=launcher)
    handle = harness.start(request(tmp_path))
    stubborn = StubbornProcess(None)
    harness._workers[handle.token].process = stubborn

    with pytest.raises(AdapterError) as raised:
        harness.interrupt(handle)

    assert raised.value.kind is ErrorKind.HARNESS_ERROR
    assert raised.value.__cause__ is None
    assert stubborn.terminated and stubborn.killed


def test_diff_failure_is_normalized(tmp_path: Path) -> None:
    harness = adapter(git=FakeGit(fail=True))
    handle = harness.start(request(tmp_path))
    with pytest.raises(AdapterError) as raised:
        harness.diff(handle)
    assert raised.value.kind is ErrorKind.HARNESS_ERROR


def test_git_timeout_is_normalized(tmp_path: Path) -> None:
    def timeout_git(argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 5)

    harness = CodexCliHarnessAdapter(
        HarnessConfig(),
        FakeCommands(),
        FakeLauncher(),
        timeout_git,
        {"PATH": "/usr/bin"},
    )
    handle = harness.start(request(tmp_path))
    with pytest.raises(AdapterError) as raised:
        harness.diff(handle)
    assert raised.value.kind is ErrorKind.HARNESS_ERROR
    assert raised.value.__cause__ is None


def test_custom_command_is_used_for_detection_and_launch(tmp_path: Path) -> None:
    commands = FakeCommands()
    launcher = FakeLauncher()
    harness = adapter(
        commands=commands,
        launcher=launcher,
        config=HarnessConfig(command="/opt/codex/bin/codex"),
    )
    harness.start(request(tmp_path))
    assert commands.calls[0][0][0] == "/opt/codex/bin/codex"
    assert launcher.calls[0]["argv"][0] == "/opt/codex/bin/codex"


def test_runtime_registers_codex_without_persisting_auth() -> None:
    harness = default_adapters(Config()).harness(HARNESS_ID)
    assert isinstance(harness, CodexCliHarnessAdapter)

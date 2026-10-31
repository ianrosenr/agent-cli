"""Codex CLI harness adapter using the user's existing ChatGPT subscription login.

The adapter launches the installed Codex CLI in bounded headless mode. It never copies OpenAI
credentials and fails closed when API/workload-identity overrides are present, so this
subscription route cannot silently become pay-as-you-go API usage.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Protocol

from agent_cli.adapters.domain import (
    AdapterError,
    Capability,
    Changes,
    ErrorKind,
    Failure,
    HarnessCapabilities,
    HarnessIdentity,
    HarnessReadiness,
    ReadinessState,
    Usage,
    WorkerHandle,
    WorkerRequest,
    WorkerResult,
    WorkerStatus,
)
from agent_cli.adapters.harness import HarnessAdapter
from agent_cli.config.model import HarnessConfig

HARNESS_ID = "codex-cli"
DISPLAY_NAME = "Codex CLI"
DEFAULT_COMMAND = "codex"
SUBSCRIPTION_PROVIDER_ID = "openai-subscription"
READINESS_TIMEOUT_S = 5.0
INTERRUPT_TIMEOUT_S = 2.0

# Codex exec can prefer environment credentials over the stored ChatGPT login. This
# subscription-only harness refuses to run while any alternate billing/auth route is configured.
BILLING_OVERRIDE_ENV = frozenset(
    {
        "CODEX_API_KEY",
        "OPENAI_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "OPENAI_FEDERATION_RULE_ID",
        "OPENAI_IDENTITY_TOKEN_FILE",
        "OPENAI_WORKLOAD_IDENTITY_CONTEXT",
    }
)
CHATGPT_LOGIN_STATUS = "Logged in using ChatGPT"


class Process(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


type CommandRunner = Callable[
    [tuple[str, ...], Mapping[str, str], float], subprocess.CompletedProcess[str]
]
type ProcessLauncher = Callable[
    [tuple[str, ...], Path, Mapping[str, str], IO[str], IO[str], IO[str]], Process
]
type GitRunner = Callable[[tuple[str, ...], Path], subprocess.CompletedProcess[str]]


def run_command(
    argv: tuple[str, ...], env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def launch_process(
    argv: tuple[str, ...],
    cwd: Path,
    env: Mapping[str, str],
    stdin: IO[str],
    stdout: IO[str],
    stderr: IO[str],
) -> Process:
    process: subprocess.Popen[str] = subprocess.Popen(
        argv,
        cwd=cwd,
        env=dict(env),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=True,
        shell=False,
    )
    return process


def run_git(argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=READINESS_TIMEOUT_S,
        check=False,
    )


@dataclass(slots=True)
class _Worker:
    request: WorkerRequest
    process: Process
    stdout: IO[str]
    stderr: IO[str]
    canceled: bool = False
    final: WorkerResult | None = None


@dataclass(slots=True)
class CodexCliHarnessAdapter(HarnessAdapter):
    """Runs bounded workers through the installed Codex CLI ChatGPT subscription session."""

    config: HarnessConfig = field(default_factory=HarnessConfig)
    runner: CommandRunner = field(default=run_command, repr=False)
    launcher: ProcessLauncher = field(default=launch_process, repr=False)
    git_runner: GitRunner = field(default=run_git, repr=False)
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ), repr=False)
    _version: str | None = None
    _workers: dict[str, _Worker] = field(default_factory=dict, repr=False)

    def identity(self) -> HarnessIdentity:
        return HarnessIdentity(HARNESS_ID, DISPLAY_NAME, self._version)

    def readiness(self) -> HarnessReadiness:
        if not self.config.enabled:
            return HarnessReadiness(ReadinessState.MISCONFIGURED, "disabled in config")
        override = self.billing_override()
        if override is not None:
            return HarnessReadiness(
                ReadinessState.MISCONFIGURED,
                f"unset {override} to use the ChatGPT subscription harness",
            )

        command = self.command()
        env = dict(self.environ)
        try:
            version = self.runner((command, "--version"), env, READINESS_TIMEOUT_S)
        except FileNotFoundError:
            return HarnessReadiness(ReadinessState.NOT_DETECTED, f"{command!r} is not installed")
        except subprocess.TimeoutExpired:
            return HarnessReadiness(ReadinessState.UNAVAILABLE, "version check timed out")
        except OSError as error:
            return HarnessReadiness(
                ReadinessState.UNAVAILABLE,
                f"cannot run Codex CLI ({type(error).__name__})",
            )
        if version.returncode != 0:
            return HarnessReadiness(ReadinessState.UNAVAILABLE, "Codex CLI version check failed")
        self._version = first_line(version.stdout) or first_line(version.stderr) or "unknown version"

        try:
            auth = self.runner(self.auth_status_argv(), env, READINESS_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return HarnessReadiness(ReadinessState.UNAVAILABLE, "authentication check timed out")
        except OSError as error:
            return HarnessReadiness(
                ReadinessState.UNAVAILABLE,
                f"cannot check Codex CLI authentication ({type(error).__name__})",
            )
        status = "\n".join(part for part in (auth.stdout, auth.stderr) if part).strip()
        if auth.returncode != 0 or "Not logged in" in status:
            return HarnessReadiness(
                ReadinessState.AUTH_REQUIRED,
                "run codex login and sign in with ChatGPT",
            )
        if CHATGPT_LOGIN_STATUS not in status:
            return HarnessReadiness(
                ReadinessState.AUTH_REQUIRED,
                "Codex is not using ChatGPT login; run codex login and choose ChatGPT",
            )
        return HarnessReadiness(
            ReadinessState.READY,
            f"{self._version}; ChatGPT subscription auth",
        )

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            frozenset(
                {
                    Capability.START_WORKER,
                    Capability.INTERRUPT,
                    Capability.MODEL_SELECTION,
                    Capability.WORKING_DIRECTORY,
                    Capability.ISOLATED_WORKTREE,
                    Capability.DIFF,
                    Capability.USAGE,
                }
            ),
            providers=frozenset({SUBSCRIPTION_PROVIDER_ID}),
        )

    def start(self, request: WorkerRequest) -> WorkerHandle:
        self.check(request)
        blocking = self.readiness().blocking_error()
        if blocking is not None:
            raise blocking
        if not request.working_directory.is_dir():
            raise AdapterError(
                ErrorKind.INVALID_REQUEST,
                f"Codex CLI working directory does not exist: {request.working_directory}",
            )

        argv = self.worker_argv(request)
        # These files intentionally outlive this method: stdout/stderr belong to the running
        # worker and are closed when its final result is collected.
        prompt = tempfile.TemporaryFile(mode="w+", encoding="utf-8")  # noqa: SIM115
        stdout = tempfile.TemporaryFile(mode="w+", encoding="utf-8")  # noqa: SIM115
        stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8")  # noqa: SIM115
        prompt.write(render_worker_prompt(request))
        prompt.seek(0)
        try:
            process = self.launcher(
                argv,
                request.working_directory,
                dict(self.environ),
                prompt,
                stdout,
                stderr,
            )
        except FileNotFoundError:
            close_files(prompt, stdout, stderr)
            raise AdapterError(ErrorKind.NOT_FOUND, "Codex CLI executable disappeared") from None
        except OSError as error:
            close_files(prompt, stdout, stderr)
            raise AdapterError(
                ErrorKind.HARNESS_ERROR,
                f"cannot start Codex CLI ({type(error).__name__})",
            ) from None
        prompt.close()
        token = str(uuid.uuid4())
        self._workers[token] = _Worker(request, process, stdout, stderr)
        return WorkerHandle(HARNESS_ID, token)

    def status(self, handle: WorkerHandle) -> WorkerStatus:
        worker = self.worker(handle)
        if worker.final is not None:
            return worker.final.status
        if worker.canceled:
            return WorkerStatus.CANCELED
        code = worker.process.poll()
        if code is None:
            return WorkerStatus.RUNNING
        return WorkerStatus.COMPLETED if code == 0 else WorkerStatus.FAILED

    def result(self, handle: WorkerHandle) -> WorkerResult:
        worker = self.worker(handle)
        if worker.final is not None:
            return worker.final
        status = self.status(handle)
        if not status.finished:
            return WorkerResult(status)
        if status is WorkerStatus.CANCELED:
            worker.final = WorkerResult(WorkerStatus.CANCELED)
            close_files(worker.stdout, worker.stderr)
            return worker.final

        code = worker.process.poll()
        stdout = read_file(worker.stdout)
        # Consume but never expose stderr: vendor output may contain task context.
        _ = read_file(worker.stderr)
        close_files(worker.stdout, worker.stderr)
        if code != 0:
            worker.final = WorkerResult(
                WorkerStatus.FAILED,
                error=Failure(ErrorKind.HARNESS_ERROR, f"Codex CLI exited with status {code}"),
            )
            return worker.final

        parsed = parse_events(stdout)
        if isinstance(parsed, Failure):
            worker.final = WorkerResult(WorkerStatus.FAILED, error=parsed)
            return worker.final

        summary, usage = parsed
        changes = self.safe_changes(worker.request.working_directory)
        worker.final = WorkerResult(
            WorkerStatus.COMPLETED,
            summary=summary,
            changes=changes,
            usage=usage,
        )
        return worker.final

    def interrupt(self, handle: WorkerHandle) -> None:
        worker = self.worker(handle)
        if worker.final is not None or worker.process.poll() is not None:
            return
        try:
            worker.process.terminate()
            try:
                worker.process.wait(timeout=INTERRUPT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                worker.process.kill()
                worker.process.wait(timeout=INTERRUPT_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AdapterError(
                ErrorKind.HARNESS_ERROR,
                f"cannot interrupt Codex CLI ({type(error).__name__})",
            ) from None
        worker.canceled = True

    def diff(self, handle: WorkerHandle) -> Changes:
        return self.git_changes(self.worker(handle).request.working_directory)

    def usage(self, handle: WorkerHandle) -> Usage:
        result = self.result(handle)
        return result.usage or Usage()

    def command(self) -> str:
        return self.config.command or DEFAULT_COMMAND

    def billing_override(self) -> str | None:
        return next(
            (name for name in sorted(BILLING_OVERRIDE_ENV) if self.environ.get(name)),
            None,
        )

    def auth_status_argv(self) -> tuple[str, ...]:
        return (
            self.command(),
            "-c",
            'forced_login_method="chatgpt"',
            "-c",
            'model_provider="openai"',
            "login",
            "status",
        )

    def worker_argv(self, request: WorkerRequest) -> tuple[str, ...]:
        sandbox = "read-only" if request.mode == "plan-only" else "workspace-write"
        argv = [
            self.command(),
            "-c",
            'forced_login_method="chatgpt"',
            "-c",
            'model_provider="openai"',
            "exec",
            "--json",
            "--ephemeral",
            "--sandbox",
            sandbox,
        ]
        if request.model is not None:
            argv += ["--model", request.model.id]
        argv.append("-")
        return tuple(argv)

    def worker(self, handle: WorkerHandle) -> _Worker:
        if handle.harness_id != HARNESS_ID:
            raise AdapterError(ErrorKind.NOT_FOUND, "worker belongs to another harness")
        worker = self._workers.get(handle.token)
        if worker is None:
            raise AdapterError(ErrorKind.NOT_FOUND, "Codex CLI worker not found")
        return worker

    def git_changes(self, cwd: Path) -> Changes:
        try:
            tracked = self.git_runner(("git", "diff", "HEAD", "--name-only", "--"), cwd)
            untracked = self.git_runner(("git", "ls-files", "--others", "--exclude-standard"), cwd)
            diff = self.git_runner(("git", "diff", "--no-ext-diff", "HEAD", "--"), cwd)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AdapterError(
                ErrorKind.HARNESS_ERROR,
                f"cannot inspect Codex CLI git changes ({type(error).__name__})",
            ) from None
        if any(result.returncode != 0 for result in (tracked, untracked, diff)):
            raise AdapterError(ErrorKind.HARNESS_ERROR, "cannot inspect Codex CLI git changes")
        files = tuple(
            sorted(
                {
                    line
                    for line in (
                        *tracked.stdout.splitlines(),
                        *untracked.stdout.splitlines(),
                    )
                    if line
                }
            )
        )
        return Changes(files, diff.stdout or None)

    def safe_changes(self, cwd: Path) -> Changes | None:
        try:
            return self.git_changes(cwd)
        except AdapterError:
            return None


def render_worker_prompt(request: WorkerRequest) -> str:
    """Only bounded task/spec/policy context; never conversation history."""
    sections = [
        "You are a bounded implementation worker controlled by Agent CLI.",
        f"Role: {request.role}",
        f"Mode: {request.mode or 'autopilot'}",
        "Approved task/spec:\n" + request.spec,
    ]
    if request.repository:
        sections.append("Repository context:\n" + request.repository)
    if request.skills:
        sections.append("Selected skills/policy:\n" + "\n\n".join(request.skills))
    sections.append(
        "Rules:\n"
        "- Work only inside the supplied working directory.\n"
        "- Follow the approved spec exactly; do not broaden scope.\n"
        "- Preserve explicit model/provider/harness constraints.\n"
        "- Run relevant tests when permitted by the harness.\n"
        "- End with a concise summary of changes and verification."
    )
    return "\n\n".join(sections)


def parse_events(text: str) -> tuple[str, Usage] | Failure:
    """Extract the last agent message and accumulated token usage from Codex JSONL."""
    summary = ""
    reference: str | None = None
    input_tokens = 0
    output_tokens = 0
    saw_usage = False
    saw_completed = False

    for line in text.splitlines():
        if not line.strip():
            continue
        event = json_object(line)
        if event is None:
            return Failure(ErrorKind.HARNESS_ERROR, "Codex CLI returned invalid JSONL")
        kind = event.get("type")
        if kind in {"error", "turn.failed"}:
            return Failure(ErrorKind.HARNESS_ERROR, "Codex CLI reported an error")
        if kind == "thread.started":
            value = event.get("thread_id")
            if isinstance(value, str) and value:
                reference = value
        elif kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                value = item.get("text")
                if isinstance(value, str):
                    summary = value
        elif kind == "turn.completed":
            saw_completed = True
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                incoming = count(raw_usage.get("input_tokens"))
                outgoing = count(raw_usage.get("output_tokens"))
                if incoming is not None:
                    input_tokens += incoming
                    saw_usage = True
                if outgoing is not None:
                    output_tokens += outgoing
                    saw_usage = True

    if not saw_completed:
        return Failure(ErrorKind.HARNESS_ERROR, "Codex CLI ended without a completed turn")
    return (
        summary,
        Usage(
            input_tokens=input_tokens if saw_usage else None,
            output_tokens=output_tokens if saw_usage else None,
            reference=reference,
        ),
    )


def json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def read_file(file: IO[str]) -> str:
    file.seek(0)
    return file.read()


def close_files(*files: IO[str]) -> None:
    for file in files:
        if not file.closed:
            file.close()

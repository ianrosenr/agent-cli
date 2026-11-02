"""Reusable CLI harness adapter driven by declarative profiles."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal, Protocol

from agent_cli.adapters.domain import (
    AdapterError,
    Capability,
    Changes,
    ErrorKind,
    Failure,
    HarnessCapabilities,
    HarnessIdentity,
    HarnessReadiness,
    ModelDescriptor,
    ProviderIdentity,
    ProviderRequest,
    ProviderResponse,
    ReadinessState,
    Usage,
    WorkerAuthority,
    WorkerHandle,
    WorkerRequest,
    WorkerResult,
    WorkerStatus,
)
from agent_cli.adapters.harness import HarnessAdapter, version_matches
from agent_cli.config.model import HarnessConfig

READINESS_TIMEOUT_S = 5.0
INTERRUPT_TIMEOUT_S = 2.0
DIRECT_MODEL_TIMEOUT_S = 120.0

type OutputProtocol = Literal["json-result", "jsonl-agent"]
type CommandRunner = Callable[
    [tuple[str, ...], Mapping[str, str], float], subprocess.CompletedProcess[str]
]
type ProcessLauncher = Callable[
    [tuple[str, ...], Path, Mapping[str, str], IO[str], IO[str], IO[str]], "Process"
]
type GitRunner = Callable[[tuple[str, ...], Path], subprocess.CompletedProcess[str]]
type InteractiveRunner = Callable[
    [tuple[str, ...], Mapping[str, str]], subprocess.CompletedProcess[str]
]
type DirectModelRunner = Callable[
    [tuple[str, ...], Mapping[str, str], str, Path, float],
    subprocess.CompletedProcess[str],
]


class Process(Protocol):
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True, slots=True)
class AuthPattern:
    source: Literal["stdout", "stderr"] = "stdout"
    contains: str | None = None
    regexes: tuple[str, ...] = ()
    json_any: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class CliHarnessProfile:
    id: str
    display_name: str
    default_command: str
    provider_id: str
    provider_name: str
    auth_label: str
    auth_status_args: tuple[str, ...]
    login_args: tuple[str, ...]
    auth_pattern: AuthPattern
    env_remove: frozenset[str]
    output_protocol: OutputProtocol
    worker_base_args: tuple[str, ...]
    worker_plan_args: tuple[str, ...]
    worker_write_args: tuple[str, ...]
    worker_research_args: tuple[str, ...] | None = None
    worker_diagnostic_args: tuple[str, ...] | None = None
    worker_model_flag: str = "--model"
    worker_stdin_marker: str | None = None
    install_latest: tuple[str, ...] = ()
    install_exact: tuple[str, ...] = ()
    update_latest: tuple[str, ...] = ()
    update_exact: tuple[str, ...] = ()
    direct_model_id: str | None = None
    direct_model_name: str | None = None
    direct_context_limit: int | None = None
    direct_base_args: tuple[str, ...] = ()
    direct_model_flag: str = "--model"
    direct_stdin_marker: str | None = None


@dataclass(slots=True)
class _Worker:
    request: WorkerRequest
    process: Process
    stdout: IO[str]
    stderr: IO[str]
    canceled: bool = False
    final: WorkerResult | None = None


def run_command(
    argv: tuple[str, ...], env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, env=dict(env), capture_output=True, text=True, timeout=timeout, check=False
    )


def launch_process(
    argv: tuple[str, ...],
    cwd: Path,
    env: Mapping[str, str],
    stdin: IO[str],
    stdout: IO[str],
    stderr: IO[str],
) -> Process:
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=dict(env),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=True,
        shell=False,
    )


def run_interactive(
    argv: tuple[str, ...], env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, env=dict(env), text=True, check=False)


def run_direct_model(
    argv: tuple[str, ...],
    env: Mapping[str, str],
    prompt: str,
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        env=dict(env),
        cwd=cwd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def run_git(argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=READINESS_TIMEOUT_S,
        check=False,
    )


def safe_process_diagnostic(stderr: str, *sensitive: str, limit: int = 240) -> str:
    """Return a bounded one-line diagnostic without echoing known prompt/system text."""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", stderr)
    for value in sensitive:
        if value:
            text = text.replace(value, "[redacted input]")
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def effective_authority(request: WorkerRequest) -> WorkerAuthority:
    """Use explicit authority when present; preserve legacy mode semantics for old callers."""
    if request.authority is not None:
        return request.authority
    if request.mode == "plan-only":
        return WorkerAuthority()
    return WorkerAuthority(execute_commands=True, modify_source=True)


def authority_args(profile: CliHarnessProfile, authority: WorkerAuthority) -> tuple[str, ...]:
    """Translate generic authority to one declarative CLI profile."""
    if authority.modify_source:
        if not authority.execute_commands:
            raise AdapterError(
                ErrorKind.INCOMPATIBLE,
                f"{profile.display_name}: source-write authority requires command execution",
            )
        return profile.worker_write_args
    if authority.execute_commands:
        if profile.worker_diagnostic_args is None:
            raise AdapterError(
                ErrorKind.INCOMPATIBLE,
                f"{profile.display_name}: cannot enforce diagnostic authority",
            )
        return profile.worker_diagnostic_args
    if profile.worker_research_args is None:
        raise AdapterError(
            ErrorKind.INCOMPATIBLE,
            f"{profile.display_name}: cannot enforce research authority",
        )
    return profile.worker_research_args


@dataclass(slots=True)
class CliHarnessAdapter(HarnessAdapter):
    """One implementation for CLI harnesses; vendor differences live in the profile."""

    harness_id: str
    profile: CliHarnessProfile
    config: HarnessConfig = field(default_factory=HarnessConfig)
    runner: CommandRunner = field(default=run_command, repr=False)
    launcher: ProcessLauncher = field(default=launch_process, repr=False)
    git_runner: GitRunner = field(default=run_git, repr=False)
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ), repr=False)
    interactive_runner: InteractiveRunner = field(default=run_interactive, repr=False)
    direct_model_runner: DirectModelRunner = field(default=run_direct_model, repr=False)
    _version: str | None = None
    _workers: dict[str, _Worker] = field(default_factory=dict, repr=False)

    def identity(self) -> HarnessIdentity:
        return HarnessIdentity(self.harness_id, self.profile.display_name, self._version)

    def command(self) -> str:
        return self.config.command or self.profile.default_command

    def provider_id(self) -> str:
        if self.harness_id == self.profile.id:
            return self.profile.provider_id
        return f"{self.harness_id}-subscription"

    def subscription_env(self) -> dict[str, str]:
        env = dict(self.environ)
        for name in self.profile.env_remove:
            env.pop(name, None)
        return env

    def readiness(self) -> HarnessReadiness:
        if not self.config.enabled:
            return HarnessReadiness(ReadinessState.MISCONFIGURED, "disabled in config")
        command = self.command()
        env = self.subscription_env()
        try:
            version = self.runner((command, "--version"), env, READINESS_TIMEOUT_S)
        except FileNotFoundError:
            return HarnessReadiness(ReadinessState.NOT_DETECTED, f"{command!r} is not installed")
        except subprocess.TimeoutExpired:
            return HarnessReadiness(ReadinessState.UNAVAILABLE, "version check timed out")
        except OSError as error:
            return HarnessReadiness(
                ReadinessState.UNAVAILABLE,
                f"cannot run {self.profile.display_name} ({type(error).__name__})",
            )
        if version.returncode != 0:
            return HarnessReadiness(ReadinessState.UNAVAILABLE, "version check failed")
        self._version = (
            first_line(version.stdout) or first_line(version.stderr) or "unknown version"
        )
        if not version_matches(self.config.version, self._version):
            return HarnessReadiness(
                ReadinessState.MISCONFIGURED,
                f"configured version {self.config.version!r} does not match detected "
                f"{self._version!r}",
            )

        try:
            auth = self.runner(
                (command, *self.profile.auth_status_args),
                env,
                READINESS_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return HarnessReadiness(ReadinessState.UNAVAILABLE, "authentication check timed out")
        except OSError as error:
            return HarnessReadiness(
                ReadinessState.UNAVAILABLE,
                f"cannot check authentication ({type(error).__name__})",
            )
        if auth.returncode != 0 or not auth_matches(auth, self.profile.auth_pattern):
            return HarnessReadiness(
                ReadinessState.AUTH_REQUIRED,
                f"{self.profile.auth_label} required; "
                f"run {command} {' '.join(self.profile.login_args)}",
            )
        return HarnessReadiness(
            ReadinessState.READY,
            f"{self._version}; {self.profile.auth_label}",
        )

    def capabilities(self) -> HarnessCapabilities:
        supported = {
            Capability.START_WORKER,
            Capability.LOGIN,
            Capability.INTERRUPT,
            Capability.MODEL_SELECTION,
            Capability.WORKING_DIRECTORY,
            Capability.ISOLATED_WORKTREE,
            Capability.DIFF,
            Capability.USAGE,
        }
        if self.profile.install_latest or self.profile.install_exact:
            supported.add(Capability.INSTALL)
        if self.profile.update_latest or self.profile.update_exact:
            supported.add(Capability.UPDATE)
        if self.profile.direct_model_id is not None:
            supported.add(Capability.DIRECT_MODEL)
        return HarnessCapabilities(
            frozenset(supported),
            providers=frozenset({self.provider_id()}),
        )

    def direct_provider(self) -> ProviderIdentity:
        if self.profile.direct_model_id is None:
            return super().direct_provider()
        return ProviderIdentity(
            self.provider_id(),
            self.profile.provider_name,
            "harness-subscription",
            "subscription",
        )

    def direct_models(self) -> tuple[ModelDescriptor, ...]:
        model_id = self.profile.direct_model_id
        if model_id is None:
            return tuple(super().direct_models())
        return (
            ModelDescriptor(
                self.provider_id(),
                model_id,
                self.profile.direct_model_name,
                self.profile.direct_context_limit,
            ),
        )

    def invoke_model(self, request: ProviderRequest) -> ProviderResponse:
        model_id = self.profile.direct_model_id
        if model_id is None:
            return super().invoke_model(request)
        if request.model_id != model_id:
            raise AdapterError(
                ErrorKind.INCOMPATIBLE,
                f"{self.profile.display_name} does not advertise model {request.model_id!r}",
            )
        blocking = self.readiness().blocking_error()
        if blocking is not None:
            raise blocking
        argv = [
            self.command(),
            *self.profile.direct_base_args,
            self.profile.direct_model_flag,
            request.model_id,
        ]
        if self.profile.direct_stdin_marker is not None:
            argv.append(self.profile.direct_stdin_marker)
        try:
            with tempfile.TemporaryDirectory(prefix="agent-cli-model-") as directory:
                result = self.direct_model_runner(
                    tuple(argv),
                    self.subscription_env(),
                    direct_model_prompt(request),
                    Path(directory),
                    DIRECT_MODEL_TIMEOUT_S,
                )
        except FileNotFoundError:
            raise AdapterError(ErrorKind.NOT_FOUND, "harness executable disappeared") from None
        except subprocess.TimeoutExpired:
            raise AdapterError(ErrorKind.TIMEOUT, "model call timed out") from None
        except OSError as error:
            raise AdapterError(
                ErrorKind.HARNESS_ERROR,
                f"cannot run model call ({type(error).__name__})",
            ) from None
        if result.returncode != 0:
            diagnostic = safe_process_diagnostic(
                result.stderr,
                request.prompt,
                request.system or "",
            )
            detail = (
                f"model call exited with status {result.returncode}: {diagnostic}"
                if diagnostic
                else f"model call exited with status {result.returncode}"
            )
            raise AdapterError(ErrorKind.PROVIDER_ERROR, detail)
        parsed = parse_output(result.stdout, self.profile.output_protocol)
        if isinstance(parsed, Failure):
            raise AdapterError(ErrorKind.PROVIDER_ERROR, parsed.message)
        text, usage = parsed
        if not text:
            raise AdapterError(ErrorKind.PROVIDER_ERROR, "harness returned no model text")
        return ProviderResponse(request.model_id, text, usage)

    def check(self, request: WorkerRequest) -> None:
        HarnessAdapter.check(self, request)
        if request.authority is not None:
            authority_args(self.profile, request.authority)

    def start(self, request: WorkerRequest) -> WorkerHandle:
        self.check(request)
        blocking = self.readiness().blocking_error()
        if blocking is not None:
            raise blocking
        if not request.working_directory.is_dir():
            raise AdapterError(
                ErrorKind.INVALID_REQUEST,
                f"working directory does not exist: {request.working_directory}",
            )
        argv = list(self.worker_argv(request))
        prompt = tempfile.TemporaryFile(mode="w+", encoding="utf-8")  # noqa: SIM115
        stdout = tempfile.TemporaryFile(mode="w+", encoding="utf-8")  # noqa: SIM115
        stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8")  # noqa: SIM115
        prompt.write(render_worker_prompt(request))
        prompt.seek(0)
        try:
            process = self.launcher(
                tuple(argv),
                request.working_directory,
                self.subscription_env(),
                prompt,
                stdout,
                stderr,
            )
        except FileNotFoundError:
            close_files(prompt, stdout, stderr)
            raise AdapterError(ErrorKind.NOT_FOUND, "harness executable disappeared") from None
        except OSError as error:
            close_files(prompt, stdout, stderr)
            raise AdapterError(
                ErrorKind.HARNESS_ERROR,
                f"cannot start harness ({type(error).__name__})",
            ) from None
        prompt.close()
        token = str(uuid.uuid4())
        self._workers[token] = _Worker(request, process, stdout, stderr)
        return WorkerHandle(self.harness_id, token)

    def worker_argv(self, request: WorkerRequest) -> tuple[str, ...]:
        if request.authority is None:
            args = (
                self.profile.worker_plan_args
                if request.mode == "plan-only"
                else self.profile.worker_write_args
            )
        else:
            args = authority_args(self.profile, request.authority)
        argv = [self.command(), *self.profile.worker_base_args, *args]
        if request.model is not None:
            argv += [self.profile.worker_model_flag, request.model.id]
        if self.profile.worker_stdin_marker is not None:
            argv.append(self.profile.worker_stdin_marker)
        return tuple(argv)

    def worker(self, handle: WorkerHandle) -> _Worker:
        if handle.harness_id != self.harness_id:
            raise AdapterError(ErrorKind.NOT_FOUND, "worker belongs to another harness")
        worker = self._workers.get(handle.token)
        if worker is None:
            raise AdapterError(ErrorKind.NOT_FOUND, "worker not found")
        return worker

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
        _ = read_file(worker.stderr)
        close_files(worker.stdout, worker.stderr)
        if code != 0:
            worker.final = WorkerResult(
                WorkerStatus.FAILED,
                error=Failure(
                    ErrorKind.HARNESS_ERROR,
                    f"{self.profile.display_name} exited with status {code}",
                ),
            )
            return worker.final
        parsed = parse_output(stdout, self.profile.output_protocol)
        if isinstance(parsed, Failure):
            worker.final = WorkerResult(WorkerStatus.FAILED, error=parsed)
            return worker.final
        summary, usage = parsed
        worker.final = WorkerResult(
            WorkerStatus.COMPLETED,
            summary=summary,
            changes=self.safe_changes(worker.request.working_directory),
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
                f"cannot interrupt harness ({type(error).__name__})",
            ) from None
        worker.canceled = True

    def diff(self, handle: WorkerHandle) -> Changes:
        return self.git_changes(self.worker(handle).request.working_directory)

    def usage(self, handle: WorkerHandle) -> Usage:
        return self.result(handle).usage or Usage()

    def install(self) -> HarnessReadiness:
        template = (
            self.profile.install_latest
            if self.config.version == "latest"
            else self.profile.install_exact
        )
        if not template:
            return super().install()
        self.run_lifecycle(render_argv(template, self), "install")
        return self.readiness()

    def update(self) -> HarnessReadiness:
        template = (
            self.profile.update_latest
            if self.config.version == "latest"
            else self.profile.update_exact
        )
        if not template:
            return super().update()
        self.run_lifecycle(render_argv(template, self), "update")
        return self.readiness()

    def login(self) -> HarnessReadiness:
        if not self.config.enabled:
            raise AdapterError(ErrorKind.INVALID_REQUEST, "harness is disabled in config")
        self.run_lifecycle((self.command(), *self.profile.login_args), "login")
        return self.readiness()

    def run_lifecycle(self, argv: tuple[str, ...], action: str) -> None:
        try:
            result = self.interactive_runner(argv, self.subscription_env())
        except FileNotFoundError:
            raise AdapterError(
                ErrorKind.NOT_FOUND,
                f"cannot {action}: required command {argv[0]!r} is not installed",
            ) from None
        except OSError as error:
            raise AdapterError(
                ErrorKind.HARNESS_ERROR,
                f"cannot {action} ({type(error).__name__})",
            ) from None
        if result.returncode != 0:
            kind = ErrorKind.AUTH_FAILED if action == "login" else ErrorKind.HARNESS_ERROR
            raise AdapterError(kind, f"{action} did not complete")

    def git_changes(self, cwd: Path) -> Changes:
        try:
            tracked = self.git_runner(("git", "diff", "HEAD", "--name-only", "--"), cwd)
            untracked = self.git_runner(("git", "ls-files", "--others", "--exclude-standard"), cwd)
            diff = self.git_runner(("git", "diff", "--no-ext-diff", "HEAD", "--"), cwd)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AdapterError(
                ErrorKind.HARNESS_ERROR,
                f"cannot inspect git changes ({type(error).__name__})",
            ) from None
        if any(item.returncode != 0 for item in (tracked, untracked, diff)):
            raise AdapterError(ErrorKind.HARNESS_ERROR, "cannot inspect git changes")
        files = tuple(
            sorted(
                line
                for line in {*tracked.stdout.splitlines(), *untracked.stdout.splitlines()}
                if line
            )
        )
        return Changes(files, diff.stdout or None)

    def safe_changes(self, cwd: Path) -> Changes | None:
        try:
            return self.git_changes(cwd)
        except AdapterError:
            return None


def auth_matches(result: subprocess.CompletedProcess[str], pattern: AuthPattern) -> bool:
    text = result.stdout if pattern.source == "stdout" else result.stderr
    if pattern.contains is not None:
        return pattern.contains in text
    if pattern.regexes:
        return any(re.search(expression, text) is not None for expression in pattern.regexes)
    payload = json_object(text)
    if payload is None:
        return False
    return any(mapping_matches(payload, candidate) for candidate in pattern.json_any)


def mapping_matches(payload: Mapping[str, Any], expected: Mapping[str, object]) -> bool:
    for key, value in expected.items():
        actual = payload.get(key)
        if value == "*":
            if actual in (None, "", False):
                return False
        elif actual != value:
            return False
    return True


def render_argv(template: tuple[str, ...], adapter: CliHarnessAdapter) -> tuple[str, ...]:
    return tuple(
        part.replace("{command}", adapter.command()).replace("{version}", adapter.config.version)
        for part in template
    )


def parse_output(text: str, protocol: OutputProtocol) -> tuple[str, Usage] | Failure:
    if protocol == "json-result":
        payload = json_object(text)
        if payload is None or payload.get("is_error") is True:
            return Failure(ErrorKind.HARNESS_ERROR, "harness returned invalid JSON")
        summary = payload.get("result")
        if not isinstance(summary, str):
            return Failure(ErrorKind.HARNESS_ERROR, "harness returned no result text")
        return summary, usage_from_mapping(payload)
    return parse_jsonl_agent(text)


def parse_jsonl_agent(text: str) -> tuple[str, Usage] | Failure:
    summary = ""
    reference: str | None = None
    input_tokens = 0
    output_tokens = 0
    input_known = True
    output_known = True
    completed = False
    for line in text.splitlines():
        if not line.strip():
            continue
        event = json_object(line)
        if event is None:
            return Failure(ErrorKind.HARNESS_ERROR, "harness returned invalid JSONL")
        kind = event.get("type")
        if kind in {"error", "turn.failed"}:
            return Failure(ErrorKind.HARNESS_ERROR, "harness reported an error")
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
            completed = True
            raw = event.get("usage")
            if not isinstance(raw, dict):
                input_known = False
                output_known = False
                continue
            incoming = count(raw.get("input_tokens"))
            outgoing = count(raw.get("output_tokens"))
            if incoming is None:
                input_known = False
            else:
                input_tokens += incoming
            if outgoing is None:
                output_known = False
            else:
                output_tokens += outgoing
    if not completed:
        return Failure(ErrorKind.HARNESS_ERROR, "harness ended without a completed turn")
    return (
        summary,
        Usage(
            input_tokens=input_tokens if input_known else None,
            output_tokens=output_tokens if output_known else None,
            reference=reference,
        ),
    )


def usage_from_mapping(payload: Mapping[str, Any]) -> Usage:
    raw = payload.get("usage")
    usage = raw if isinstance(raw, dict) else {}
    reference = payload.get("session_id")
    return Usage(
        input_tokens=count(usage.get("input_tokens")),
        output_tokens=count(usage.get("output_tokens")),
        reference=reference if isinstance(reference, str) and reference else None,
    )


def direct_model_prompt(request: ProviderRequest) -> str:
    sections = []
    if request.system:
        sections.append("System instructions:\n" + request.system)
    sections.append("User request:\n" + request.prompt)
    if request.max_output_tokens is not None:
        sections.append(
            f"Keep the final answer within approximately {request.max_output_tokens} output tokens."
        )
    return "\n\n".join(sections)


def render_worker_prompt(request: WorkerRequest) -> str:
    authority = effective_authority(request)
    sections = [
        "You are a bounded worker controlled by Agent CLI.",
        f"Role: {request.role}",
        f"Authority: {authority.profile}",
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
        f"- Command execution: {'allowed' if authority.execute_commands else 'not allowed'}.\n"
        f"- Source modification: {'allowed' if authority.modify_source else 'not authorized'}.\n"
        "- Scratch/cache files inside a disposable worker workspace are not source authority.\n"
        "- End with a concise summary of work and verification."
    )
    return "\n\n".join(sections)


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

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rich.console import RenderableType

from agent_cli.adapters import (
    AdapterError,
    AdapterRegistry,
    Capability,
    ErrorKind,
    HarnessCapabilities,
    HarnessIdentity,
    HarnessReadiness,
    ReadinessState,
    WorkerHandle,
    WorkerRequest,
    WorkerResult,
    WorkerStatus,
)
from agent_cli.adapters.harness import HarnessAdapter
from agent_cli.config import Config
from agent_cli.config.model import HarnessConfig
from agent_cli.harness_lifecycle import HarnessLifecycleService
from agent_cli.ui import plain


@dataclass
class FakeLifecycleHarness(HarnessAdapter):
    harness_id: str
    state: ReadinessState
    version: str = "1.0"
    install_calls: int = 0
    update_calls: int = 0
    fail_update: bool = False

    def identity(self) -> HarnessIdentity:
        return HarnessIdentity(self.harness_id, self.harness_id, self.version)

    def readiness(self) -> HarnessReadiness:
        return HarnessReadiness(self.state, self.version)

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            frozenset({Capability.START_WORKER, Capability.INSTALL, Capability.UPDATE})
        )

    def install(self) -> HarnessReadiness:
        self.install_calls += 1
        self.state = ReadinessState.AUTH_REQUIRED
        return self.readiness()

    def update(self) -> HarnessReadiness:
        self.update_calls += 1
        if self.fail_update:
            raise AdapterError(ErrorKind.HARNESS_ERROR, "offline")
        self.version = "2.0"
        return self.readiness()

    def start(self, request: WorkerRequest) -> WorkerHandle:
        raise AssertionError("not used")

    def status(self, handle: WorkerHandle) -> WorkerStatus:
        raise AssertionError("not used")

    def result(self, handle: WorkerHandle) -> WorkerResult:
        raise AssertionError("not used")


@dataclass
class Conversation:
    answers: list[str] = field(default_factory=list)
    output: list[str] = field(default_factory=list)

    def read_line(self, prompt: str) -> str:
        return self.answers.pop(0)

    def write_line(self, shown: RenderableType) -> None:
        self.output.append(plain(shown))


def configured(*ids: str, version: str = "latest") -> Config:
    return Config(
        harnesses={id: HarnessConfig(type="cli", profile=id, version=version) for id in ids}
    )


def test_offer_install_only_installs_missing_configured_harness() -> None:
    claude = FakeLifecycleHarness("claude-code", ReadinessState.NOT_DETECTED)
    codex = FakeLifecycleHarness("codex-cli", ReadinessState.READY)
    conversation = Conversation(["y"])
    service = HarnessLifecycleService(
        AdapterRegistry([claude, codex]),
        configured("claude-code", "codex-cli"),
        conversation.read_line,
        conversation.write_line,
    )

    output = service.offer_install()

    assert output is not None and "installed" in plain(output)
    assert claude.install_calls == 1
    assert codex.install_calls == 0


def test_declining_install_does_not_run_installer() -> None:
    claude = FakeLifecycleHarness("claude-code", ReadinessState.NOT_DETECTED)
    conversation = Conversation(["n"])
    service = HarnessLifecycleService(
        AdapterRegistry([claude]),
        configured("claude-code"),
        conversation.read_line,
        conversation.write_line,
    )

    output = service.offer_install()

    assert output is not None and "skipped" in plain(output)
    assert claude.install_calls == 0


def test_latest_refresh_runs_once_then_uses_24_hour_cache(tmp_path: Path) -> None:
    claude = FakeLifecycleHarness("claude-code", ReadinessState.READY)
    cache = tmp_path / "updates.json"
    service = HarnessLifecycleService(
        AdapterRegistry([claude]),
        configured("claude-code"),
        cache_file=cache,
        clock=lambda: 100_000.0,
    )

    first = service.refresh_latest()
    second = service.refresh_latest()

    assert first is not None and "updated to 2.0" in plain(first)
    assert second is None
    assert claude.update_calls == 1


def test_exact_version_never_runs_automatic_update(tmp_path: Path) -> None:
    claude = FakeLifecycleHarness("claude-code", ReadinessState.READY)
    service = HarnessLifecycleService(
        AdapterRegistry([claude]),
        configured("claude-code", version="1.0"),
        cache_file=tmp_path / "updates.json",
        clock=lambda: 100_000.0,
    )

    assert service.refresh_latest() is None
    assert claude.update_calls == 0


def test_failed_latest_update_is_nonblocking_and_cached(tmp_path: Path) -> None:
    claude = FakeLifecycleHarness("claude-code", ReadinessState.READY, fail_update=True)
    cache = tmp_path / "updates.json"
    service = HarnessLifecycleService(
        AdapterRegistry([claude]),
        configured("claude-code"),
        cache_file=cache,
        clock=lambda: 100_000.0,
    )

    output = service.refresh_latest()
    again = service.refresh_latest()

    assert output is not None and "continuing" in plain(output)
    assert again is None
    assert claude.update_calls == 1


def test_unconfigured_harness_is_never_installed_or_updated(tmp_path: Path) -> None:
    claude = FakeLifecycleHarness("claude-code", ReadinessState.NOT_DETECTED)
    service = HarnessLifecycleService(
        AdapterRegistry([claude]),
        Config(),
        lambda _: "y",
        lambda _: None,
        tmp_path / "updates.json",
    )

    assert service.offer_install() is None
    assert service.refresh_latest() is None
    assert claude.install_calls == 0
    assert claude.update_calls == 0

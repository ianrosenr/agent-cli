from __future__ import annotations

from dataclasses import dataclass, field
from agent_cli.adapters import (
    AdapterRegistry,
    Capability,
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
from agent_cli.config.model import AccessConfig, HarnessConfig
from agent_cli.login import HarnessLoginService
from agent_cli.ui import plain


@dataclass
class FakeHarness(HarnessAdapter):
    harness_id: str
    name: str
    state: ReadinessState
    login_calls: int = 0

    def identity(self) -> HarnessIdentity:
        return HarnessIdentity(self.harness_id, self.name, "1.0")

    def readiness(self) -> HarnessReadiness:
        return HarnessReadiness(self.state)

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(frozenset({Capability.LOGIN, Capability.START_WORKER}))

    def login(self) -> HarnessReadiness:
        self.login_calls += 1
        self.state = ReadinessState.READY
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


def service(
    claude: FakeHarness,
    codex: FakeHarness,
    config: Config | None = None,
    answers: list[str] | None = None,
) -> tuple[HarnessLoginService, Conversation]:
    conversation = Conversation(answers or [])
    registry = AdapterRegistry([claude, codex])
    return (
        HarnessLoginService(
            registry,
            config or Config(),
            conversation.read_line,
            conversation.write_line,
        ),
        conversation,
    )


def test_login_skips_ready_and_logs_in_only_auth_required() -> None:
    claude = FakeHarness("claude-code", "Claude Code", ReadinessState.READY)
    codex = FakeHarness("codex-cli", "Codex CLI", ReadinessState.AUTH_REQUIRED)
    login, _ = service(claude, codex)

    output = plain(login.run())

    assert claude.login_calls == 0
    assert codex.login_calls == 1
    assert "Claude Code" in output and "already signed in" in output
    assert "Codex CLI" in output and "signed in" in output
    assert "All configured harnesses are ready" in output


def test_login_respects_saved_disabled_harness() -> None:
    claude = FakeHarness("claude-code", "Claude Code", ReadinessState.AUTH_REQUIRED)
    codex = FakeHarness("codex-cli", "Codex CLI", ReadinessState.AUTH_REQUIRED)
    config = Config(
        harnesses={
            "claude-code": HarnessConfig(enabled=False),
            "codex-cli": HarnessConfig(enabled=True),
        }
    )
    login, _ = service(claude, codex, config)

    output = plain(login.run())

    assert claude.login_calls == 0
    assert codex.login_calls == 1
    assert "Claude Code" not in output


def test_targeted_login_uses_alias_and_respects_disabled_config() -> None:
    claude = FakeHarness("claude-code", "Claude Code", ReadinessState.AUTH_REQUIRED)
    codex = FakeHarness("codex-cli", "Codex CLI", ReadinessState.AUTH_REQUIRED)
    config = Config(harnesses={"claude-code": HarnessConfig(enabled=False)})
    login, _ = service(claude, codex, config)

    disabled = plain(login.run("claude"))
    codex_output = plain(login.run("codex"))

    assert "disabled in /startup" in disabled
    assert claude.login_calls == 0
    assert codex.login_calls == 1
    assert "signed in" in codex_output


def test_offer_prompts_only_when_auth_is_needed_and_can_be_declined() -> None:
    claude = FakeHarness("claude-code", "Claude Code", ReadinessState.READY)
    codex = FakeHarness("codex-cli", "Codex CLI", ReadinessState.AUTH_REQUIRED)
    login, conversation = service(claude, codex, answers=["n"])

    output = login.offer()

    assert output is not None
    assert "Sign-in skipped" in plain(output)
    assert codex.login_calls == 0
    assert any("Authentication is needed for: Codex CLI" in shown for shown in conversation.output)


def test_offer_is_silent_when_everything_is_already_ready() -> None:
    claude = FakeHarness("claude-code", "Claude Code", ReadinessState.READY)
    codex = FakeHarness("codex-cli", "Codex CLI", ReadinessState.READY)
    login, conversation = service(claude, codex)

    assert login.offer() is None
    assert conversation.output == []


def test_unknown_target_is_actionable() -> None:
    claude = FakeHarness("claude-code", "Claude Code", ReadinessState.READY)
    codex = FakeHarness("codex-cli", "Codex CLI", ReadinessState.READY)
    login, _ = service(claude, codex)

    output = plain(login.run("other"))

    assert "Unknown login target" in output
    assert "/login claude" in output and "/login codex" in output


def test_config_without_explicit_harness_entries_remains_backward_compatible() -> None:
    claude = FakeHarness("claude-code", "Claude Code", ReadinessState.AUTH_REQUIRED)
    codex = FakeHarness("codex-cli", "Codex CLI", ReadinessState.AUTH_REQUIRED)
    login, _ = service(claude, codex, Config(access=AccessConfig()))

    login.run()

    assert claude.login_calls == 1
    assert codex.login_calls == 1

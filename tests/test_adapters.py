from __future__ import annotations

import ast
import dataclasses
import inspect
import re
from pathlib import Path

import pytest
import yaml

from agent_cli import cli
from agent_cli.adapters import (
    AdapterError,
    AdapterRegistry,
    Capability,
    Changes,
    ErrorKind,
    Failure,
    HarnessAdapter,
    HarnessCapabilities,
    HarnessIdentity,
    HarnessReadiness,
    ModelDescriptor,
    ProviderIdentity,
    ProviderReadiness,
    ProviderRequest,
    ReadinessState,
    Selection,
    Usage,
    WorkerHandle,
    WorkerRequest,
    WorkerResult,
    WorkerStatus,
    adapter_status,
    domain,
)
from agent_cli.adapters.fake import FakeHarnessAdapter, FakeProviderAdapter
from agent_cli.commands import CommandContext, build_registry
from agent_cli.config import Config, ConfigService
from agent_cli.config.model import HarnessConfig, ProviderConfig
from agent_cli.guided_setup import SETTINGS
from agent_cli.ui import plain
from rendered import recorder

ENTER_THROUGH = [""] * (len(SETTINGS) + 2)  # target layer, every setting, confirmation
VENDORS = {"pi", "codex", "claude", "opencode", "anthropic", "openai"}


def request(
    model: Selection | None = None,
    provider: Selection | None = None,
    harness: Selection | None = None,
    task_id: str = "t1",
) -> WorkerRequest:
    return WorkerRequest(
        task_id,
        "implementer",
        "Add cache expiry.",
        Path("/work"),
        model=model,
        provider=provider,
        harness=harness,
    )


def capabilities(*supported: Capability, **constraints: frozenset[str]) -> HarnessCapabilities:
    return HarnessCapabilities(frozenset(supported), **constraints)


def field_names(cls: type) -> set[str]:
    return {item.name for item in dataclasses.fields(cls)}


# -- Domain ------------------------------------------------------------------------------------


def test_no_vendor_specific_fields_in_domain_models() -> None:
    for _, cls in inspect.getmembers(domain, inspect.isclass):
        if not dataclasses.is_dataclass(cls):
            continue
        for item in dataclasses.fields(cls):
            words = set(item.name.lower().split("_"))
            assert words.isdisjoint(VENDORS), f"{cls.__name__}.{item.name}"


def test_domain_does_not_depend_on_configuration() -> None:
    tree = ast.parse(inspect.getsource(domain))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(name and name.startswith("agent_cli.config") for name in imported)


def test_model_provider_and_harness_are_separate_selections() -> None:
    assert {"model", "provider", "harness"} <= field_names(WorkerRequest)
    # A harness identity names no model or provider, and a provider identity names no harness.
    assert field_names(HarnessIdentity).isdisjoint({"model", "provider", "models", "providers"})
    assert field_names(ProviderIdentity).isdisjoint({"harness", "model", "models"})
    # A model is described by the provider it was reached through, not by a harness.
    assert "provider_id" in field_names(ModelDescriptor)
    assert field_names(ModelDescriptor).isdisjoint({"harness", "harness_id"})


def test_capability_set_is_exactly_the_contract() -> None:
    assert {c.value for c in Capability} == {
        "start_worker",
        "login",
        "follow_up",
        "interrupt",
        "model_selection",
        "provider_selection",
        "working_directory",
        "isolated_worktree",
        "diff",
        "usage",
        "resumable_sessions",
    }
    assert HarnessCapabilities().supported == frozenset()


def test_selection_records_whether_it_was_pinned() -> None:
    assert Selection.pin("m1") == Selection("m1", pinned=True)
    assert not Selection("m1").pinned


def test_failure_is_plain_comparable_data() -> None:
    error = AdapterError(ErrorKind.TIMEOUT, "no answer")
    assert error.failure == Failure(ErrorKind.TIMEOUT, "no answer")
    assert (error.kind, error.message) == (ErrorKind.TIMEOUT, "no answer")
    assert dataclasses.asdict(error.failure) == {"kind": "timeout", "message": "no answer"}


@pytest.mark.parametrize(
    ("state", "kind"),
    [
        (ReadinessState.NOT_DETECTED, ErrorKind.NOT_FOUND),
        (ReadinessState.AUTH_REQUIRED, ErrorKind.AUTH_REQUIRED),
        (ReadinessState.MISCONFIGURED, ErrorKind.INVALID_REQUEST),
        (ReadinessState.UNAVAILABLE, ErrorKind.UNAVAILABLE),
        (ReadinessState.DETECTED, ErrorKind.UNAVAILABLE),
        (ReadinessState.READY, None),
    ],
)
def test_readiness_maps_to_a_matching_error_kind(
    state: ReadinessState, kind: ErrorKind | None
) -> None:
    error = HarnessReadiness(state, "why").blocking_error()
    assert (error.kind if error else None) is kind


def test_usage_total_is_derived_when_missing() -> None:
    assert Usage(input_tokens=3, output_tokens=4).tokens == 7
    assert Usage(total_tokens=10).tokens == 10
    assert Usage(input_tokens=3).tokens is None


def test_finished_statuses() -> None:
    assert {s for s in WorkerStatus if s.finished} == {
        WorkerStatus.COMPLETED,
        WorkerStatus.FAILED,
        WorkerStatus.CANCELED,
    }


# -- Harness -----------------------------------------------------------------------------------


def test_fake_harness_runs_a_worker_to_a_normalized_result() -> None:
    usage = Usage(input_tokens=10, output_tokens=5, cost_usd=0.01)
    changes = Changes(("cache.py",), "--- a/cache.py\n+++ b/cache.py\n")
    outcome = WorkerResult(WorkerStatus.COMPLETED, "Added expiry.", changes=changes, usage=usage)
    harness = FakeHarnessAdapter(outcome=outcome)

    handle = harness.start(request())

    assert handle == WorkerHandle("fake", "worker-1")
    assert harness.status(handle) is WorkerStatus.COMPLETED
    assert harness.result(handle) == outcome
    assert harness.diff(handle) == changes
    assert harness.usage(handle) == usage


def test_fake_harness_scripts_outcomes_per_worker() -> None:
    failure = Failure(ErrorKind.HARNESS_ERROR, "tests failed")

    def script(request: WorkerRequest) -> WorkerResult:
        if request.task_id == "b":
            return WorkerResult(WorkerStatus.FAILED, error=failure)
        return WorkerResult(WorkerStatus.COMPLETED, f"did {request.task_id}")

    harness = FakeHarnessAdapter(script=script)
    a = harness.start(request(task_id="a"))
    b = harness.start(request(task_id="b"))
    assert harness.result(a) == WorkerResult(WorkerStatus.COMPLETED, "did a")
    assert harness.result(b) == WorkerResult(WorkerStatus.FAILED, error=failure)


def test_worker_lifecycle_running_follow_up_and_interrupt() -> None:
    harness = FakeHarnessAdapter(auto_finish=False)
    handle = harness.start(request())
    assert harness.status(handle) is WorkerStatus.RUNNING
    harness.send(handle, "also add tests")
    assert harness.workers[handle.token].messages == ["also add tests"]
    harness.interrupt(handle)
    assert harness.status(handle) is WorkerStatus.CANCELED
    assert harness.result(handle).status is WorkerStatus.CANCELED


def test_running_worker_finishes_with_its_outcome() -> None:
    harness = FakeHarnessAdapter(auto_finish=False)
    handle = harness.start(request())
    harness.finish(handle)
    assert harness.result(handle) == harness.outcome


OPTIONAL_OPERATIONS = [
    ("send", Capability.FOLLOW_UP),
    ("interrupt", Capability.INTERRUPT),
    ("diff", Capability.DIFF),
    ("usage", Capability.USAGE),
]


def call(harness: HarnessAdapter, operation: str, handle: WorkerHandle) -> None:
    args = ("hi",) if operation == "send" else ()
    getattr(harness, operation)(handle, *args)


@pytest.mark.parametrize(("operation", "capability"), OPTIONAL_OPERATIONS)
def test_unsupported_operations_raise_normalized_errors(
    operation: str, capability: Capability
) -> None:
    harness = FakeHarnessAdapter(harness_capabilities=capabilities(Capability.START_WORKER))
    handle = harness.start(request())
    with pytest.raises(AdapterError) as caught:
        call(harness, operation, handle)
    assert caught.value.kind is ErrorKind.UNSUPPORTED
    assert str(capability) in caught.value.message


class MinimalHarness(HarnessAdapter):
    """Implements only the required methods, to check the base class defaults."""

    def identity(self) -> HarnessIdentity:
        return HarnessIdentity("minimal", "Minimal")

    def readiness(self) -> HarnessReadiness:
        return HarnessReadiness(ReadinessState.READY)

    def capabilities(self) -> HarnessCapabilities:
        return capabilities(Capability.START_WORKER)

    def start(self, request: WorkerRequest) -> WorkerHandle:
        return WorkerHandle("minimal", "1")

    def status(self, handle: WorkerHandle) -> WorkerStatus:
        return WorkerStatus.RUNNING

    def result(self, handle: WorkerHandle) -> WorkerResult:
        return WorkerResult(WorkerStatus.RUNNING)


@pytest.mark.parametrize(("operation", "capability"), OPTIONAL_OPERATIONS)
def test_optional_operations_default_to_unsupported(operation: str, capability: Capability) -> None:
    harness = MinimalHarness()
    with pytest.raises(AdapterError) as caught:
        call(harness, operation, harness.start(request()))
    assert caught.value.kind is ErrorKind.UNSUPPORTED


def test_start_failure_is_a_normalized_error() -> None:
    error = AdapterError(ErrorKind.AUTH_FAILED, "token expired")
    harness = FakeHarnessAdapter(start_error=error)
    with pytest.raises(AdapterError) as caught:
        harness.start(request())
    assert caught.value.kind is ErrorKind.AUTH_FAILED


@pytest.mark.parametrize(
    ("state", "kind"),
    [
        (ReadinessState.AUTH_REQUIRED, ErrorKind.AUTH_REQUIRED),
        (ReadinessState.NOT_DETECTED, ErrorKind.NOT_FOUND),
        (ReadinessState.DETECTED, ErrorKind.UNAVAILABLE),
        (ReadinessState.UNAVAILABLE, ErrorKind.UNAVAILABLE),
    ],
)
def test_harness_that_is_not_ready_refuses_with_a_matching_error(
    state: ReadinessState, kind: ErrorKind
) -> None:
    harness = FakeHarnessAdapter(harness_readiness=HarnessReadiness(state, "why"))
    with pytest.raises(AdapterError) as caught:
        harness.start(request())
    assert caught.value.kind is kind


def test_unknown_handle_is_not_found() -> None:
    harness = FakeHarnessAdapter()
    with pytest.raises(AdapterError) as caught:
        harness.status(WorkerHandle("fake", "nope"))
    assert caught.value.kind is ErrorKind.NOT_FOUND


# -- Selections and pins -----------------------------------------------------------------------


def test_pins_reach_the_harness_unchanged() -> None:
    harness = FakeHarnessAdapter()
    sent = request(Selection.pin("m1"), Selection.pin("p1"), Selection.pin("fake"))
    handle = harness.start(sent)
    assert harness.workers[handle.token].request == sent


@pytest.mark.parametrize(
    ("caps", "sent"),
    [
        # A fixed-model harness runs that model, pinned or not, without a selection capability.
        (
            capabilities(Capability.START_WORKER, models=frozenset({"m1"})),
            request(Selection.pin("m1")),
        ),
        (capabilities(Capability.START_WORKER, models=frozenset({"m1"})), request(Selection("m1"))),
        (
            capabilities(Capability.START_WORKER, providers=frozenset({"p1"})),
            request(provider=Selection.pin("p1")),
        ),
        # A harness that lets callers choose accepts any model.
        (
            capabilities(Capability.START_WORKER, Capability.MODEL_SELECTION),
            request(Selection("m9")),
        ),
    ],
)
def test_compatible_selections_start(caps: HarnessCapabilities, sent: WorkerRequest) -> None:
    harness = FakeHarnessAdapter(harness_capabilities=caps)
    handle = harness.start(sent)
    assert harness.workers[handle.token].request == sent


@pytest.mark.parametrize(
    ("caps", "sent", "reason"),
    [
        (
            capabilities(Capability.START_WORKER),
            request(harness=Selection.pin("other")),
            "pinned harness 'other' is required",
        ),
        (
            capabilities(Capability.START_WORKER),
            request(Selection.pin("m1")),
            "cannot select pinned model 'm1'",
        ),
        (
            capabilities(Capability.START_WORKER),
            request(Selection("m1")),
            "cannot select model 'm1'",
        ),
        (
            capabilities(
                Capability.START_WORKER, Capability.MODEL_SELECTION, models=frozenset({"m2"})
            ),
            request(Selection.pin("m1")),
            "cannot run pinned model 'm1'",
        ),
        (
            capabilities(Capability.START_WORKER, providers=frozenset({"p2"})),
            request(provider=Selection.pin("p1")),
            "cannot run pinned provider 'p1'",
        ),
    ],
)
def test_unsatisfiable_selections_are_incompatible_not_substituted(
    caps: HarnessCapabilities, sent: WorkerRequest, reason: str
) -> None:
    harness = FakeHarnessAdapter(harness_capabilities=caps)
    with pytest.raises(AdapterError) as caught:
        harness.start(sent)
    assert caught.value.kind is ErrorKind.INCOMPATIBLE
    assert reason in caught.value.message
    assert harness.workers == {}


# -- Provider ----------------------------------------------------------------------------------

MODELS = (
    ModelDescriptor("ignored", "m1", "Model One", context_limit=200_000),
    ModelDescriptor("ignored", "m2", available=False),
)


def test_fake_provider_lists_normalized_models() -> None:
    provider = FakeProviderAdapter(models=MODELS)
    listed = provider.list_models()
    assert [m.model_id for m in listed] == ["m1", "m2"]
    assert all(m.provider_id == "fake" for m in listed)
    assert listed[0].context_limit == 200_000
    assert not listed[1].available


def test_fake_provider_invokes_the_exact_model() -> None:
    usage = Usage(input_tokens=1, output_tokens=2)
    provider = FakeProviderAdapter(models=MODELS, reply="hello", usage=usage)
    response = provider.invoke(ProviderRequest("m1", "hi"))
    assert (response.model_id, response.text, response.usage) == ("m1", "hello", usage)


@pytest.mark.parametrize(
    ("model", "kind"), [("m9", ErrorKind.NOT_FOUND), ("m2", ErrorKind.UNAVAILABLE)]
)
def test_fake_provider_never_substitutes_a_model(model: str, kind: ErrorKind) -> None:
    provider = FakeProviderAdapter(models=MODELS)
    with pytest.raises(AdapterError) as caught:
        provider.invoke(ProviderRequest(model, "hi"))
    assert caught.value.kind is kind


def test_provider_that_is_not_ready_refuses_with_a_matching_error() -> None:
    provider = FakeProviderAdapter(
        models=MODELS, provider_readiness=ProviderReadiness(ReadinessState.AUTH_REQUIRED)
    )
    with pytest.raises(AdapterError) as caught:
        provider.invoke(ProviderRequest("m1", "hi"))
    assert caught.value.kind is ErrorKind.AUTH_REQUIRED


def test_provider_failures_are_normalized_errors() -> None:
    provider = FakeProviderAdapter(
        models=MODELS, error=AdapterError(ErrorKind.RATE_LIMITED, "slow down")
    )
    for attempt in (provider.list_models, lambda: provider.invoke(ProviderRequest("m1", "hi"))):
        with pytest.raises(AdapterError) as caught:
            attempt()
        assert caught.value.kind is ErrorKind.RATE_LIMITED


# -- Registry ----------------------------------------------------------------------------------


def test_registry_looks_up_adapters_by_id() -> None:
    harness, provider = FakeHarnessAdapter(), FakeProviderAdapter()
    registry = AdapterRegistry([harness], [provider])
    assert registry.harness("fake") is harness
    assert registry.provider("fake") is provider
    assert registry.harness("missing") is None


def test_registry_rejects_duplicate_ids() -> None:
    registry = AdapterRegistry([FakeHarnessAdapter()])
    with pytest.raises(ValueError, match="already registered"):
        registry.register_harness(FakeHarnessAdapter())


# -- Status and the configuration boundary -----------------------------------------------------


def status_registry() -> AdapterRegistry:
    return AdapterRegistry(
        [
            FakeHarnessAdapter(HarnessIdentity("alpha", "Alpha", "2.1")),
            FakeHarnessAdapter(
                HarnessIdentity("beta", "beta"),
                HarnessReadiness(ReadinessState.AUTH_REQUIRED, "run beta login"),
            ),
        ],
        [
            FakeProviderAdapter(
                ProviderIdentity("local", "Local vLLM", "openai-compatible", "local"),
                ProviderReadiness(ReadinessState.UNAVAILABLE, "connection refused"),
            )
        ],
    )


def test_status_combines_config_with_runtime_readiness() -> None:
    config = Config(
        harnesses={"beta": HarnessConfig(), "gamma": HarnessConfig(), "off": HarnessConfig(False)},
        providers={"local": ProviderConfig("openai-compatible")},
    )
    assert adapter_status(status_registry(), config) == [
        "Harnesses:",
        "  alpha (Alpha 2.1): Ready",
        "  beta: Sign-in required (run beta login)",
        "  gamma: configured; no adapter available yet",
        "  off: disabled in config",
        "Providers:",
        "  local (Local vLLM): Unavailable (connection refused)",
    ]


def test_status_reports_misbehaving_adapters_instead_of_crashing() -> None:
    class BrokenReadiness(FakeHarnessAdapter):
        def readiness(self) -> HarnessReadiness:
            raise RuntimeError("boom")

    class BrokenIdentity(FakeProviderAdapter):
        """Registers normally, then fails when asked who it is."""

        def identity(self) -> ProviderIdentity:
            if self.requests:
                raise RuntimeError("no id")
            return super().identity()

    provider = BrokenIdentity()
    registry = AdapterRegistry([BrokenReadiness()], [provider])
    provider.requests.append(ProviderRequest("m", "break"))
    lines = adapter_status(registry, Config())
    assert "  fake (Fake harness 1.0): Unavailable (adapter error: boom)" in lines
    assert "  fake: Unavailable (adapter error: no id)" in lines


def test_runtime_readiness_is_not_configuration() -> None:
    config_fields = field_names(HarnessConfig) | field_names(ProviderConfig)
    assert config_fields.isdisjoint({"detected", "version", "ready", "readiness", "health"})
    config = Config(harnesses={"beta": HarnessConfig()})
    adapter_status(status_registry(), config)
    assert config == Config(harnesses={"beta": HarnessConfig()})


def test_startup_shows_adapter_readiness_and_saves_none_of_it(service: ConfigService) -> None:
    answers = iter([*ENTER_THROUGH, "n"])
    output, write_line = recorder()
    context = CommandContext(
        registry=build_registry(),
        config=service,
        read_line=lambda prompt: next(answers),
        write_line=write_line,
        adapters=status_registry(),
    )
    context.registry.dispatch("/startup", context)
    transcript = "\n".join(output)
    rows = [re.split(r"\s{2,}", line.strip()) for line in transcript.splitlines()]
    assert ["alpha (Alpha 2.1)", "Ready"] in rows
    assert ["beta", "Sign-in required (run beta login)"] in rows
    saved = yaml.safe_load(service.paths.global_file.read_text())
    assert set(saved["harnesses"]) == {"claude-code", "codex-cli"}
    assert "alpha" not in saved["harnesses"]
    assert "beta" not in saved["harnesses"]
    assert "providers" not in saved


def test_providers_command_shows_adapter_status(service: ConfigService) -> None:
    context = CommandContext(registry=build_registry(), config=service, adapters=status_registry())
    lines = plain(context.registry.dispatch("/providers", context)).splitlines()
    rows = [re.split(r"\s{2,}", line.strip()) for line in lines]
    assert rows[:3] == [["Harnesses"], ["Name", "Status"], ["alpha (Alpha 2.1)", "Ready"]]
    assert ["beta", "Sign-in required (run beta login)"] in rows


def test_providers_command_without_adapters_or_config() -> None:
    context = CommandContext(registry=build_registry())
    assert plain(context.registry.dispatch("/providers", context)) == (
        "Harnesses  none configured or detected\n\nProviders  none configured or detected"
    )


def test_shell_passes_its_adapters_to_commands() -> None:
    output, write_line = recorder()
    lines = iter(["/providers"])

    def scripted(prompt: str) -> str:
        line = next(lines, None)
        if line is None:
            raise EOFError
        return line

    cli.run_shell(build_registry(), scripted, write_line, "", adapters=status_registry())
    assert "  alpha (Alpha 2.1)  Ready" in output[1]

from __future__ import annotations

from dataclasses import dataclass

from agent_cli.adapters import (
    Capability,
    HarnessBackedProviderAdapter,
    HarnessCapabilities,
    HarnessIdentity,
    HarnessReadiness,
    ModelDescriptor,
    ProviderIdentity,
    ProviderRequest,
    ProviderResponse,
    ReadinessState,
    Usage,
    WorkerHandle,
    WorkerRequest,
    WorkerResult,
    WorkerStatus,
)
from agent_cli.adapters.harness import HarnessAdapter


@dataclass
class DirectHarness(HarnessAdapter):
    state: ReadinessState = ReadinessState.READY
    calls: list[ProviderRequest] | None = None

    def identity(self) -> HarnessIdentity:
        return HarnessIdentity("example-cli", "Example CLI", "1.0")

    def readiness(self) -> HarnessReadiness:
        return HarnessReadiness(self.state, "example readiness")

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(frozenset({Capability.START_WORKER, Capability.DIRECT_MODEL}))

    def direct_provider(self) -> ProviderIdentity:
        return ProviderIdentity("example-subscription", "Example subscription", "harness", "subscription")

    def direct_models(self) -> tuple[ModelDescriptor, ...]:
        return (ModelDescriptor("example-subscription", "example-model", "Example Model", 1000),)

    def invoke_model(self, request: ProviderRequest) -> ProviderResponse:
        if self.calls is not None:
            self.calls.append(request)
        return ProviderResponse(request.model_id, "hello", Usage(3, 2))

    def start(self, request: WorkerRequest) -> WorkerHandle:
        raise AssertionError("not used")

    def status(self, handle: WorkerHandle) -> WorkerStatus:
        raise AssertionError("not used")

    def result(self, handle: WorkerHandle) -> WorkerResult:
        raise AssertionError("not used")


def test_bridge_maps_identity_readiness_models_and_invocation_generically() -> None:
    calls: list[ProviderRequest] = []
    harness = DirectHarness(calls=calls)
    provider = HarnessBackedProviderAdapter(harness)
    request = ProviderRequest("example-model", "hello", "system", 20)

    assert provider.identity() == ProviderIdentity(
        "example-subscription", "Example subscription", "harness", "subscription"
    )
    assert provider.readiness().state is ReadinessState.READY
    assert provider.list_models() == (
        ModelDescriptor("example-subscription", "example-model", "Example Model", 1000),
    )
    assert provider.invoke(request) == ProviderResponse("example-model", "hello", Usage(3, 2))
    assert calls == [request]


def test_bridge_readiness_mirrors_underlying_harness_auth_state() -> None:
    provider = HarnessBackedProviderAdapter(DirectHarness(ReadinessState.AUTH_REQUIRED))

    readiness = provider.readiness()

    assert readiness.state is ReadinessState.AUTH_REQUIRED
    assert "example readiness" in readiness.reason

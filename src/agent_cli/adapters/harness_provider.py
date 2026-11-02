"""Generic provider bridge for harnesses that can make bounded direct model calls."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agent_cli.adapters.domain import (
    ModelDescriptor,
    ProviderIdentity,
    ProviderReadiness,
    ProviderRequest,
    ProviderResponse,
)
from agent_cli.adapters.harness import HarnessAdapter
from agent_cli.adapters.provider import ProviderAdapter


@dataclass(slots=True)
class HarnessBackedProviderAdapter(ProviderAdapter):
    """Expose one DIRECT_MODEL-capable harness through the ordinary provider interface."""

    harness: HarnessAdapter

    def identity(self) -> ProviderIdentity:
        return self.harness.direct_provider()

    def readiness(self) -> ProviderReadiness:
        readiness = self.harness.readiness()
        return ProviderReadiness(readiness.state, readiness.reason)

    def list_models(self) -> Sequence[ModelDescriptor]:
        return self.harness.direct_models()

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        return self.harness.invoke_model(request)

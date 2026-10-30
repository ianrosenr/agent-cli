"""The interface every provider adapter implements.

Provider access is separate from harness execution: Agent CLI calls models directly for its
own work (classification, planning, research). A provider adapter reports facts and executes
requests; whether to use it, and what to do when it fails, is decided by core policy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from agent_cli.adapters.domain import (
    ModelDescriptor,
    ProviderIdentity,
    ProviderReadiness,
    ProviderRequest,
    ProviderResponse,
)


class ProviderAdapter(ABC):
    """Reaches models through one provider. Every method raises only ``AdapterError``."""

    @abstractmethod
    def identity(self) -> ProviderIdentity: ...

    @abstractmethod
    def readiness(self) -> ProviderReadiness:
        """Report authentication and health.

        Never raises, and returns promptly: any network check must be bounded by a short
        timeout (a timeout is reported as ``UNAVAILABLE``).
        """

    @abstractmethod
    def list_models(self) -> Sequence[ModelDescriptor]: ...

    @abstractmethod
    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        """Call exactly ``request.model_id``; never substitute another model."""

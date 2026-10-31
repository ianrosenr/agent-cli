"""The set of harness and provider adapters available in this session.

Orchestration looks adapters up here by ID, so adding an adapter means registering it, not
editing orchestration.
"""

from __future__ import annotations

from collections.abc import Iterable

from agent_cli.adapters.harness import HarnessAdapter
from agent_cli.adapters.provider import ProviderAdapter


class AdapterRegistry:
    def __init__(
        self,
        harnesses: Iterable[HarnessAdapter] = (),
        providers: Iterable[ProviderAdapter] = (),
    ) -> None:
        self._harnesses: dict[str, HarnessAdapter] = {}
        self._providers: dict[str, ProviderAdapter] = {}
        for harness in harnesses:
            self.register_harness(harness)
        for provider in providers:
            self.register_provider(provider)

    def reset(self, other: AdapterRegistry) -> None:
        """Replace every adapter with ``other``'s, in place, so holders of this registry see the
        new set (for example after setup changes configuration)."""
        self._harnesses = other.harnesses()
        self._providers = other.providers()

    def register_harness(self, adapter: HarnessAdapter) -> None:
        harness_id = adapter.identity().id
        if harness_id in self._harnesses:
            raise ValueError(f"Harness adapter already registered: {harness_id}")
        self._harnesses[harness_id] = adapter

    def register_provider(self, adapter: ProviderAdapter) -> None:
        provider_id = adapter.identity().id
        if provider_id in self._providers:
            raise ValueError(f"Provider adapter already registered: {provider_id}")
        self._providers[provider_id] = adapter

    def harness(self, harness_id: str) -> HarnessAdapter | None:
        return self._harnesses.get(harness_id)

    def provider(self, provider_id: str) -> ProviderAdapter | None:
        return self._providers.get(provider_id)

    def harnesses(self) -> dict[str, HarnessAdapter]:
        return dict(self._harnesses)

    def providers(self) -> dict[str, ProviderAdapter]:
        return dict(self._providers)

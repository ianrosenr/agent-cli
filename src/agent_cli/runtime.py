"""Building the session's real adapters and their cost estimators from effective configuration.

::

    resolved Config -> default_adapters(config, credentials, ...) -> AdapterRegistry

Adapters receive their resolved config entry; they never read configuration files themselves,
and routing never resolves credentials. Construction stays cheap: network checks happen later,
through ``readiness()``. Vendor-specific wiring lives here and in the adapters, never in
orchestration, routing, or budget policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_cli.adapters import AdapterRegistry, Capability, HarnessBackedProviderAdapter
from agent_cli.adapters.anthropic import ClientFactory, sdk_client
from agent_cli.adapters.cli_profiles import BUILTIN_CLI_PROFILES
from agent_cli.adapters.credentials import CredentialResolver
from agent_cli.adapters.factories import (
    HarnessFactoryRegistry,
    ProviderFactoryRegistry,
    default_harness_factories,
    default_provider_factories,
)
from agent_cli.config import Config, ConfigError, ConfigService
from agent_cli.config.model import HarnessConfig

if TYPE_CHECKING:
    from agent_cli.budget import CostEstimator, ProviderEstimators


def default_adapters(
    config: Config,
    credentials: CredentialResolver | None = None,
    anthropic_client: ClientFactory = sdk_client,
    harness_factories: HarnessFactoryRegistry | None = None,
    provider_factories: ProviderFactoryRegistry | None = None,
) -> AdapterRegistry:
    """Build adapters from config through reusable type/profile factories."""
    harness_factories = (
        default_harness_factories() if harness_factories is None else harness_factories
    )
    provider_factories = (
        default_provider_factories(credentials, anthropic_client)
        if provider_factories is None
        else provider_factories
    )
    registry = AdapterRegistry()

    harness_ids = set(BUILTIN_CLI_PROFILES) | set(config.harnesses)
    for harness_id in sorted(harness_ids):
        entry = config.harnesses.get(harness_id)
        effective = (
            HarnessConfig(profile=harness_id)
            if entry is None
            else entry
        )
        harness = harness_factories.build(harness_id, effective)
        registry.register_harness(harness)

        if (
            entry is not None
            and entry.enabled
            and harness.capabilities().supports(Capability.DIRECT_MODEL)
        ):
            registry.register_provider(HarnessBackedProviderAdapter(harness))

    for provider_id, entry in sorted(config.providers.items()):
        registry.register_provider(provider_factories.build(provider_id, entry))
    return registry


def session_adapters(
    config: ConfigService | None,
    credentials: CredentialResolver | None = None,
    anthropic_client: ClientFactory = sdk_client,
) -> AdapterRegistry:
    """Build the effective adapter registry; invalid config yields no partial runtime."""
    if config is None:
        return AdapterRegistry()
    try:
        resolved = config.resolve()
        return default_adapters(resolved, credentials, anthropic_client)
    except (ConfigError, ValueError):
        return AdapterRegistry()

def provider_estimators(adapters: AdapterRegistry) -> ProviderEstimators:
    """Trusted cost estimators for the registered providers that have one.

    Looked up per call, so adapters replaced after setup are priced by their own estimator.
    Every other provider fails closed (``pricing_unknown``).
    """
    from agent_cli.budget import ProviderEstimators
    from agent_cli.budget.anthropic import AnthropicCostEstimator

    built: list[tuple[object, CostEstimator]] = []

    def lookup(provider_id: str) -> CostEstimator | None:
        adapter = adapters.provider(provider_id)
        if adapter is None or adapter.identity().type != "anthropic":
            return None
        count_tokens = getattr(adapter, "count_input_tokens", None)
        if not callable(count_tokens):
            return None
        for owner, estimator in built:
            if owner is adapter:
                return estimator
        estimator = AnthropicCostEstimator(count_tokens)
        built[:] = [(adapter, estimator)]
        return estimator

    return ProviderEstimators(lookup)

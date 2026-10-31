"""Building the session's real adapters and their cost estimators from effective configuration.

::

    resolved Config -> default_adapters(config, credentials, ...) -> AdapterRegistry

Adapters receive their resolved config entry; they never read configuration files themselves,
and routing never resolves credentials. Construction stays cheap: network checks happen later,
through ``readiness()``. Vendor-specific wiring lives here and in the adapters, never in
orchestration, routing, or budget policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_cli.adapters import AdapterRegistry
from agent_cli.adapters.anthropic import PROVIDER_ID as ANTHROPIC
from agent_cli.adapters.anthropic import AnthropicProviderAdapter, ClientFactory, sdk_client
from agent_cli.adapters.claude_code import HARNESS_ID as CLAUDE_CODE
from agent_cli.adapters.claude_code import ClaudeCodeHarnessAdapter
from agent_cli.adapters.credentials import CredentialResolver
from agent_cli.config import Config, ConfigError, ConfigService
from agent_cli.config.model import HarnessConfig

if TYPE_CHECKING:
    from agent_cli.budget import CostEstimator, ProviderEstimators


def default_adapters(
    config: Config,
    credentials: CredentialResolver | None = None,
    anthropic_client: ClientFactory = sdk_client,
) -> AdapterRegistry:
    """Build the real adapters available to this session.

    Claude Code is always registered so setup/status can detect an installation without first
    persisting runtime facts. Its config entry only controls enablement or a command override.
    ``providers.anthropic`` remains opt-in because it is a paid API path.
    """
    credentials = CredentialResolver() if credentials is None else credentials
    claude_config = config.harnesses.get(CLAUDE_CODE, HarnessConfig())
    registry = AdapterRegistry([ClaudeCodeHarnessAdapter(claude_config)])
    entry = config.providers.get(ANTHROPIC)
    if entry is not None:
        registry.register_provider(AnthropicProviderAdapter(entry, credentials, anthropic_client))
    return registry


def session_adapters(
    config: ConfigService | None,
    credentials: CredentialResolver | None = None,
    anthropic_client: ClientFactory = sdk_client,
) -> AdapterRegistry:
    """``default_adapters`` for the effective configuration; none when it is invalid, so a
    partially configured runtime is never built (the configuration error is shown instead)."""
    if config is None:
        return AdapterRegistry()
    try:
        resolved = config.resolve()
    except ConfigError:
        return AdapterRegistry()
    return default_adapters(resolved, credentials, anthropic_client)


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
        if not isinstance(adapter, AnthropicProviderAdapter):
            return None
        for owner, estimator in built:
            if owner is adapter:
                return estimator
        estimator = AnthropicCostEstimator(adapter.count_input_tokens)
        built[:] = [(adapter, estimator)]
        return estimator

    return ProviderEstimators(lookup)

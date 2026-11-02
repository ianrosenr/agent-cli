"""Adapter factory registries.

Runtime depends on these reusable factories, not concrete vendor classes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from agent_cli.adapters.anthropic import AnthropicProviderAdapter, ClientFactory, sdk_client
from agent_cli.adapters.cli_harness import CliHarnessAdapter, CliHarnessProfile
from agent_cli.adapters.cli_profiles import BUILTIN_CLI_PROFILES
from agent_cli.adapters.credentials import CredentialResolver
from agent_cli.adapters.harness import HarnessAdapter
from agent_cli.adapters.provider import ProviderAdapter
from agent_cli.config.model import HarnessConfig, ProviderConfig


class AdapterFactoryError(ValueError):
    """A configured adapter type/profile is unknown or invalid."""


type HarnessFactory = Callable[[str, HarnessConfig], HarnessAdapter]
type ProviderFactory = Callable[[str, ProviderConfig], ProviderAdapter]


@dataclass(slots=True)
class HarnessFactoryRegistry:
    factories: dict[str, HarnessFactory] = field(default_factory=dict)

    def register(self, type_name: str, factory: HarnessFactory) -> None:
        if type_name in self.factories:
            raise ValueError(f"harness factory already registered: {type_name}")
        self.factories[type_name] = factory

    def build(self, harness_id: str, config: HarnessConfig) -> HarnessAdapter:
        factory = self.factories.get(config.type)
        if factory is None:
            raise AdapterFactoryError(
                f"harness {harness_id!r}: unknown type {config.type!r}"
            )
        return factory(harness_id, config)


@dataclass(slots=True)
class ProviderFactoryRegistry:
    factories: dict[str, ProviderFactory] = field(default_factory=dict)

    def register(self, type_name: str, factory: ProviderFactory) -> None:
        if type_name in self.factories:
            raise ValueError(f"provider factory already registered: {type_name}")
        self.factories[type_name] = factory

    def build(self, provider_id: str, config: ProviderConfig) -> ProviderAdapter:
        factory = self.factories.get(config.type)
        if factory is None:
            raise AdapterFactoryError(
                f"provider {provider_id!r}: unknown type {config.type!r}"
            )
        return factory(provider_id, config)


def cli_factory(
    profiles: dict[str, CliHarnessProfile] | None = None,
) -> HarnessFactory:
    known = BUILTIN_CLI_PROFILES if profiles is None else profiles

    def build(harness_id: str, config: HarnessConfig) -> HarnessAdapter:
        profile_name = config.profile or harness_id
        profile = known.get(profile_name)
        if profile is None:
            raise AdapterFactoryError(
                f"harness {harness_id!r}: unknown cli profile {profile_name!r}"
            )
        return CliHarnessAdapter(harness_id, profile, config)

    return build


def default_harness_factories() -> HarnessFactoryRegistry:
    registry = HarnessFactoryRegistry()
    registry.register("cli", cli_factory())
    return registry


def default_provider_factories(
    credentials: CredentialResolver | None = None,
    anthropic_client: ClientFactory = sdk_client,
) -> ProviderFactoryRegistry:
    resolved = CredentialResolver() if credentials is None else credentials
    registry = ProviderFactoryRegistry()

    def anthropic_factory(provider_id: str, config: ProviderConfig) -> ProviderAdapter:
        if provider_id != "anthropic":
            raise AdapterFactoryError(
                "anthropic provider type currently requires the configured id 'anthropic'"
            )
        return AnthropicProviderAdapter(config, resolved, anthropic_client)

    registry.register("anthropic", anthropic_factory)
    return registry

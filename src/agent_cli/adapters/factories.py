"""Adapter factory registries.

Runtime depends on these reusable factories, not concrete vendor classes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from agent_cli.adapters.anthropic import AnthropicProviderAdapter, ClientFactory, sdk_client
from agent_cli.adapters.cli_harness import AuthPattern, CliHarnessAdapter, CliHarnessProfile
from agent_cli.adapters.cli_profiles import BUILTIN_CLI_PROFILES
from agent_cli.adapters.credentials import CredentialResolver
from agent_cli.adapters.harness import HarnessAdapter
from agent_cli.adapters.provider import ProviderAdapter
from agent_cli.config.model import CliProfileConfig, HarnessConfig, ProviderConfig


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
            raise AdapterFactoryError(f"harness {harness_id!r}: unknown type {config.type!r}")
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
            raise AdapterFactoryError(f"provider {provider_id!r}: unknown type {config.type!r}")
        return factory(provider_id, config)


def profile_from_config(profile_id: str, config: CliProfileConfig) -> CliHarnessProfile:
    for expression in config.auth_regexes:
        try:
            re.compile(expression)
        except re.error as error:
            raise AdapterFactoryError(
                f"harness profile {profile_id!r}: invalid auth regex: {error}"
            ) from None
    return CliHarnessProfile(
        id=profile_id,
        display_name=config.display_name,
        default_command=config.default_command,
        provider_id=config.provider_id,
        provider_name=config.provider_name,
        auth_label=config.auth_label,
        auth_status_args=config.auth_status_args,
        login_args=config.login_args,
        auth_pattern=AuthPattern(
            source=config.auth_source,
            regexes=config.auth_regexes,
        ),
        env_remove=frozenset(config.env_remove),
        output_protocol=config.output_protocol,
        worker_base_args=config.worker_base_args,
        worker_research_args=config.worker_research_args,
        worker_diagnostic_args=config.worker_diagnostic_args,
        worker_write_args=config.worker_write_args,
        worker_model_flag=config.worker_model_flag,
        worker_stdin_marker=config.worker_stdin_marker,
        install_latest=config.install_latest,
        install_exact=config.install_exact,
        update_latest=config.update_latest,
        update_exact=config.update_exact,
        direct_model_id=config.direct_model_id,
        direct_model_name=config.direct_model_name,
        direct_context_limit=config.direct_context_limit,
        direct_base_args=config.direct_base_args,
        direct_model_flag=config.direct_model_flag,
        direct_stdin_marker=config.direct_stdin_marker,
    )


def cli_factory(
    profiles: dict[str, CliHarnessProfile] | None = None,
) -> HarnessFactory:
    known = BUILTIN_CLI_PROFILES if profiles is None else profiles

    def build(harness_id: str, config: HarnessConfig) -> HarnessAdapter:
        profile_name = config.profile
        profile = known.get(profile_name)
        if profile is None:
            raise AdapterFactoryError(
                f"harness {harness_id!r}: unknown cli profile {profile_name!r}"
            )
        return CliHarnessAdapter(harness_id, profile, config)

    return build


def default_harness_factories(
    configured_profiles: Mapping[str, CliProfileConfig] | None = None,
) -> HarnessFactoryRegistry:
    profiles = dict(BUILTIN_CLI_PROFILES)
    if configured_profiles is not None:
        profiles.update(
            {
                profile_id: profile_from_config(profile_id, profile_config)
                for profile_id, profile_config in configured_profiles.items()
            }
        )
    registry = HarnessFactoryRegistry()
    registry.register("cli", cli_factory(profiles))
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

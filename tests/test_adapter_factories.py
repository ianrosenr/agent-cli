from __future__ import annotations

import pytest

from agent_cli.adapters import HarnessIdentity, ProviderIdentity
from agent_cli.adapters.cli_harness import CliHarnessAdapter
from agent_cli.adapters.factories import (
    AdapterFactoryError,
    HarnessFactoryRegistry,
    ProviderFactoryRegistry,
    default_harness_factories,
)
from agent_cli.adapters.fake import FakeHarnessAdapter, FakeProviderAdapter
from agent_cli.config import Config
from agent_cli.config.model import HarnessConfig, ProviderConfig
from agent_cli.runtime import default_adapters


def harness(profile: str, **changes: object) -> HarnessConfig:
    values = {"type": "cli", "profile": profile, **changes}
    return HarnessConfig(**values)


def test_builtin_cli_harnesses_require_explicit_type_and_profile() -> None:
    registry = default_adapters(
        Config(
            harnesses={
                "claude-code": harness("claude-code"),
                "codex-cli": harness("codex-cli"),
            }
        )
    )

    assert isinstance(registry.harness("claude-code"), CliHarnessAdapter)
    assert isinstance(registry.harness("codex-cli"), CliHarnessAdapter)


def test_cli_profile_can_be_reused_under_another_configured_instance_name() -> None:
    registry = default_adapters(
        Config(
            harnesses={
                "work-claude": harness(
                    "claude-code",
                    command="/opt/work/claude",
                )
            }
        )
    )

    adapter = registry.harness("work-claude")
    assert isinstance(adapter, CliHarnessAdapter)
    assert adapter.command() == "/opt/work/claude"
    assert adapter.identity().id == "work-claude"
    assert adapter.provider_id() == "work-claude-subscription"


def test_unknown_harness_type_and_profile_fail_explicitly() -> None:
    with pytest.raises(AdapterFactoryError, match="unknown type"):
        default_adapters(
            Config(harnesses={"x": HarnessConfig(type="other", profile="anything")})
        )

    with pytest.raises(AdapterFactoryError, match="unknown cli profile"):
        default_adapters(Config(harnesses={"x": harness("not-real")}))


def test_new_supported_harness_type_can_be_registered_without_runtime_changes() -> None:
    factories = default_harness_factories()
    factories.register(
        "fake",
        lambda harness_id, config: FakeHarnessAdapter(
            harness_identity=HarnessIdentity(harness_id, harness_id, "1.0")
        ),
    )

    registry = default_adapters(
        Config(harnesses={"custom": HarnessConfig(type="fake", profile="custom")}),
        harness_factories=factories,
    )

    assert registry.harness("custom") is not None
    assert registry.harness("custom").identity().id == "custom"


def test_provider_factory_registry_supports_new_provider_type_without_runtime_changes() -> None:
    providers = ProviderFactoryRegistry()
    providers.register(
        "fake",
        lambda provider_id, config: FakeProviderAdapter(
            provider_identity=ProviderIdentity(provider_id, provider_id, "fake", "api")
        ),
    )

    registry = default_adapters(
        Config(providers={"custom": ProviderConfig(type="fake")}),
        provider_factories=providers,
    )

    assert registry.provider("custom") is not None


def test_factory_registry_rejects_duplicate_type_registration() -> None:
    registry = HarnessFactoryRegistry()
    def factory(harness_id: str, config: HarnessConfig) -> FakeHarnessAdapter:
        return FakeHarnessAdapter(
            harness_identity=HarnessIdentity(harness_id, harness_id, "1.0")
        )

    registry.register("fake", factory)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("fake", factory)

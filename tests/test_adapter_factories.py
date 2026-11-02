from __future__ import annotations

import pytest

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


def test_existing_harness_keys_infer_cli_profile_for_backward_compatibility() -> None:
    registry = default_adapters(
        Config(
            harnesses={
                "claude-code": HarnessConfig(),
                "codex-cli": HarnessConfig(),
            }
        )
    )

    assert isinstance(registry.harness("claude-code"), CliHarnessAdapter)
    assert isinstance(registry.harness("codex-cli"), CliHarnessAdapter)


def test_cli_profile_can_be_reused_under_another_configured_instance_name() -> None:
    registry = default_adapters(
        Config(
            harnesses={
                "work-claude": HarnessConfig(
                    type="cli",
                    profile="claude-code",
                    command="/opt/work/claude",
                )
            }
        )
    )

    harness = registry.harness("work-claude")
    assert isinstance(harness, CliHarnessAdapter)
    assert harness.command() == "/opt/work/claude"
    assert harness.identity().id == "work-claude"
    assert harness.provider_id() == "work-claude-subscription"


def test_unknown_harness_type_and_profile_fail_explicitly() -> None:
    with pytest.raises(AdapterFactoryError, match="unknown type"):
        default_adapters(Config(harnesses={"x": HarnessConfig(type="other")}))

    with pytest.raises(AdapterFactoryError, match="unknown cli profile"):
        default_adapters(
            Config(harnesses={"x": HarnessConfig(type="cli", profile="not-real")})
        )


def test_new_supported_harness_type_can_be_registered_without_runtime_changes() -> None:
    factories = default_harness_factories()
    factories.register("fake", lambda harness_id, config: FakeHarnessAdapter(harness_id=harness_id))

    registry = default_adapters(
        Config(harnesses={"custom": HarnessConfig(type="fake")}),
        harness_factories=factories,
    )

    assert registry.harness("custom") is not None
    assert registry.harness("custom").identity().id == "custom"


def test_provider_factory_registry_supports_new_provider_type_without_runtime_changes() -> None:
    providers = ProviderFactoryRegistry()
    providers.register(
        "fake",
        lambda provider_id, config: FakeProviderAdapter(provider_id=provider_id),
    )

    registry = default_adapters(
        Config(providers={"custom": ProviderConfig(type="fake")}),
        provider_factories=providers,
    )

    assert registry.provider("custom") is not None


def test_factory_registry_rejects_duplicate_type_registration() -> None:
    registry = HarnessFactoryRegistry()
    registry.register("fake", lambda harness_id, config: FakeHarnessAdapter(harness_id=harness_id))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            "fake", lambda harness_id, config: FakeHarnessAdapter(harness_id=harness_id)
        )

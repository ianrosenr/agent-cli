"""The first-party Anthropic provider path: credentials, adapter, pricing, registry, and setup.

Every test uses the in-memory ``FakeAnthropic`` client or builds the real SDK client without
sending a request. No test reaches the network or reads a developer's credentials.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest
import yaml

from agent_cli import cli
from agent_cli.adapters import (
    AdapterError,
    ErrorKind,
    ModelDescriptor,
    ProviderRequest,
    ReadinessState,
    providers_view,
)
from agent_cli.adapters.anthropic import (
    CACHE_TTL_S,
    FAILURE_TTL_S,
    FIRST_PARTY_BASE_URL,
    INVOKE_TIMEOUT_S,
    READINESS_TIMEOUT_S,
    AnthropicProviderAdapter,
    sdk_client,
)
from agent_cli.adapters.credentials import CredentialResolver, CredentialState, Secret
from agent_cli.budget import (
    BudgetGuard,
    BudgetReason,
    BudgetRejected,
    PaidCall,
    ReservationStatus,
    SettlementSource,
    SpendLedger,
)
from agent_cli.budget.anthropic import (
    ANTHROPIC_PRICING,
    AnthropicCostEstimator,
    with_margin,
)
from agent_cli.config import Config, ConfigPaths, ConfigService, Layer
from agent_cli.config.model import ProviderConfig, RoleFloors
from agent_cli.guided_setup import SETTINGS, GuidedSetup
from agent_cli.models import ModelCapability, load_registry, runtime_paths
from agent_cli.models.eligibility import eligible_models, requirement
from agent_cli.models.status import models_view
from agent_cli.runtime import default_adapters, provider_estimators, session_adapters
from agent_cli.ui import plain
from fake_anthropic import (
    KEY,
    SONNET,
    FakeAnthropic,
    FakeMessages,
    FakeModels,
    connection_error,
    message,
    model_info,
    resolver,
    status_error,
    timeout_error,
)

ENTRY = ProviderConfig(type="anthropic", api_key_env="ANTHROPIC_API_KEY")
REQUEST = ProviderRequest(SONNET, "plan the cache work", "you are a planner", 500)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def adapter(
    fake: FakeAnthropic | None = None,
    entry: ProviderConfig = ENTRY,
    credentials: CredentialResolver | None = None,
    clock: Clock | None = None,
) -> AnthropicProviderAdapter:
    fake = FakeAnthropic() if fake is None else fake
    return AnthropicProviderAdapter(
        entry,
        resolver() if credentials is None else credentials,
        fake.factory,
        clock or Clock(),
    )


def anthropic_config(**entry: Any) -> Config:
    return Config(providers={"anthropic": replace(ENTRY, **entry)})


# -- Credentials -------------------------------------------------------------------------------


def test_env_reference_resolves_the_named_variable_in_memory_only() -> None:
    credential = CredentialResolver({"TEAM_CLAUDE_KEY": " sk-team "}).resolve(
        ProviderConfig(type="anthropic", api_key_env="TEAM_CLAUDE_KEY")
    )
    assert credential.state is CredentialState.RESOLVED
    assert credential.secret is not None and credential.secret.reveal() == "sk-team"
    assert credential.reference == "env TEAM_CLAUDE_KEY"
    assert "sk-team" not in repr(credential) + str(credential.secret) + repr(credential.secret)


def test_keyring_reference_resolves_through_the_injected_lookup() -> None:
    asked: list[tuple[str, str]] = []

    def lookup(service: str, name: str) -> str | None:
        asked.append((service, name))
        return "sk-from-keyring"

    resolve = CredentialResolver({}, lookup).resolve
    named = resolve(ProviderConfig(type="anthropic", api_key_keyring="agent-cli/anthropic"))
    bare = resolve(ProviderConfig(type="anthropic", api_key_keyring="claude"))

    assert named.secret is not None and named.secret.reveal() == "sk-from-keyring"
    assert bare.resolved
    assert asked == [("agent-cli", "anthropic"), ("agent-cli", "claude")]


def test_a_broken_keyring_is_a_missing_secret_not_a_crash() -> None:
    def broken(service: str, name: str) -> str | None:
        raise RuntimeError("no backend")

    credential = CredentialResolver({}, broken).resolve(
        ProviderConfig(type="anthropic", api_key_keyring="claude")
    )
    assert credential.state is CredentialState.ABSENT


@pytest.mark.parametrize(
    ("entry", "state"),
    [
        (ProviderConfig(type="anthropic"), CredentialState.NO_REFERENCE),
        (
            ProviderConfig(type="anthropic", api_key_env="A", api_key_keyring="b"),
            CredentialState.CONFLICTING,
        ),
        (ProviderConfig(type="anthropic", api_key_env="MISSING"), CredentialState.ABSENT),
        (ProviderConfig(type="anthropic", api_key_env="EMPTY"), CredentialState.ABSENT),
    ],
)
def test_unusable_references_are_classified(entry: ProviderConfig, state: CredentialState) -> None:
    credential = CredentialResolver({"EMPTY": "  "}, lambda s, n: None).resolve(entry)
    assert credential.state is state
    assert credential.secret is None


def test_a_secret_cannot_be_pickled() -> None:
    import pickle

    with pytest.raises(TypeError):
        pickle.dumps(Secret(KEY))


# -- Construction ------------------------------------------------------------------------------


def write_config(home: Path, data: dict[str, Any]) -> ConfigService:
    path = home / ".agent-cli" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))
    return ConfigService(ConfigPaths(path, None))


ANTHROPIC_YAML = {
    "providers": {
        "anthropic": {"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY", "enabled": True}
    }
}


def test_normal_cli_startup_registers_the_anthropic_adapter(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(isolated_home, ANTHROPIC_YAML)
    monkeypatch.chdir(isolated_home)
    seen: dict[str, Any] = {}

    def run_shell(*args: Any) -> int:
        seen["adapters"] = args[5]
        return 0

    monkeypatch.setattr(cli, "run_shell", run_shell)
    assert cli.main([]) == 0

    provider = seen["adapters"].provider("anthropic")
    assert isinstance(provider, AnthropicProviderAdapter)
    assert provider.identity().id == "anthropic"
    assert provider.identity().access == "api"
    assert provider.identity().display_name == "Anthropic"


def test_no_anthropic_config_registers_nothing() -> None:
    assert default_adapters(Config()).providers() == {}


def test_invalid_config_builds_no_partial_runtime(isolated_home: Path) -> None:
    service = write_config(isolated_home, {**ANTHROPIC_YAML, "bogus": 1})
    assert session_adapters(service).providers() == {}


def test_setup_reload_registers_a_newly_configured_provider(isolated_home: Path) -> None:
    service = write_config(isolated_home, {})
    adapters = session_adapters(service)
    estimators = provider_estimators(adapters)
    call = PaidCall("anthropic", "api", SONNET, "planning", 10, 10)
    assert estimators.estimate(call) is None

    write_config(isolated_home, ANTHROPIC_YAML)
    cli.reloader(service, adapters)()

    assert isinstance(adapters.provider("anthropic"), AnthropicProviderAdapter)
    assert estimators.estimate(call) is not None


def test_sdk_client_is_first_party_with_no_automatic_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example.com")
    client = sdk_client(Secret(KEY))
    assert client.max_retries == 0
    assert str(client.base_url).rstrip("/") == FIRST_PARTY_BASE_URL
    assert client.api_key == KEY


# -- Readiness ---------------------------------------------------------------------------------


def test_valid_credentials_are_ready_after_one_bounded_models_check() -> None:
    fake = FakeAnthropic()
    readiness = adapter(fake).readiness()

    assert readiness.state is ReadinessState.READY
    assert "env ANTHROPIC_API_KEY" in readiness.reason
    assert fake.models.calls == [{"limit": 1, "timeout": READINESS_TIMEOUT_S}]
    assert fake.secrets[0].reveal() == KEY


@pytest.mark.parametrize(
    ("entry", "credentials", "state"),
    [
        (replace(ENTRY, type="openai-compatible"), None, ReadinessState.MISCONFIGURED),
        (replace(ENTRY, base_url="https://proxy.example.com"), None, ReadinessState.MISCONFIGURED),
        (replace(ENTRY, api_key_env=None), None, ReadinessState.MISCONFIGURED),
        (replace(ENTRY, api_key_keyring="claude"), None, ReadinessState.MISCONFIGURED),
        (ENTRY, resolver(OTHER="x"), ReadinessState.AUTH_REQUIRED),
    ],
)
def test_configuration_problems_are_reported_without_a_network_call(
    entry: ProviderConfig, credentials: CredentialResolver | None, state: ReadinessState
) -> None:
    fake = FakeAnthropic()
    assert adapter(fake, entry, credentials).readiness().state is state
    assert fake.models.calls == []
    assert fake.secrets == []


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (status_error(anthropic.AuthenticationError, 401, "authentication_error"), "auth"),
        (status_error(anthropic.PermissionDeniedError, 403, "permission_error"), "auth"),
        (timeout_error(), "unavailable"),
        (connection_error(), "unavailable"),
        (status_error(anthropic.InternalServerError, 500, "api_error"), "unavailable"),
        (status_error(anthropic.RateLimitError, 429, "rate_limit_error"), "unavailable"),
        (RuntimeError("surprise"), "unavailable"),
    ],
)
def test_readiness_maps_failures_and_never_raises(error: Exception, state: str) -> None:
    fake = FakeAnthropic(models=FakeModels(error=error))
    readiness = adapter(fake).readiness()

    expected = ReadinessState.AUTH_REQUIRED if state == "auth" else ReadinessState.UNAVAILABLE
    assert readiness.state is expected
    assert KEY not in readiness.describe()
    assert "leaked detail" not in readiness.describe()


def test_readiness_survives_a_client_that_cannot_be_built() -> None:
    def broken(secret: Secret) -> Any:
        raise ValueError(f"bad key {secret.reveal()}")

    provider = AnthropicProviderAdapter(ENTRY, resolver(), broken)
    readiness = provider.readiness()
    assert readiness.state is ReadinessState.UNAVAILABLE
    assert KEY not in readiness.describe()


def test_readiness_is_cached_briefly_in_memory() -> None:
    fake, clock = FakeAnthropic(), Clock()
    provider = adapter(fake, clock=clock)
    provider.readiness()
    provider.readiness()
    assert len(fake.models.calls) == 1
    clock.now = CACHE_TTL_S + 1
    provider.readiness()
    assert len(fake.models.calls) == 2


def test_providers_view_shows_real_readiness_without_the_secret() -> None:
    config = anthropic_config()
    adapters = default_adapters(config, resolver(), FakeAnthropic().factory)
    shown = plain(providers_view(adapters, config))
    assert "anthropic (Anthropic)" in shown
    assert "Ready (Claude API; key from env ANTHROPIC_API_KEY)" in shown
    assert KEY not in shown


def test_secrets_never_appear_in_adapter_repr() -> None:
    provider = adapter()
    provider.readiness()
    assert KEY not in repr(provider)
    assert KEY not in repr(resolver())


# -- Models ------------------------------------------------------------------------------------


def test_active_models_are_listed_with_exact_ids() -> None:
    fake = FakeAnthropic(
        models=FakeModels(
            [
                model_info(SONNET),
                model_info("claude-Future-9", max_input_tokens=None),
                model_info("claude-old-1", lifecycle="deprecated"),
            ]
        )
    )
    models = adapter(fake).list_models()
    assert [m.model_id for m in models] == [SONNET, "claude-Future-9"]
    assert models[0] == ModelDescriptor("anthropic", SONNET, "Claude Sonnet 5 5", 1_000_000)
    assert models[1].context_limit is None


def test_listed_models_never_become_registry_entries() -> None:
    fake = FakeAnthropic(models=FakeModels([model_info(SONNET), model_info("claude-opus-5-5")]))
    config = anthropic_config()
    adapters = default_adapters(config, resolver(), fake.factory)
    registry = load_registry()

    runtime = runtime_paths(adapters, config)
    assert {p.model_id for p in runtime.paths} == {SONNET, "claude-opus-5-5"}
    assert registry.get("claude-opus-5-5") is None
    shown = plain(models_view(ConfigService(ConfigPaths(Path("/nonexistent"), None)), adapters))
    assert "Reachable but unscored" in shown and "claude-opus-5-5" in shown


def test_models_view_shows_the_sonnet_runtime_path(tmp_path: Path) -> None:
    service = write_config(tmp_path, ANTHROPIC_YAML)
    adapters = default_adapters(service.resolve(), resolver(), FakeAnthropic().factory)
    shown = plain(models_view(service, adapters))
    assert "Runtime paths" in shown
    assert f"{SONNET}  anthropic (api)" in shown


def test_model_listing_failures_are_adapter_errors() -> None:
    fake = FakeAnthropic(models=FakeModels(error=connection_error()))
    with pytest.raises(AdapterError) as raised:
        adapter(fake).list_models()
    assert raised.value.kind is ErrorKind.UNAVAILABLE


# -- Invocation --------------------------------------------------------------------------------


def test_invoke_sends_exactly_one_standard_messages_request() -> None:
    fake = FakeAnthropic()
    response = adapter(fake).invoke(REQUEST)

    assert fake.messages.created == [
        {
            "model": SONNET,
            "messages": [{"role": "user", "content": "plan the cache work"}],
            "system": "you are a planner",
            "max_tokens": 500,
            "inference_geo": "global",
            "service_tier": "standard_only",
            "speed": "standard",
            "timeout": INVOKE_TIMEOUT_S,
        }
    ]
    assert response.model_id == SONNET
    assert response.text == "hello"
    assert response.usage is not None
    assert (response.usage.input_tokens, response.usage.output_tokens) == (120, 30)
    assert response.usage.tokens == 150
    assert response.usage.cost_usd is None
    assert response.usage.reference == "msg_01ABC"


def test_system_is_omitted_when_absent() -> None:
    fake = FakeAnthropic()
    adapter(fake).invoke(replace(REQUEST, system=None))
    assert "system" not in fake.messages.created[0]


@pytest.mark.parametrize("bound", [None, 0])
def test_missing_output_bound_is_rejected_before_any_network_call(bound: int | None) -> None:
    fake = FakeAnthropic()
    with pytest.raises(AdapterError) as raised:
        adapter(fake).invoke(replace(REQUEST, max_output_tokens=bound))
    assert raised.value.kind is ErrorKind.INVALID_REQUEST
    assert fake.messages.created == [] and fake.secrets == []


def test_only_text_blocks_are_kept() -> None:
    reply = message()
    reply.content = [
        SimpleNamespace(type="thinking", thinking="private"),
        SimpleNamespace(type="text", text="part one, "),
        SimpleNamespace(type="text", text="part two"),
    ]
    fake = FakeAnthropic(messages=FakeMessages(reply=reply))
    assert adapter(fake).invoke(REQUEST).text == "part one, part two"


def test_a_reply_without_text_is_a_provider_error() -> None:
    fake = FakeAnthropic(messages=FakeMessages(reply=message(text=None)))
    with pytest.raises(AdapterError) as raised:
        adapter(fake).invoke(REQUEST)
    assert raised.value.kind is ErrorKind.PROVIDER_ERROR


def test_a_substituted_model_is_never_accepted_silently() -> None:
    fake = FakeAnthropic(messages=FakeMessages(reply=message(model="claude-haiku-5-5")))
    with pytest.raises(AdapterError) as raised:
        adapter(fake).invoke(REQUEST)
    assert raised.value.kind is ErrorKind.PROVIDER_ERROR
    assert "claude-haiku-5-5" in raised.value.message


@pytest.mark.parametrize(
    "usage",
    [
        {"cache_read_input_tokens": 50},
        {"inference_geo": "us"},
        {"speed": "fast"},
        {"service_tier": "priority"},
    ],
)
def test_usage_outside_the_standard_shape_is_reported_as_unknown(usage: dict[str, Any]) -> None:
    reply = message()
    for name, value in usage.items():
        setattr(reply.usage, name, value)
    fake = FakeAnthropic(messages=FakeMessages(reply=reply))
    reported = adapter(fake).invoke(REQUEST).usage
    assert reported is not None
    assert reported.input_tokens is None and reported.output_tokens is None
    assert reported.reference == "msg_01ABC"


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (status_error(anthropic.AuthenticationError, 401, "authentication_error"), "auth_failed"),
        (status_error(anthropic.PermissionDeniedError, 403, "permission_error"), "auth_failed"),
        (status_error(anthropic.NotFoundError, 404, "not_found_error"), "not_found"),
        (status_error(anthropic.BadRequestError, 400, "invalid_request_error"), "invalid_request"),
        (status_error(anthropic.UnprocessableEntityError, 422, "invalid_request_error"),
         "invalid_request"),
        (status_error(anthropic.RateLimitError, 429, "rate_limit_error"), "rate_limited"),
        (timeout_error(), "timeout"),
        (connection_error(), "unavailable"),
        (status_error(anthropic.InternalServerError, 500, "api_error"), "provider_error"),
        (status_error(anthropic.OverloadedError, 529, "overloaded_error"), "provider_error"),
        (ValueError("unexpected"), "provider_error"),
    ],
)  # fmt: skip
def test_vendor_failures_map_to_normalized_errors(error: Exception, kind: str) -> None:
    fake = FakeAnthropic(messages=FakeMessages(reply=error))
    with pytest.raises(AdapterError) as raised:
        adapter(fake).invoke(REQUEST)

    assert raised.value.kind == ErrorKind(kind)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    text = raised.value.message
    for private in (KEY, "leaked detail", REQUEST.prompt, REQUEST.system or ""):
        assert private not in text
    assert len(fake.messages.created) == 1  # one attempt: no hidden retry
    if isinstance(error, anthropic.APIStatusError):
        assert "req_011XYZ" in text


def test_missing_credentials_fail_invocation_safely() -> None:
    with pytest.raises(AdapterError) as raised:
        adapter(credentials=resolver(OTHER="x")).invoke(REQUEST)
    assert raised.value.kind is ErrorKind.AUTH_REQUIRED
    assert "ANTHROPIC_API_KEY is not set" in raised.value.message


# -- Budget and pricing ------------------------------------------------------------------------


def paid(model: str = SONNET, output: int | None = 500) -> PaidCall:
    return PaidCall("anthropic", "api", model, "planning", None, output)


def test_input_bound_comes_from_the_token_count_api_for_the_exact_request() -> None:
    fake = FakeAnthropic(messages=FakeMessages(count=1_000))
    estimator = AnthropicCostEstimator(adapter(fake).count_input_tokens)

    assert estimator.input_bound(paid(), REQUEST) == with_margin(1_000) == 1_084
    assert fake.messages.counted == [
        {
            "model": SONNET,
            "messages": [{"role": "user", "content": "plan the cache work"}],
            "system": "you are a planner",
            "speed": "standard",
            "timeout": 15.0,
        }
    ]


def test_bounds_are_cached_per_exact_request() -> None:
    fake = FakeAnthropic()
    estimator = AnthropicCostEstimator(adapter(fake).count_input_tokens)
    estimator.input_bound(paid(), REQUEST)
    estimator.input_bound(paid(), REQUEST)
    assert len(fake.messages.counted) == 1
    estimator.input_bound(paid(), replace(REQUEST, prompt="another prompt"))
    estimator.input_bound(paid(), replace(REQUEST, system="another system"))
    assert len(fake.messages.counted) == 3


@pytest.mark.parametrize(
    "count", [connection_error(), status_error(anthropic.NotFoundError, 404, "not_found_error")]
)
def test_token_count_failure_leaves_the_bound_unknown(count: Exception) -> None:
    fake = FakeAnthropic(messages=FakeMessages(count=count))
    estimator = AnthropicCostEstimator(adapter(fake).count_input_tokens)
    assert estimator.input_bound(paid(), REQUEST) is None


def test_unpriced_models_are_never_counted_or_priced() -> None:
    fake = FakeAnthropic()
    estimator = AnthropicCostEstimator(adapter(fake).count_input_tokens)
    other = replace(REQUEST, model_id="claude-opus-5-5")
    assert estimator.input_bound(paid("claude-opus-5-5"), other) is None
    assert estimator.estimate(replace(paid("claude-opus-5-5"), input_tokens=10)) is None
    assert fake.messages.counted == []


def test_sonnet_quote_uses_the_reviewed_standard_price_source() -> None:
    estimator = AnthropicCostEstimator(lambda request: 0)
    estimate = estimator.estimate(replace(paid(output=4_000), input_tokens=10_000))

    assert estimate is not None
    # 10k input at $2/MTok + 4k output at $10/MTok = $0.02 + $0.04
    assert estimate.usd_micros == 60_000
    assert (estimate.source, estimate.source_version) == (
        ANTHROPIC_PRICING.source,
        ANTHROPIC_PRICING.version,
    )
    assert estimate.conservative


def test_other_providers_and_access_kinds_are_not_priced_here() -> None:
    estimator = AnthropicCostEstimator(lambda request: 0)
    assert estimator.estimate(replace(paid(), provider_id="proxy", input_tokens=1)) is None
    assert estimator.estimate(replace(paid(), access="subscription", input_tokens=1)) is None


def guard(tmp_path: Path, fake: FakeAnthropic) -> tuple[BudgetGuard, AnthropicProviderAdapter]:
    provider = adapter(fake)
    estimator = AnthropicCostEstimator(provider.count_input_tokens)
    ledger = SpendLedger(tmp_path / "spend.sqlite")
    return BudgetGuard.from_config(Config(), ledger, estimator), provider


def test_paid_generation_reserves_before_dispatch_and_settles_on_actual_usage(
    tmp_path: Path,
) -> None:
    fake = FakeAnthropic(messages=FakeMessages(count=1_000))
    budget, provider = guard(tmp_path, fake)
    seen: list[list[ReservationStatus]] = []

    def reply(kwargs: dict[str, Any]) -> Any:
        seen.append([r.status for r in budget.ledger.reservations()])
        return message(input_tokens=990, output_tokens=200)

    fake.messages.reply = reply
    budget.invoke(provider, REQUEST, "planning")

    assert seen == [[ReservationStatus.DISPATCHED]]
    [settled] = budget.ledger.reservations()
    assert settled.status is ReservationStatus.SETTLED
    assert settled.call.input_tokens == 1_084
    # bound: 1,084 in at $2/MTok + 500 out at $10/MTok, rounded up to whole micro-dollars
    assert settled.reserved_micros == 2_168 + 5_000
    assert settled.settlement_source is SettlementSource.TOKEN_PRICING
    assert settled.settled_micros == 1_980 + 2_000
    assert settled.reference == "msg_01ABC"


def test_unknown_input_bound_fails_closed_before_dispatch(tmp_path: Path) -> None:
    fake = FakeAnthropic(messages=FakeMessages(count=timeout_error()))
    budget, provider = guard(tmp_path, fake)
    with pytest.raises(BudgetRejected) as raised:
        budget.invoke(provider, REQUEST, "planning")
    assert raised.value.reason is BudgetReason.PRICING_UNKNOWN
    assert fake.messages.created == []


def test_a_failed_paid_call_settles_at_its_reserved_bound(tmp_path: Path) -> None:
    fake = FakeAnthropic(
        messages=FakeMessages(reply=status_error(anthropic.InternalServerError, 500, "api_error"))
    )
    budget, provider = guard(tmp_path, fake)
    with pytest.raises(AdapterError):
        budget.invoke(provider, REQUEST, "planning")
    [settled] = budget.ledger.reservations()
    assert settled.settlement_source is SettlementSource.RESERVED_BOUND
    assert settled.settled_micros == settled.reserved_micros


# -- Registry ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "eligible"),
    [
        ("orchestrator", True),
        ("spec_author", True),
        ("architect", True),
        ("research_investigator", True),
        ("security_investigator", False),
    ],
)
def test_sonnet_meets_planning_floors_but_not_high_security(role: str, eligible: bool) -> None:
    registry = load_registry()
    found = eligible_models(registry, requirement(role, RoleFloors()))
    assert (SONNET in [entry.id for entry in found]) is eligible


def test_sonnet_is_the_single_active_registry_model() -> None:
    registry = load_registry()
    assert [entry.id for entry in registry.active()] == [SONNET]
    entry = registry.get(SONNET)
    assert entry is not None
    assert max(entry.scores[capability] for capability in ModelCapability) <= 4
    assert all("platform.claude.com" in source for source in entry.sources)


# -- Setup -------------------------------------------------------------------------------------


class Conversation:
    def __init__(self, *answers: str) -> None:
        self.answers = iter(answers)
        self.prompts: list[str] = []
        self.output: list[str] = []

    def write_line(self, shown: Any) -> None:
        self.output.append(plain(shown))

    def read_line(self, prompt: str) -> str:
        self.prompts.append(prompt)
        answer = next(self.answers, None)
        if answer is None:
            raise EOFError
        return answer

    @property
    def text(self) -> str:
        return "\n".join(self.output)


def setup(service: ConfigService, environ: dict[str, str], *answers: str) -> Conversation:
    conversation = Conversation(*answers)
    GuidedSetup(service, conversation.read_line, conversation.write_line, environ=environ).run()
    return conversation


ENTER_THROUGH = [""] * (len(SETTINGS) + 1)  # target layer and every setting


def test_setup_configures_anthropic_by_reference_without_seeing_the_secret(
    service: ConfigService,
) -> None:
    talk = setup(service, {"ANTHROPIC_API_KEY": KEY}, *ENTER_THROUGH, "", "")

    assert "ANTHROPIC_API_KEY detected  yes" in talk.text
    assert "Configure Anthropic using ANTHROPIC_API_KEY (y/n): " in talk.prompts
    assert KEY not in talk.text + "".join(talk.prompts)
    assert service.read(Layer.GLOBAL)["providers"]["anthropic"] == {
        "type": "anthropic",
        "api_key_env": "ANTHROPIC_API_KEY",
        "enabled": True,
    }
    assert KEY not in service.paths.global_file.read_text()


def test_setup_without_the_variable_says_what_to_set_and_skips(service: ConfigService) -> None:
    talk = setup(service, {}, *ENTER_THROUGH, "")

    assert "ANTHROPIC_API_KEY detected  no" in talk.text
    assert "set ANTHROPIC_API_KEY in your environment" in talk.text
    assert not any("Configure Anthropic" in prompt for prompt in talk.prompts)
    assert "providers" not in service.read(Layer.GLOBAL)


def test_setup_can_decline_anthropic(service: ConfigService) -> None:
    setup(service, {"ANTHROPIC_API_KEY": KEY}, *ENTER_THROUGH, "n", "")
    assert "providers" not in service.read(Layer.GLOBAL)


def test_setup_rerun_preserves_a_custom_credential_reference(service: ConfigService) -> None:
    custom = {"type": "anthropic", "api_key_keyring": "agent-cli/claude", "enabled": True}
    service.save(Layer.GLOBAL, {"providers": {"anthropic": custom}})
    talk = setup(service, {"ANTHROPIC_API_KEY": KEY}, *ENTER_THROUGH, "")

    assert not any("Configure Anthropic" in prompt for prompt in talk.prompts)
    assert service.read(Layer.GLOBAL)["providers"]["anthropic"] == custom


# -- Review fixes ------------------------------------------------------------------------------


def test_startup_reloads_adapters_only_after_a_save(service: ConfigService) -> None:
    from agent_cli.commands import CommandContext, build_registry

    reloads: list[int] = []
    context = CommandContext(
        build_registry(),
        service,
        Conversation("n", *[""] * len(SETTINGS), "n").read_line,
        lambda shown: None,
        reload_adapters=lambda: reloads.append(1),
    )
    context.registry.dispatch("/startup", context)
    assert reloads == []

    saving = replace(context, read_line=Conversation(*ENTER_THROUGH, "").read_line)
    saving.registry.dispatch("/startup", saving)
    assert reloads == [1]


def test_shell_without_a_reloader_keeps_its_adapters(service: ConfigService) -> None:
    from agent_cli.commands import build_registry

    adapters = default_adapters(anthropic_config(), resolver(), FakeAnthropic().factory)
    answers = Conversation("/startup", *ENTER_THROUGH, "")
    cli.run_shell(build_registry(), answers.read_line, lambda s: None, "", service, adapters)
    assert isinstance(adapters.provider("anthropic"), AnthropicProviderAdapter)


def test_reload_keeps_adapters_when_config_is_invalid(isolated_home: Path) -> None:
    service = write_config(isolated_home, ANTHROPIC_YAML)
    adapters = session_adapters(service)
    write_config(isolated_home, {**ANTHROPIC_YAML, "bogus": 1})
    cli.reloader(service, adapters)()
    assert isinstance(adapters.provider("anthropic"), AnthropicProviderAdapter)


def test_the_keyring_is_asked_once_per_adapter() -> None:
    asked: list[str] = []

    def lookup(service: str, name: str) -> str | None:
        asked.append(name)
        return KEY

    clock = Clock()
    provider = adapter(
        entry=ProviderConfig(type="anthropic", api_key_keyring="claude"),
        credentials=CredentialResolver({}, lookup),
        clock=clock,
    )
    provider.readiness()
    clock.now = CACHE_TTL_S + 1
    provider.readiness()
    provider.invoke(REQUEST)
    assert asked == ["claude"]


def test_unavailable_readiness_is_retried_soon() -> None:
    fake, clock = FakeAnthropic(models=FakeModels(error=timeout_error())), Clock()
    provider = adapter(fake, clock=clock)
    assert provider.readiness().state is ReadinessState.UNAVAILABLE
    fake.models.error = None
    clock.now = FAILURE_TTL_S + 0.1
    assert provider.readiness().state is ReadinessState.READY


def test_boolean_token_counts_are_not_counts() -> None:
    fake = FakeAnthropic(messages=FakeMessages(reply=message(input_tokens=True), count=True))
    provider = adapter(fake)
    reported = provider.invoke(REQUEST).usage
    assert reported is not None and reported.input_tokens is None
    with pytest.raises(AdapterError):
        provider.count_input_tokens(REQUEST)

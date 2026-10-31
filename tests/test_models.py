from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_cli.adapters import (
    AdapterRegistry,
    ModelDescriptor,
    ProviderIdentity,
    ProviderReadiness,
    ReadinessState,
)
from agent_cli.adapters.fake import FakeProviderAdapter
from agent_cli.commands import CommandContext, build_registry
from agent_cli.config import Config, ConfigPaths, ConfigService
from agent_cli.config.model import ClassifierConfig, ProviderConfig, RoleFloors
from agent_cli.models import (
    FLOOR_SCORES,
    ROLE_CAPABILITIES,
    ClassifierChoice,
    ModelCapability,
    ModelPin,
    ModelRegistry,
    ModelRoutingError,
    NoModelAvailable,
    ProviderSignalClassifier,
    RegistryError,
    RuntimePaths,
    SelectionKind,
    build_classifier,
    eligible_models,
    load_registry,
    models_view,
    parse_registry,
    ranked_model_candidates,
    requirement,
    runtime_paths,
    select_classifier,
    select_model,
)
from agent_cli.models.access import ModelPath, choose_path
from agent_cli.models.provider_classifier import ClassifierOutputError, parse_reply
from agent_cli.skills import ClassificationInput, ClassifierMode, load_policy
from agent_cli.skills.classifier import classify
from agent_cli.terms import CANONICAL_ROLES, AccessKind
from agent_cli.ui import plain

# Fixture models: deterministic test data, not claims about real models.
PREFERENCE: tuple[AccessKind, ...] = ("subscription", "local", "api")


def models_lines(
    config: ConfigService | None, adapters: AdapterRegistry | None, path: Path | None = None
) -> list[str]:
    """``/models`` as rendered plain-text lines."""
    return plain(models_view(config, adapters, path)).splitlines()


def model(id: str, **overrides: Any) -> dict[str, Any]:
    scores = dict.fromkeys((c.value for c in ModelCapability), 3)
    scores.update(overrides.pop("scores", {}))
    entry = {
        "id": id,
        "display_name": id.title(),
        "status": "active",
        "scores": scores,
        "latency": "medium",
        "cost": "medium",
        "confidence": "medium",
        "sources": ["fixture"],
    }
    entry.update(overrides)
    return entry


def registry(*entries: dict[str, Any]) -> ModelRegistry:
    return parse_registry({"version": 1, "models": list(entries)})


def provider(
    id: str,
    access: AccessKind | None,
    *model_ids: str,
    state: ReadinessState = ReadinessState.READY,
    reply: str = '{"results": []}',
) -> FakeProviderAdapter:
    return FakeProviderAdapter(
        provider_identity=ProviderIdentity(id, id.title(), "fake", access),
        provider_readiness=ProviderReadiness(state, "" if state is ReadinessState.READY else "x"),
        models=[ModelDescriptor(id, model_id) for model_id in model_ids],
        reply=reply,
    )


def paths(*adapters: FakeProviderAdapter, config: Config | None = None) -> RuntimePaths:
    return runtime_paths(AdapterRegistry(providers=adapters), config or Config())


# -- Registry schema ---------------------------------------------------------------------------


def test_builtin_registry_loads_and_holds_only_evidence_backed_entries() -> None:
    builtin = load_registry()
    assert builtin.version
    for entry in builtin:
        assert entry.sources


def test_valid_entry_is_parsed_with_categories_and_aliases() -> None:
    parsed = registry(model("alpha", aliases=["Alpha-Latest"], cost="low", latency="fast"))
    entry = parsed.get("alpha-latest")
    assert entry is not None and entry.id == "alpha"
    assert entry.scores[ModelCapability.CODING] == 3
    assert (entry.cost.value, entry.latency.value, entry.status.value) == ("low", "fast", "active")


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        (model("a", extra=1), "unknown key(s) extra"),
        ({k: v for k, v in model("a").items() if k != "confidence"}, "missing key(s) confidence"),
        (model("a", sources=[]), "sources must name the evidence"),
        (model("a", status="preferred"), "status must be one of active, candidate"),
        (model("a", cost="cheap"), "cost must be one of free, low, medium, high"),
        (model("a", latency=1), "latency must be one of"),
        (model("a", display_name=""), "display_name must be a non-empty string"),
        (model(" a "), "id must not have surrounding whitespace"),
        (model("a", aliases=[" b "]), "aliases[0] must not have surrounding whitespace"),
        (model("a", aliases="b"), "aliases must be a list"),
        (model("a", scores={"vibes": 3}), "scores must have exactly"),
    ],
)
def test_invalid_entries_are_rejected(entry: dict[str, Any], problem: str) -> None:
    with pytest.raises(RegistryError, match=re.escape(problem)):
        registry(entry)


@pytest.mark.parametrize("score", [0, 6, -1, 3.5, True, "4"])
def test_scores_outside_one_through_five_or_non_integer_are_rejected(score: object) -> None:
    with pytest.raises(RegistryError, match=r"scores\.coding"):
        registry(model("a", scores={"coding": score}))


@pytest.mark.parametrize(
    "data",
    [{"version": 1}, {"models": []}, {"version": 1, "models": [], "x": 1}, {"version": True}],
)
def test_invalid_top_level_is_rejected(data: dict[str, Any]) -> None:
    with pytest.raises(RegistryError):
        parse_registry(data)


@pytest.mark.parametrize(
    "entries",
    [
        (model("a"), model("a")),
        (model("a"), model("A")),
        (model("a", aliases=["shared"]), model("b", aliases=["shared"])),
        (model("a"), model("b", aliases=["a"])),
        (model("a", aliases=["x", "x"]),),
    ],
)
def test_duplicate_ids_and_alias_collisions_are_rejected(entries: tuple[dict[str, Any]]) -> None:
    with pytest.raises(RegistryError, match=r"already used|repeated"):
        registry(*entries)


def test_load_registry_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "models": [model("a", scores={"coding": 9})]}))
    with pytest.raises(RegistryError, match=str(path)):
        load_registry(path)


# -- Roles and floors --------------------------------------------------------------------------


def test_model_and_skill_routing_share_exactly_the_canonical_roles() -> None:
    assert tuple(ROLE_CAPABILITIES) == CANONICAL_ROLES
    assert set(load_policy().roles) == set(CANONICAL_ROLES)
    assert tuple(f.name for f in dataclasses.fields(RoleFloors)) == CANONICAL_ROLES


def test_named_floors_map_to_two_three_four() -> None:
    assert dict(FLOOR_SCORES) == {"low": 2, "mid": 3, "high": 4}


def test_primary_and_secondary_capabilities_follow_the_contract() -> None:
    expected = {
        "orchestrator": ("reasoning", "architecture"),
        "spec_author": ("reasoning", "repository_engineering"),
        "spec_reviewer": ("reasoning", "repository_engineering"),
        "architect": ("architecture", "reasoning"),
        "implementer": ("coding", "repository_engineering"),
        "code_reviewer": ("coding", "debugging"),
        "research_investigator": ("reasoning", "tool_use"),
        "security_investigator": ("security_reasoning", "reasoning"),
        "skill_author": ("reasoning", "repository_engineering"),
        "skill_evaluator": ("reasoning", "debugging"),
    }
    actual = {role: (p.value, s.value) for role, (p, s) in ROLE_CAPABILITIES.items()}
    assert actual == expected


def test_default_floors_follow_the_contract() -> None:
    floors = {role: requirement(role, RoleFloors()).minimum for role in CANONICAL_ROLES}
    assert floors == {
        "orchestrator": 4,
        "spec_author": 4,
        "spec_reviewer": 3,
        "architect": 4,
        "implementer": 2,
        "code_reviewer": 3,
        "research_investigator": 4,
        "security_investigator": 4,
        "skill_author": 4,
        "skill_evaluator": 4,
    }


def test_unknown_role_fails_clearly() -> None:
    with pytest.raises(ModelRoutingError, match="unknown role 'intern'"):
        requirement("intern", RoleFloors())


def test_configured_floor_overrides_the_default() -> None:
    floors = dataclasses.replace(RoleFloors(), implementer="high")
    weak = registry(model("weak", scores={"coding": 3}))
    assert eligible_models(weak, requirement("implementer", RoleFloors()))
    assert not eligible_models(weak, requirement("implementer", floors))


# -- Automatic eligibility and ranking ---------------------------------------------------------


def test_automatic_selection_never_goes_below_the_floor() -> None:
    reg = registry(model("weak", scores={"reasoning": 3}), model("strong", scores={"reasoning": 4}))
    need = requirement("orchestrator", RoleFloors())
    assert [entry.id for entry in eligible_models(reg, need)] == ["strong"]
    runtime = paths(provider("p", "api", "weak"))
    assert ranked_model_candidates(reg, "orchestrator", RoleFloors(), runtime, PREFERENCE) == ()


def test_candidate_models_are_never_selected_automatically() -> None:
    reg = registry(model("cand", status="candidate", scores={"coding": 5}), model("ok"))
    need = requirement("implementer", RoleFloors())
    assert [entry.id for entry in eligible_models(reg, need)] == ["ok"]
    runtime = paths(provider("p", "api", "cand"))
    assert ranked_model_candidates(reg, "implementer", RoleFloors(), runtime, PREFERENCE) == ()


def test_unknown_runtime_models_are_never_selected_automatically() -> None:
    runtime = paths(provider("p", "api", "mystery"))
    assert (
        ranked_model_candidates(registry(), "implementer", RoleFloors(), runtime, PREFERENCE) == ()
    )


def test_ranking_uses_every_tie_break_in_order() -> None:
    reg = registry(
        model("z-id", confidence="high", latency="fast", cost="low"),
        model("a-id", confidence="high", latency="fast", cost="low"),
        model("costly", confidence="high", latency="fast", cost="high"),
        model("slow", confidence="high", latency="slow", cost="free"),
        model("unsure", confidence="low", latency="fast", cost="free"),
        model("debugger", scores={"debugging": 4}, confidence="low", latency="slow"),
        model("best", scores={"coding": 5}, confidence="low", latency="slow", cost="high"),
    )
    ranked = eligible_models(reg, requirement("code_reviewer", RoleFloors()))
    assert [e.id for e in ranked] == [
        "best",
        "debugger",
        "a-id",
        "z-id",
        "costly",
        "slow",
        "unsure",
    ]


def test_ranked_candidates_skip_models_without_a_usable_path() -> None:
    reg = registry(model("best", scores={"coding": 5}), model("next", scores={"coding": 4}))
    runtime = paths(provider("p", "api", "next"))
    candidates = ranked_model_candidates(reg, "implementer", RoleFloors(), runtime, PREFERENCE)
    assert [candidate.model_id for candidate in candidates] == ["next"]
    assert [path.provider.id for path in candidates[0].paths] == ["p"]


def test_automatic_candidate_keeps_all_usable_provider_paths() -> None:
    reg = registry(model("m", scores={"coding": 5}))
    runtime = paths(
        provider("sub", "subscription", "m"),
        provider("paid", "api", "m"),
    )

    (candidate,) = ranked_model_candidates(reg, "implementer", RoleFloors(), runtime, PREFERENCE)

    assert [path.provider.id for path in candidate.paths] == ["sub", "paid"]


def test_automatic_candidates_do_not_finalize_model_or_provider_choice() -> None:
    reg = registry(
        model("best", scores={"coding": 5}),
        model("subscription-option", scores={"coding": 4}),
    )
    runtime = paths(
        provider("paid", "api", "best"),
        provider("sub", "subscription", "subscription-option"),
    )

    candidates = ranked_model_candidates(reg, "implementer", RoleFloors(), runtime, PREFERENCE)

    assert [candidate.model_id for candidate in candidates] == ["best", "subscription-option"]
    assert candidates[0].paths[0].provider.id == "paid"
    assert candidates[1].paths[0].provider.id == "sub"


def test_alias_reported_by_a_provider_matches_the_canonical_model() -> None:
    reg = registry(model("canon", aliases=["vendor/canon-2"]))
    runtime = paths(provider("p", "api", "vendor/canon-2"))
    (candidate,) = ranked_model_candidates(reg, "implementer", RoleFloors(), runtime, PREFERENCE)
    assert candidate.model_id == "canon"
    assert candidate.paths[0].model_id == "vendor/canon-2"


# -- Explicit pins -----------------------------------------------------------------------------


def test_pin_below_floor_is_honored_with_a_recorded_warning() -> None:
    reg = registry(model("small", scores={"reasoning": 2}), model("big", scores={"reasoning": 5}))
    runtime = paths(provider("p", "api", "small", "big"))
    pin = ModelPin("Small", scope="task:t1/role:orchestrator", source="user message")
    chosen = select_model(reg, "orchestrator", RoleFloors(), runtime, PREFERENCE, pin)
    assert (chosen.kind, chosen.model_id, chosen.pin) == (SelectionKind.PINNED, "small", pin)
    assert len(chosen.warnings) == 1
    assert "below the orchestrator floor (reasoning 2 < 4)" in chosen.warnings[0]
    assert "task:t1/role:orchestrator" in chosen.warnings[0]


def test_pin_above_floor_has_no_warning() -> None:
    reg = registry(model("big", scores={"coding": 5}))
    runtime = paths(provider("p", "api", "big"))
    chosen = select_model(
        reg, "implementer", RoleFloors(), runtime, PREFERENCE, ModelPin("big", "t")
    )
    assert chosen.kind is SelectionKind.PINNED and not chosen.warnings


def test_pinned_unscored_runtime_model_is_allowed_without_inventing_scores() -> None:
    runtime = paths(provider("p", "local", "homebrew-7b"))
    pin = ModelPin("homebrew-7b", "task:t1")
    chosen = select_model(registry(), "architect", RoleFloors(), runtime, PREFERENCE, pin)
    assert (chosen.kind, chosen.model_id, chosen.entry) == (
        SelectionKind.PINNED_UNSCORED,
        "homebrew-7b",
        None,
    )
    assert "unscored" in chosen.warnings[0]
    # The pin does not make the model eligible for unrelated automatic selection.
    assert ranked_model_candidates(registry(), "architect", RoleFloors(), runtime, PREFERENCE) == ()


def test_pinned_candidate_is_treated_as_unscored() -> None:
    reg = registry(model("cand", status="candidate"))
    runtime = paths(provider("p", "api", "cand"))
    chosen = select_model(
        reg, "implementer", RoleFloors(), runtime, PREFERENCE, ModelPin("cand", "t")
    )
    assert chosen.kind is SelectionKind.PINNED_UNSCORED and chosen.entry is not None


def test_unavailable_pin_is_reported_and_never_substituted() -> None:
    reg = registry(model("wanted"), model("other", scores={"coding": 5}))
    runtime = paths(
        provider("p", "api", "other"),
        provider("q", "api", "wanted", state=ReadinessState.AUTH_REQUIRED),
    )
    pin = ModelPin("wanted", "task:t1")
    with pytest.raises(NoModelAvailable, match="ask the user") as caught:
        select_model(reg, "implementer", RoleFloors(), runtime, PREFERENCE, pin)
    assert caught.value.pin == pin
    assert "q: Sign-in required" in str(caught.value)


def test_pin_still_respects_access_preference_and_context_limits() -> None:
    reg = registry(model("m"))
    adapters = AdapterRegistry(
        providers=[
            FakeProviderAdapter(
                provider_identity=ProviderIdentity("small", "Small", "fake", "subscription"),
                models=[ModelDescriptor("small", "m", context_limit=1000)],
            ),
            FakeProviderAdapter(
                provider_identity=ProviderIdentity("big", "Big", "fake", "api"),
                models=[ModelDescriptor("big", "m", context_limit=100_000)],
            ),
        ]
    )
    runtime = runtime_paths(adapters, Config())
    pin = ModelPin("m", "t")
    assert (
        select_model(reg, "implementer", RoleFloors(), runtime, PREFERENCE, pin).path.provider.id
        == "small"
    )
    roomy = select_model(reg, "implementer", RoleFloors(), runtime, PREFERENCE, pin, 50_000)
    assert roomy.path.provider.id == "big"
    with pytest.raises(NoModelAvailable):
        select_model(reg, "implementer", RoleFloors(), runtime, ("subscription",), pin, 50_000)


@pytest.mark.parametrize(
    ("access", "preference", "context", "explanation"),
    [
        (None, PREFERENCE, None, "p serves it, but does not report its access kind"),
        ("api", ("subscription", "local"), None, "p serves it over api, which access.preference"),
        ("api", PREFERENCE, 50_000, "p serves it with a 1000-token context, below the 50000"),
    ],
)
def test_unusable_pin_explains_why_each_path_was_excluded(
    access: AccessKind | None,
    preference: tuple[AccessKind, ...],
    context: int | None,
    explanation: str,
) -> None:
    adapter = FakeProviderAdapter(
        provider_identity=ProviderIdentity("p", "P", "fake", access),
        models=[ModelDescriptor("p", "m", context_limit=1000)],
    )
    runtime = paths(adapter)
    with pytest.raises(NoModelAvailable, match=re.escape(explanation)):
        select_model(
            registry(),
            "implementer",
            RoleFloors(),
            runtime,
            preference,
            ModelPin("m", "t"),
            context,
        )


def test_pin_names_are_matched_ignoring_surrounding_whitespace() -> None:
    runtime = paths(provider("p", "api", "homebrew-7b"))
    pin = ModelPin("  homebrew-7b ", "t")
    assert (
        select_model(
            registry(), "implementer", RoleFloors(), runtime, PREFERENCE, pin
        ).path.model_id
        == "homebrew-7b"
    )


# -- Provider access ---------------------------------------------------------------------------


def test_access_preference_orders_subscription_local_api() -> None:
    runtime = paths(
        provider("a-api", "api", "m"),
        provider("b-local", "local", "m"),
        provider("c-sub", "subscription", "m"),
    )
    candidates = runtime.serving(["m"])
    assert choose_path(candidates, PREFERENCE).provider.id == "c-sub"  # type: ignore[union-attr]
    assert choose_path(candidates, ("local", "api")).provider.id == "b-local"  # type: ignore[union-attr]
    assert choose_path(candidates, ("api",)).provider.id == "a-api"  # type: ignore[union-attr]


def test_access_kinds_outside_the_preference_or_unknown_are_never_used() -> None:
    runtime = paths(provider("paid", "api", "m"), provider("mystery", None, "m"))
    assert choose_path(runtime.serving(["m"]), ("subscription", "local")) is None
    assert choose_path(runtime.serving(["m"]), PREFERENCE).provider.id == "paid"  # type: ignore[union-attr]


def test_only_ready_enabled_providers_contribute_paths() -> None:
    disabled = Config(providers={"off": ProviderConfig(type="fake", enabled=False)})
    runtime = paths(
        provider("ready", "api", "m"),
        provider("auth", "api", "m", state=ReadinessState.AUTH_REQUIRED),
        provider("detected", "api", "m", state=ReadinessState.DETECTED),
        provider("off", "api", "m"),
        config=disabled,
    )
    assert [path.provider.id for path in runtime.paths] == ["ready"]
    assert {problem.split(":")[0] for problem in runtime.problems} == {"auth", "detected"}


def test_unavailable_descriptors_and_failing_adapters_are_skipped() -> None:
    broken = provider("broken", "api", "m")
    broken.error = RuntimeError("boom")  # type: ignore[assignment]
    listed = FakeProviderAdapter(
        provider_identity=ProviderIdentity("p", "P", "fake", "api"),
        models=[ModelDescriptor("p", "gone", available=False), ModelDescriptor("p", "here")],
    )
    runtime = paths(broken, listed)
    assert [path.model_id for path in runtime.paths] == ["here"]
    assert runtime.problems == ("broken: adapter error: boom",)


# -- Classifier selection ----------------------------------------------------------------------


def cheap(id: str, **overrides: Any) -> dict[str, Any]:
    overrides.setdefault("cost", "low")
    overrides["scores"] = {"reasoning": 2, **overrides.get("scores", {})}
    return model(id, **overrides)


def test_auto_uses_the_cheapest_qualifying_free_or_low_model() -> None:
    reg = registry(
        cheap("low-fast", latency="fast"),
        cheap("free-slow", cost="free", latency="slow"),
        cheap("too-weak", cost="free", scores={"reasoning": 1}),
        cheap("cand", cost="free", status="candidate"),
    )
    runtime = paths(provider("p", "api", "low-fast", "free-slow", "too-weak", "cand"))
    choice = select_classifier(ClassifierConfig(), reg, runtime, PREFERENCE)
    assert (choice.mode, choice.model_id, choice.deterministic) == ("auto", "free-slow", False)


def test_auto_ranks_cost_latency_reasoning_confidence_then_id() -> None:
    from agent_cli.models import auto_candidates

    reg = registry(
        cheap("b", latency="fast", confidence="high"),
        cheap("a", latency="fast", confidence="high"),
        cheap("unsure", latency="fast", confidence="low"),
        cheap("smart", latency="fast", confidence="low", scores={"reasoning": 4}),
        cheap("slow", latency="slow", scores={"reasoning": 5}),
        cheap("free", cost="free", latency="slow"),
    )
    assert [e.id for e in auto_candidates(reg)] == ["free", "smart", "a", "b", "unsure", "slow"]


@pytest.mark.parametrize("cost", ["medium", "high"])
def test_auto_never_promotes_a_medium_or_high_cost_model(cost: str) -> None:
    reg = registry(model("expensive", cost=cost, scores={"reasoning": 5}))
    runtime = paths(provider("p", "subscription", "expensive"))
    choice = select_classifier(ClassifierConfig(), reg, runtime, PREFERENCE)
    assert choice.deterministic and choice.recommend_low_cost
    assert build_classifier(choice, AdapterRegistry()) is None


def test_auto_with_no_match_falls_back_to_deterministic_routing() -> None:
    reg = registry(cheap("tiny"))
    adapter = provider("p", "api", "tiny", state=ReadinessState.UNAVAILABLE)
    runtime = paths(adapter)
    choice = select_classifier(ClassifierConfig(), reg, runtime, PREFERENCE)
    assert choice.deterministic and choice.recommend_low_cost
    assert build_classifier(choice, AdapterRegistry(providers=[adapter])) is None
    assert adapter.requests == []


def test_selected_uses_the_exact_model_even_when_expensive_or_unscored() -> None:
    reg = registry(model("pricey", cost="high"), cheap("tiny"))
    runtime = paths(provider("p", "api", "pricey", "tiny", "unscored-x"))
    for name, expected in (("Pricey", "pricey"), ("unscored-x", "unscored-x")):
        settings = ClassifierConfig(mode="selected", selected_model=name)
        choice = select_classifier(settings, reg, runtime, PREFERENCE)
        assert (choice.model_id, choice.explicit, choice.deterministic) == (expected, True, False)
        assert choice.path is not None and choice.path.model_id == expected


def test_unavailable_selected_model_is_not_substituted() -> None:
    reg = registry(model("pricey", cost="high"), cheap("tiny"))
    runtime = paths(provider("p", "api", "tiny"))
    settings = ClassifierConfig(mode="selected", selected_model="pricey")
    choice = select_classifier(settings, reg, runtime, PREFERENCE)
    assert choice.deterministic and choice.model_id == "pricey"
    assert "pricey" in choice.reason and not choice.recommend_low_cost


def test_selected_model_explains_an_excluded_access_path() -> None:
    runtime = paths(provider("acme", None, "tiny"))
    settings = ClassifierConfig(mode="selected", selected_model="tiny")
    choice = select_classifier(settings, registry(), runtime, PREFERENCE)
    assert choice.deterministic
    assert "acme serves it, but does not report its access kind" in choice.reason


def test_no_classifier_is_built_for_an_unregistered_provider() -> None:
    adapter = provider("p", "api", "tiny")
    choice = ClassifierChoice("auto", ModelPath("tiny", adapter.identity()), "tiny")
    assert build_classifier(choice, AdapterRegistry()) is None


def test_disabled_never_invokes_a_provider() -> None:
    reg = registry(cheap("tiny"))
    adapter = provider("p", "api", "tiny")
    runtime = paths(adapter)
    settings = ClassifierConfig(mode="disabled", selected_model="tiny")
    choice = select_classifier(settings, reg, runtime, PREFERENCE)
    assert choice.deterministic and not choice.recommend_low_cost
    assert build_classifier(choice, AdapterRegistry(providers=[adapter])) is None
    assert adapter.requests == []


def test_classifier_respects_access_preference() -> None:
    reg = registry(cheap("tiny"))
    runtime = paths(provider("a", "api", "tiny"), provider("b", "local", "tiny"))
    choice = select_classifier(ClassifierConfig(), reg, runtime, PREFERENCE)
    assert choice.path is not None and choice.path.provider.id == "b"
    assert select_classifier(ClassifierConfig(), reg, runtime, ("subscription",)).deterministic


# -- Provider-backed classifier ----------------------------------------------------------------


def inputs() -> list[ClassificationInput]:
    return [
        ClassificationInput("t1", "implementer", "add login", paths=("src/auth.py",)),
        ClassificationInput("t2", "code_reviewer", "review cache", known_signals=("testing",)),
    ]


def test_provider_classifier_is_batch_first_and_sends_only_compact_inputs() -> None:
    vocabulary = load_policy().vocabulary
    signal = sorted(vocabulary.signals)[0]
    reply = json.dumps(
        {
            "results": [
                {"task_id": "t1", "signals": [signal, "security-skill-name"], "evidence": "auth"},
                {"task_id": "t2", "signals": []},
                {"task_id": "stranger", "signals": [signal]},
            ]
        }
    )
    adapter = provider("p", "subscription", "tiny", reply=f"```json\n{reply}\n```")
    choice = ClassifierChoice("auto", ModelPath("tiny", adapter.identity()), "tiny")
    classifier = build_classifier(choice, AdapterRegistry(providers=[adapter]))
    assert isinstance(classifier, ProviderSignalClassifier)

    result = classify(classifier, inputs(), vocabulary)

    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert request.model_id == "tiny" and request.max_output_tokens
    payload = json.loads(request.prompt)
    assert set(payload) == {"vocabulary", "tasks"}
    assert payload["vocabulary"] == sorted(vocabulary.signals)
    assert [task["task_id"] for task in payload["tasks"]] == ["t1", "t2"]
    assert set(payload["tasks"][0]) == {
        "task_id",
        "role",
        "statement",
        "spec_summary",
        "paths",
        "known_signals",
    }
    assert result.mode is ClassifierMode.USED
    assert result.signals == {"t1": (signal,), "t2": ()}
    assert result.rejected == {"t1": ("security-skill-name",)}
    assert result.identity is not None and result.identity.startswith("provider-classifier@1 ")
    assert "auto: tiny via p (subscription)" in result.identity


def test_provider_classifier_failure_falls_back_without_breaking_routing() -> None:
    adapter = provider("p", "subscription", "tiny", reply="I think t1 is about auth.")
    classifier = ProviderSignalClassifier(
        adapter, ModelPath("tiny", adapter.identity()), "selected"
    )
    result = classify(classifier, inputs(), load_policy().vocabulary)
    assert result.mode is ClassifierMode.FAILED and "not JSON" in result.error
    assert "selected:" in classifier.identity


def test_paid_provider_classifier_without_budgeted_invoker_never_calls_provider() -> None:
    adapter = provider("p", "api", "tiny")
    classifier = ProviderSignalClassifier(
        adapter, ModelPath("tiny", adapter.identity()), "selected"
    )

    result = classify(classifier, inputs(), load_policy().vocabulary)

    assert result.mode is ClassifierMode.FAILED
    assert "requires budget enforcement" in result.error
    assert adapter.requests == []


def test_oversized_batch_falls_back_without_calling_the_provider() -> None:
    adapter = provider("p", "api", "tiny")
    path = ModelPath("tiny", adapter.identity(), context_limit=300)
    result = classify(
        ProviderSignalClassifier(adapter, path, "auto"), inputs(), load_policy().vocabulary
    )
    assert result.mode is ClassifierMode.FAILED and "allows 300" in result.error
    assert adapter.requests == []


def test_provider_classifier_makes_no_call_for_an_empty_batch() -> None:
    adapter = provider("p", "api", "tiny")
    classifier = ProviderSignalClassifier(adapter, ModelPath("tiny", adapter.identity()), "auto")
    assert classifier.classify([], load_policy().vocabulary) == []
    assert adapter.requests == []


@pytest.mark.parametrize(
    "reply",
    [
        "[]",
        '{"results": {}}',
        '{"results": [{"signals": []}]}',
        '{"results": [{"task_id": "t1", "signals": "testing"}]}',
        '{"results": [{"task_id": "t1", "signals": [], "evidence": 3}]}',
    ],
)
def test_malformed_classifier_output_is_rejected(reply: str) -> None:
    with pytest.raises(ClassifierOutputError):
        parse_reply(reply, {"t1"})


def test_duplicate_classifier_results_for_one_task_are_rejected() -> None:
    reply = json.dumps(
        {
            "results": [
                {"task_id": "t1", "signals": ["a"]},
                {"task_id": "t1", "signals": ["b"]},
            ]
        }
    )
    with pytest.raises(ClassifierOutputError, match="duplicate result for task_id 't1'"):
        parse_reply(reply, {"t1"})


def test_classifier_output_is_bounded_and_normalized() -> None:
    many = [f" s{i} " for i in range(20)] + [" s0 "]
    reply = json.dumps({"results": [{"task_id": "t1", "signals": many, "evidence": "e" * 999}]})
    (result,) = parse_reply(reply, {"t1"})
    assert result.signals == tuple(f"s{i}" for i in range(8))
    assert len(result.evidence) == 200


# -- /models -----------------------------------------------------------------------------------


def write_registry(tmp_path: Path, *entries: dict[str, Any]) -> Path:
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"version": 7, "models": list(entries)}), encoding="utf-8")
    return path


def test_models_command_is_implemented() -> None:
    registry_ = build_registry()
    command = registry_.get("models")
    assert command is not None and command.planned_in is None
    output = plain(registry_.dispatch("/models", CommandContext(registry=registry_)))
    assert not output.startswith("Not implemented yet")
    assert "Model registry v" in output and "Role floors" in output


def test_models_shows_registry_floors_and_effective_classifier(tmp_path: Path) -> None:
    path = write_registry(
        tmp_path,
        cheap("tiny", latency="fast", sources=["fixture evidence prose"]),
        model("cand", status="candidate"),
    )
    adapters = AdapterRegistry(providers=[provider("acme", "local", "tiny")])
    lines = models_lines(None, adapters, path)
    output = "\n".join(lines)
    assert lines[0] == "Model registry v7: 2 models (1 active, 1 candidate)"
    tiny = next(line for line in lines if line.strip().startswith("tiny "))
    assert tiny.split() == ["tiny", "Tiny", "active", "2", *["3"] * 6, "fast", "low", "medium"]
    assert "fixture evidence prose" not in output
    for role in CANONICAL_ROLES:
        assert any(line.strip().startswith(role + " ") for line in lines)
    floor = next(line for line in lines if line.strip().startswith("security_investigator"))
    assert floor.split() == ["security_investigator", "security_reasoning", "high", "(>=", "4)"]
    assert "Skill classifier  tiny via acme (local)" in output
    assert "Mode: auto" in output and "No low-cost routing model" not in output


def test_models_lists_reachable_unscored_models(tmp_path: Path) -> None:
    path = write_registry(tmp_path, model("known", aliases=["known-2"]))
    adapters = AdapterRegistry(providers=[provider("acme", "local", "known-2", "homebrew-7b")])
    lines = models_lines(None, adapters, path)
    start = lines.index("Reachable but unscored  used only when you name them")
    assert lines[start + 1 : start + 2] == ["  homebrew-7b via acme (local)"]
    assert not any("known-2 via" in line for line in lines)
    no_unscored = models_lines(
        None, AdapterRegistry(providers=[provider("a", "api", "known")]), path
    )
    assert not any(line.startswith("Reachable but unscored") for line in no_unscored)


def test_models_recommends_a_low_cost_classifier_without_requiring_one(tmp_path: Path) -> None:
    path = write_registry(tmp_path, model("expensive", cost="high"))
    adapters = AdapterRegistry(providers=[provider("acme", "api", "expensive")])
    output = "\n".join(models_lines(None, adapters, path))
    assert "Skill classifier  deterministic only" in output
    assert "No low-cost routing model is configured." in output
    assert "explicitly select one of your existing models for classification." in output


def test_models_reports_selected_and_disabled_modes(tmp_path: Path, isolated_home: Path) -> None:
    path = write_registry(tmp_path)
    service = ConfigService(ConfigPaths(isolated_home / "config.yaml", None))
    service.set_session({"routing": {"classifier": {"mode": "selected", "selected_model": "gone"}}})
    output = "\n".join(models_lines(service, None, path))
    assert "deterministic only" in output and "selected model: gone" in output
    assert "'gone' cannot run: no ready provider serves it" in output
    assert "No low-cost routing model" not in output

    service.set_session({"routing": {"classifier": {"mode": "disabled", "selected_model": "gone"}}})
    output = "\n".join(models_lines(service, None, path))
    assert "Mode: disabled" in output and "classifier disabled in config" in output
    assert "selected model" not in output


def test_models_reports_an_empty_registry_and_invalid_registry(tmp_path: Path) -> None:
    output = "\n".join(models_lines(None, None, write_registry(tmp_path)))
    assert "0 models" in output and "No scored models yet" in output
    bad = write_registry(tmp_path, model("a", scores={"coding": 0}))
    assert models_lines(None, None, bad)[0] == "Error  Cannot load model registry."


def test_models_shows_configured_floor_overrides(tmp_path: Path, isolated_home: Path) -> None:
    service = ConfigService(ConfigPaths(isolated_home / "config.yaml", None))
    service.set_session({"routing": {"role_floors": {"implementer": "high"}}})
    lines = models_lines(service, None, write_registry(tmp_path))
    implementer = next(line for line in lines if line.strip().startswith("implementer"))
    assert implementer.split() == [
        "implementer",
        "coding",
        "high",
        "(>=",
        "4)",
        "·",
        "default",
        "low",
    ]
    assert not any("default" in line for line in lines if "orchestrator" in line)

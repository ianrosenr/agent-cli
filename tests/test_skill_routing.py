"""Skill selection, activation, budgeting, the classifier seam, persistence, and escalation."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_cli.config import ConfigPaths, ConfigService, Layer
from agent_cli.config.model import SkillActivation, SkillsConfig
from agent_cli.repository import (
    AnalysisCache,
    Anchors,
    Fact,
    FactSource,
    RepositoryFacts,
    RepositoryIndex,
    TaskInspector,
)
from agent_cli.skills import (
    CLASSIFIER_INPUT_VERSION,
    ClassifierMode,
    Discovery,
    FakeClassifier,
    Priority,
    RoutingDecision,
    RoutingError,
    RoutingRequest,
    SignalSource,
    SkillCatalog,
    SkillRouter,
    Strength,
    load_catalog,
    load_policy,
    skill_sources,
)
from agent_cli.skills.policy import default_policy_path
from agent_cli.state import TaskStateError, TaskStore
from gitrepo import commit, write

IMPLEMENTER_DEFAULTS = ("software-engineering", "testing")


@pytest.fixture
def catalog(tmp_path: Path) -> SkillCatalog:
    return load_catalog(skill_sources((), None, tmp_path / "home"))


def router(
    catalog: SkillCatalog,
    activation: dict[str, SkillActivation] | None = None,
    budget: int = 8000,
    classifier: FakeClassifier | None = None,
    store: TaskStore | None = None,
) -> SkillRouter:
    config = SkillsConfig(activation=activation or {}, context_budget_tokens=budget)
    return SkillRouter(catalog, load_policy(), config, classifier, store)


def facts(
    categories: Sequence[str] = (), signals: Sequence[str] = (), path: str = "src/app.py"
) -> RepositoryFacts:
    evidence = tuple(
        Fact(path, category, FactSource.STRUCTURAL, f"path rule for {category}")
        for category in categories
    ) + tuple(
        Fact(path, f"signal:{signal}", FactSource.STRUCTURAL, f"imports '{signal}' (line 1)")
        for signal in signals
    )
    return RepositoryFacts(tuple(categories), tuple(signals), ("python",), (), (path,), evidence)


def route_one(router: SkillRouter, request: RoutingRequest) -> RoutingDecision:
    return router.route([request])[0]


def implementer(task_id: str = "t1", **fields: object) -> RoutingRequest:
    return RoutingRequest(task_id, "implementer", "Change the code.", **fields)  # type: ignore[arg-type]


# -- Role defaults and deterministic signals --------------------------------------------------


@pytest.mark.parametrize(("role", "defaults"), list(load_policy().role_defaults.items()))
def test_each_role_gets_exactly_its_required_defaults(
    catalog: SkillCatalog, role: str, defaults: tuple[str, ...]
) -> None:
    decision = route_one(router(catalog), RoutingRequest("t1", role, "Do the work."))
    assert decision.skills == defaults
    assert all(item.priority is Priority.ROLE_DEFAULT for item in decision.selection.selected)


def test_unknown_role_is_rejected(catalog: SkillCatalog) -> None:
    with pytest.raises(RoutingError, match="unknown role 'wizard'"):
        route_one(router(catalog), RoutingRequest("t1", "wizard"))


def test_routing_without_a_classifier_uses_repository_facts(catalog: SkillCatalog) -> None:
    decision = route_one(
        router(catalog), implementer(facts=facts(["migration", "ci"], ["http_api"]))
    )
    assert decision.classifier_mode is ClassifierMode.NONE
    assert decision.classifier is None
    assert decision.skills == (
        *IMPLEMENTER_DEFAULTS,
        "interfaces",
        "data",
        "observability",
        "delivery",
    )
    assert decision.classifier_signals == ()
    migration = next(s for s in decision.signals if s.signal == "migration")
    assert migration.source is SignalSource.REPOSITORY
    assert migration.strength is Strength.STRONG
    assert migration.evidence == ("src/app.py: path rule for migration",)
    data = decision.selection.selected[3]
    assert data.priority is Priority.STRONG_EVIDENCE
    assert data.reason == "signals: migration (repository), schema (repository)"


def test_weak_repository_evidence_ranks_below_strong(catalog: SkillCatalog) -> None:
    decision = route_one(router(catalog), implementer(facts=facts(["migration"], ["concurrency"])))
    assert decision.skills == (*IMPLEMENTER_DEFAULTS, "data", "resilience")
    resilience = decision.selection.selected[-1]
    assert resilience.priority is Priority.WEAK_EVIDENCE


def test_selected_bodies_are_never_truncated(catalog: SkillCatalog) -> None:
    decision = route_one(router(catalog), implementer(facts=facts(["migration"])))
    for item in decision.selection.selected:
        assert item.skill.body == catalog[item.name].body
        assert item.skill.body == item.skill.path.read_text().split("---", 2)[2].strip() + "\n"


# -- Activation -------------------------------------------------------------------------------


def test_explicit_skills_need_an_explicit_request(catalog: SkillCatalog) -> None:
    explicit = router(catalog, {"data": "explicit"})
    automatic = route_one(explicit, implementer(facts=facts(["migration"])))
    assert "data" not in automatic.skills
    assert [(d.name, d.reason) for d in automatic.selection.dropped] == [
        ("data", "activation: explicit")
    ]

    requested = route_one(explicit, implementer(requested=("data",)))
    assert requested.skills == (*IMPLEMENTER_DEFAULTS, "data")
    assert requested.selection.selected[-1].priority is Priority.REQUESTED


def test_disabled_skills_are_unavailable(catalog: SkillCatalog) -> None:
    disabled = router(catalog, {"data": "disabled"})
    decision = route_one(disabled, implementer(facts=facts(["migration"])))
    assert "data" not in decision.skills
    assert decision.selection.dropped[0].reason == "activation: disabled"
    with pytest.raises(RoutingError, match="requested skill 'data' is disabled"):
        route_one(disabled, implementer(requested=("data",)))


def test_disabling_a_required_default_fails_clearly(catalog: SkillCatalog) -> None:
    with pytest.raises(RoutingError, match="'testing' is required for the implementer role"):
        route_one(router(catalog, {"testing": "disabled"}), implementer())


def test_always_skills_are_added_for_every_role(catalog: SkillCatalog) -> None:
    always = router(catalog, {"research": "always"})
    for role in ("implementer", "orchestrator"):
        decision = route_one(always, RoutingRequest("t1", role))
        assert "research" in decision.skills
        item = next(i for i in decision.selection.selected if i.name == "research")
        assert (item.priority, item.reason) == (Priority.ALWAYS, "activation: always")


def test_unknown_requested_skills_fail(catalog: SkillCatalog) -> None:
    with pytest.raises(RoutingError, match="requested skill 'nope'"):
        route_one(router(catalog), implementer(requested=("nope",)))


def test_unknown_activation_names_are_ignored(catalog: SkillCatalog) -> None:
    activation: dict[str, SkillActivation] = {"nope": "always", "gone": "disabled"}
    decision = route_one(router(catalog, activation), implementer())
    assert decision.skills == IMPLEMENTER_DEFAULTS


def test_routing_uses_the_layered_skill_config(paths: ConfigPaths, tmp_path: Path) -> None:
    service = ConfigService(paths)
    service.save(Layer.GLOBAL, {"skills": {"activation": {"data": "disabled"}}})
    service.save(Layer.PROJECT, {"skills": {"activation": {"research": "always"}}})
    service.set_session({"skills": {"context_budget_tokens": 5000}})

    loaded = SkillRouter.from_config(service, home=tmp_path / "home")
    assert loaded.config.activation == {"data": "disabled", "research": "always"}
    assert loaded.config.context_budget_tokens == 5000
    decision = route_one(loaded, implementer(facts=facts(["migration"])))
    assert decision.skills == (*IMPLEMENTER_DEFAULTS, "research")
    assert decision.selection.budget_tokens == 5000


def test_router_reloads_project_skills_without_restart(paths: ConfigPaths, tmp_path: Path) -> None:
    service = ConfigService(paths)
    assert paths.project_root is not None
    project_skills = paths.project_root / ".agent-cli" / "skills" / "testing"
    project_skills.mkdir(parents=True)
    (project_skills / "SKILL.md").write_text(
        "---\nname: testing\ndescription: Project testing.\nversion: 9.0.0\n---\nOurs.\n"
    )
    decision = route_one(SkillRouter.from_config(service, home=tmp_path), implementer())
    tested = decision.selection.selected[1].skill
    assert (tested.version, tested.body, tested.source.kind) == ("9.0.0", "Ours.\n", "project")


# -- Budget -----------------------------------------------------------------------------------


def required_tokens(catalog: SkillCatalog) -> int:
    return sum(catalog[name].tokens for name in IMPLEMENTER_DEFAULTS)


def test_weakest_optional_skills_are_dropped_first(catalog: SkillCatalog) -> None:
    budget = required_tokens(catalog) + catalog["data"].tokens + 10
    classifier = FakeClassifier({"t1": ["latency"]})
    decision = route_one(
        router(catalog, budget=budget, classifier=classifier),
        implementer(facts=facts(["migration"], ["concurrency"])),
    )
    assert decision.skills == (*IMPLEMENTER_DEFAULTS, "data")
    assert [d.name for d in decision.selection.dropped] == ["resilience", "observability"]
    assert all(d.reason.startswith("context budget:") for d in decision.selection.dropped)
    assert decision.selection.total_tokens <= budget


def test_equal_priority_ties_break_by_policy_order(catalog: SkillCatalog) -> None:
    budget = required_tokens(catalog) + max(catalog["data"].tokens, catalog["delivery"].tokens)
    decision = route_one(
        router(catalog, budget=budget), implementer(facts=facts(["ci", "migration"]))
    )
    assert decision.skills == (*IMPLEMENTER_DEFAULTS, "data")
    assert [d.name for d in decision.selection.dropped] == ["delivery"]


def test_required_and_requested_skills_survive_the_budget(catalog: SkillCatalog) -> None:
    budget = required_tokens(catalog) + catalog["research"].tokens
    decision = route_one(
        router(catalog, budget=budget),
        implementer(requested=("research",), facts=facts(["migration"])),
    )
    assert decision.skills == (*IMPLEMENTER_DEFAULTS, "research")
    assert [d.name for d in decision.selection.dropped] == ["data"]


def test_required_skills_over_budget_fail_before_launch(catalog: SkillCatalog) -> None:
    with pytest.raises(RoutingError, match=r"over the skills\.context_budget_tokens budget of 100"):
        route_one(router(catalog, budget=100), implementer())
    with pytest.raises(RoutingError, match="research"):
        route_one(
            router(catalog, budget=0), RoutingRequest("t1", "orchestrator", requested=("research",))
        )


def test_zero_budget_works_for_roles_without_required_skills(catalog: SkillCatalog) -> None:
    decision = route_one(
        router(catalog, budget=0), RoutingRequest("t1", "orchestrator", facts=facts(["ci"]))
    )
    assert decision.skills == ()
    assert [d.name for d in decision.selection.dropped] == ["delivery"]


# -- Classifier seam --------------------------------------------------------------------------


def test_one_classifier_call_per_batch_with_compact_inputs(catalog: SkillCatalog) -> None:
    classifier = FakeClassifier({"t1": ["authentication"], "t2": ["technical_research"]})
    decisions = router(catalog, classifier=classifier).route(
        [
            implementer("t1", facts=facts(["migration"]), spec_summary="Tokens expire."),
            RoutingRequest("t2", "research_investigator", "Compare caches."),
        ]
    )
    assert len(classifier.batches) == 1
    first, second = classifier.batches[0]
    assert (first.task_id, first.role) == ("t1", "implementer")
    assert first.spec_summary == "Tokens expire."
    assert first.paths == ("src/app.py",)
    assert first.known_signals == ("migration", "schema")
    assert second.paths == () and second.known_signals == ()
    assert [d.skills for d in decisions] == [
        (*IMPLEMENTER_DEFAULTS, "data", "security"),
        ("research",),
    ]
    assert decisions[0].classifier == "fake-classifier@1"
    assert decisions[0].classifier_mode is ClassifierMode.USED
    security = decisions[0].selection.selected[-1]
    assert security.priority is Priority.CLASSIFIER
    assert security.reason == "signals: authentication (classifier)"


def test_classifier_cannot_emit_unknown_signals_or_choose_skills(catalog: SkillCatalog) -> None:
    classifier = FakeClassifier({"t1": ["database", "security", "telepathy", "database"]})
    decision = route_one(router(catalog, classifier=classifier), implementer())
    assert decision.skills == (*IMPLEMENTER_DEFAULTS, "data")
    assert decision.rejected_signals == ("security", "telepathy")
    assert [s.signal for s in decision.classifier_signals] == ["database"]


def test_classifier_results_for_unknown_tasks_are_ignored(catalog: SkillCatalog) -> None:
    classifier = FakeClassifier({"other": ["database"]})
    decision = route_one(router(catalog, classifier=classifier), implementer())
    assert decision.skills == IMPLEMENTER_DEFAULTS


def test_classifier_failure_falls_back_to_deterministic_routing(catalog: SkillCatalog) -> None:
    classifier = FakeClassifier(fail_with=lambda: TimeoutError("too slow"))
    decision = route_one(
        router(catalog, classifier=classifier), implementer(facts=facts(["migration"]))
    )
    assert decision.classifier_mode is ClassifierMode.FAILED
    assert decision.skills == (*IMPLEMENTER_DEFAULTS, "data")


def test_a_task_may_appear_only_once_per_batch(catalog: SkillCatalog) -> None:
    with pytest.raises(RoutingError, match="only once"):
        router(catalog).route([implementer("t1"), implementer("t1")])


# -- Repository integration -------------------------------------------------------------------


def test_routing_consumes_inspection_facts_without_rescanning(
    catalog: SkillCatalog, repo: Path, tmp_path: Path
) -> None:
    write(repo, "migrations/0001_sessions.sql", "CREATE TABLE sessions (id TEXT);\n")
    write(repo, "src/app/store.py", "import sqlalchemy\n")
    commit(repo)
    index = RepositoryIndex.open(repo, cache=AnalysisCache(tmp_path / "cache.sqlite"))
    inspected = TaskInspector(index).inspect(Anchors(paths=("migrations", "src/app/store.py")))
    index.close()
    for path in ("migrations/0001_sessions.sql", "src/app/store.py"):
        (repo / path).unlink()  # routing must work from the facts alone

    decision = route_one(router(catalog), implementer(facts=inspected.facts()))
    assert "data" in decision.skills
    by_signal = {s.signal: s for s in decision.deterministic_signals}
    assert by_signal["migration"].strength is Strength.STRONG
    assert by_signal["database"].strength is Strength.WEAK
    assert by_signal["database"].evidence == ("src/app/store.py: imports 'sqlalchemy' (line 1)",)


# -- Persistence and escalation ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "state" / "tasks.sqlite")


def tasks(store: TaskStore) -> tuple[str, str]:
    session = store.start_session("refresh tokens")
    one = store.create_task(session.id, "Add refresh endpoint", role="implementer")
    two = store.create_task(session.id, "Research token rotation", role="research_investigator")
    return one.id, two.id


def test_routing_decisions_survive_restart(catalog: SkillCatalog, store: TaskStore) -> None:
    one, _ = tasks(store)
    budget = required_tokens(catalog) + catalog["data"].tokens
    classifier = FakeClassifier({one: ["latency", "nope"]})
    decision = route_one(
        router(catalog, budget=budget, classifier=classifier, store=store),
        implementer(one, facts=facts(["migration"])),
    )
    store.close()

    record = TaskStore(store.path).latest_skill_routing(one)
    assert record is not None and record.id == decision.record_id
    assert (record.kind, record.role, record.policy_version) == ("initial", "implementer", "3")
    assert (record.classifier, record.classifier_mode) == ("fake-classifier@1", "used")
    assert record.classifier_input_version == CLASSIFIER_INPUT_VERSION
    assert record.estimator == "utf8-bytes-div3@1"
    assert (record.budget_tokens, record.total_tokens) == (budget, decision.selection.total_tokens)
    assert [(s.name, s.version, s.source) for s in record.selected] == [
        ("software-engineering", "2.0.0", "builtin"),
        ("testing", "1.0.0", "builtin"),
        ("data", "1.0.0", "builtin"),
    ]
    expected_source = str(decision.selection.selected[0].skill.source.root.resolve())
    assert {s.source_path for s in record.selected} == {expected_source}
    assert record.selected[0].reason == "role default for implementer"
    assert [(d.name, d.reason.split(":")[0]) for d in record.dropped] == [
        ("observability", "context budget")
    ]
    sources = {(s.signal, s.source, s.strength) for s in record.signals}
    assert ("migration", "repository", "strong") in sources
    assert ("latency", "classifier", None) in sources
    assert record.rejected_signals == ("nope",)
    assert record.reason == "" and record.previous_id is None


def test_routing_cannot_change_a_tasks_role(catalog: SkillCatalog, store: TaskStore) -> None:
    one, _ = tasks(store)
    with pytest.raises(TaskStateError, match="has role implementer"):
        route_one(router(catalog, store=store), RoutingRequest(one, "architect"))


def test_escalation_reroutes_only_the_affected_task(
    catalog: SkillCatalog, store: TaskStore
) -> None:
    one, two = tasks(store)
    classifier = FakeClassifier()
    routing = router(catalog, classifier=classifier, store=store)
    first, _ = routing.route(
        [implementer(one, facts=facts(["migration"])), RoutingRequest(two, "research_investigator")]
    )
    policy_before = default_policy_path().read_bytes()

    escalated = routing.escalate(
        first,
        implementer(one, facts=facts(["migration"])),
        Discovery(
            "The endpoint also validates bearer tokens (src/auth.py:12).",
            facts=facts(signals=["http_api"], path="src/auth.py"),
            signals=("authentication",),
        ),
    )

    assert escalated.role == "implementer"
    assert escalated.added == ("interfaces", "security", "observability")
    assert escalated.skills[:2] == IMPLEMENTER_DEFAULTS and "data" in escalated.skills
    assert [batch[0].task_id for batch in classifier.batches] == [one, one]
    assert len(classifier.batches[1]) == 1
    assert "New finding: The endpoint also validates" in classifier.batches[1][0].statement
    assert classifier.batches[1][0].paths == ("src/app.py", "src/auth.py")
    escalation = next(s for s in escalated.signals if s.source is SignalSource.ESCALATION)
    assert escalation.signal == "authentication"

    history = store.skill_routings(one)
    assert [r.kind for r in history] == ["initial", "escalation"]
    assert history[1].previous_id == history[0].id
    assert history[1].reason.startswith("The endpoint also validates")
    assert len(store.skill_routings(two)) == 1
    assert default_policy_path().read_bytes() == policy_before
    assert store.task(one).role == "implementer"  # type: ignore[union-attr]


def test_escalation_cannot_change_role_or_use_unknown_signals(catalog: SkillCatalog) -> None:
    routing = router(catalog)
    first = route_one(routing, implementer())
    with pytest.raises(RoutingError, match="role authority"):
        routing.escalate(first, RoutingRequest("t1", "architect"), Discovery("why"))
    with pytest.raises(RoutingError, match="unknown signal"):
        routing.escalate(first, implementer(), Discovery("why", signals=("telepathy",)))


def test_escalation_respects_the_policy_switch(catalog: SkillCatalog) -> None:
    routing = router(catalog)
    first = route_one(routing, implementer())
    routing.policy = dataclasses.replace(routing.policy, dynamic_escalation=False)
    with pytest.raises(RoutingError, match="disabled by the routing policy"):
        routing.escalate(first, implementer(), Discovery("why", signals=("database",)))


def test_a_task_keeps_the_role_it_was_first_routed_as(
    catalog: SkillCatalog, store: TaskStore
) -> None:
    session = store.start_session()
    unassigned = store.create_task(session.id, "No role stored")
    routing = router(catalog, store=store)
    route_one(routing, RoutingRequest(unassigned.id, "architect"))
    with pytest.raises(TaskStateError, match="has role architect; cannot route it as implementer"):
        route_one(routing, implementer(unassigned.id))


def test_escalation_links_are_enforced(catalog: SkillCatalog, store: TaskStore) -> None:
    one, two = tasks(store)
    unsaved = route_one(router(catalog), implementer(one))
    with pytest.raises(RoutingError, match="never recorded"):
        router(catalog, store=store).escalate(unsaved, implementer(one), Discovery("why"))

    record = route_one(router(catalog, store=store), implementer(one)).record()
    with pytest.raises(TaskStateError, match="must link to the decision it extends"):
        store.record_skill_routing(dataclasses.replace(record, kind="escalation"))
    first = store.skill_routings(one)[0].id
    with pytest.raises(TaskStateError, match="must link to the decision it extends"):
        store.record_skill_routing(dataclasses.replace(record, previous_id=first))
    with pytest.raises(TaskStateError, match="is not a decision for"):
        store.record_skill_routing(
            dataclasses.replace(
                record,
                task_id=two,
                role="research_investigator",
                kind="escalation",
                previous_id=first,
            )
        )


def test_a_batch_is_recorded_all_or_nothing(catalog: SkillCatalog, store: TaskStore) -> None:
    one, two = tasks(store)
    with pytest.raises(TaskStateError, match="cannot route it as implementer"):
        router(catalog, store=store).route([implementer(one), implementer(two)])
    assert store.skill_routings(one) == []
    assert store.skill_routings(two) == []


def test_escalation_derives_signals_from_updated_request_facts(catalog: SkillCatalog) -> None:
    routing = router(catalog)
    first = route_one(routing, implementer())
    escalated = routing.escalate(
        first, implementer(facts=facts(["migration"])), Discovery("A migration is needed.")
    )
    assert escalated.added == ("data",)


def test_latest_routings_are_read_per_session(catalog: SkillCatalog, store: TaskStore) -> None:
    one, two = tasks(store)
    routing = router(catalog, store=store)
    first, _ = routing.route([implementer(one), RoutingRequest(two, "research_investigator")])
    escalated = routing.escalate(first, implementer(one), Discovery("why", signals=("schema",)))
    latest = store.latest_skill_routings(store.latest_session().id)  # type: ignore[union-attr]
    assert list(latest) == [one, two]
    assert latest[one].id == escalated.record_id
    assert store.latest_skill_routing(one) == latest[one]

"""The orchestration service: ordinary root-prompt text in, persisted planning state out.

For each request it coordinates, in order: the session and root task, V1-05 inspection, the
planner, whole-proposal validation, the atomic plan write, V1-06 skill routing, and pending
specs. Then it stops. It never edits repository files, launches a harness, creates a worktree,
or approves a spec; V1-10 and V1-11 take over from the persisted plan.

A pending planning question captures the next ordinary input as its answer, so a restart can
never skip it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from agent_cli.adapters import AdapterRegistry
from agent_cli.budget import NO_PRICING, BudgetGuard, CostEstimator, SpendLedger
from agent_cli.config import Config, ConfigError, ConfigService
from agent_cli.models import (
    ModelRegistry,
    RegistryError,
    build_classifier,
    load_registry,
    runtime_paths,
    select_classifier,
)
from agent_cli.orchestration.calls import DirectModelCall, ModelReply, PlanningUnavailable
from agent_cli.orchestration.domain import (
    PlanningInput,
    PlanRejected,
    PriorDecision,
    RepositoryContext,
    UserText,
    ValidatedPlan,
    WorkItem,
    validate_proposal,
)
from agent_cli.orchestration.inspection import MAX_FILES, goal_anchors, repository_context
from agent_cli.orchestration.output import ModelOutputError
from agent_cli.orchestration.planner import Planner, ProviderPlanner
from agent_cli.orchestration.render import render_plan, render_question, render_stopped
from agent_cli.orchestration.spec_author import (
    ProviderSpecAuthor,
    SpecAuthor,
    SpecContext,
    SpecRequest,
    draft_problems,
    known_area,
    match_drafts,
    render_spec,
)
from agent_cli.repository import RepositoryError, RepositoryFacts, RepositoryIndex, TaskInspector
from agent_cli.skills import RoutingError, RoutingRequest, SignalClassifier, SkillError, SkillRouter
from agent_cli.state import PinKind, Task, TaskStateError, TaskStore

type ClassifierFactory = Callable[[Config, BudgetGuard], SignalClassifier | None]

PLANNING_ATTEMPTS = 2
"""A proposal may pin the planning model; if the call that made it broke the pin, planning is
redone once under it."""
MAX_QUESTIONS = 3
"""Questions one request may ask before planning stops and asks for a restated request."""


@dataclass(slots=True)
class OrchestrationService:
    store: TaskStore
    config: ConfigService
    adapters: AdapterRegistry = field(default_factory=AdapterRegistry)
    planner: Planner = field(default_factory=ProviderPlanner)
    spec_author: SpecAuthor = field(default_factory=ProviderSpecAuthor)
    registry: ModelRegistry | None = None
    """The model registry; the built-in one when ``None``."""
    ledger: SpendLedger | None = None
    """The paid-spend ledger; the global one when ``None``."""
    estimator: CostEstimator = NO_PRICING
    classifier: ClassifierFactory | None = None
    """Builds the skill classifier; the configured one (V1-07) when ``None``."""
    home: Path | None = None
    _index: RepositoryIndex | None = None

    def handle(self, text: str) -> str:
        """Plan ``text`` as a new request, or take it as the answer to a pending question."""
        try:
            session = self.store.latest_session() if self.store.exists() else None
            if session is None or not session.open:
                session = self.store.start_session()
            pending = self.store.pending_planning_question(session.id)
            if pending is None:
                root, _ = self.store.start_root_task(session.id, text)
            else:
                self.store.answer_planning_question(pending.id, text)
                root = self.store.task(pending.task_id) or missing(pending.task_id)
            return self.plan(root)
        except TaskStateError as error:
            return f"Cannot use task state: {error}"

    def plan(self, root: Task) -> str:
        try:
            config = self.config.resolve()
            registry = self.registry if self.registry is not None else load_registry()
        except (ConfigError, RegistryError) as error:
            return self.stop(root, str(error))
        guard = BudgetGuard.from_config(config, self.ledger, self.estimator, self.store)
        answers = [d.decision for d in self.store.task_decisions(root.id) if d.decided_by == "user"]
        repository, facts = self.inspect(" ".join([root.goal, *answers]))

        replies: list[ModelReply] = []
        for attempt in range(PLANNING_ATTEMPTS):
            pins = {kind: pin.value for kind, pin in self.store.pins(root.id).items()}
            planning = self.planning_input(root, repository, pins)
            call = DirectModelCall(self.adapters, config, registry, guard, root.id, pins, replies)
            made = len(replies)
            try:
                plan = validate_proposal(self.planner.propose(planning, call), planning)
            except (PlanningUnavailable, ModelOutputError, PlanRejected) as error:
                return self.stop(root, f"{failure(error)}: {error}")
            for pin in plan.planning_pins:
                if pins.get(pin.kind) != pin.value:
                    self.store.pin_by_user(root.id, pin.kind, pin.value, pin.evidence)
            pins = {kind: pin.value for kind, pin in self.store.pins(root.id).items()}
            if honored(replies[made:], pins, registry):
                break
            if attempt + 1 == PLANNING_ATTEMPTS:
                return self.stop(root, "planning could not be made under the pinned model")

        if plan.question is not None:
            if len(self.store.planning_questions(root.id)) >= MAX_QUESTIONS:
                return self.stop(
                    root,
                    f"{MAX_QUESTIONS} questions were not enough to settle the plan; "
                    "restate the request with the decisions it needs",
                )
            asked = self.store.ask_planning_question(
                root.id, plan.question.question, plan.question.why
            )
            return render_question(asked, plan.proposal.summary)
        try:
            ids = self.store.persist_plan(plan.write(root.id))
        except TaskStateError as error:
            return self.stop(root, f"the plan could not be saved: {error}")

        items = {ids[item.id]: item for item in plan.proposal.work_items}
        problems = self.route_skills(config, guard, registry, items, facts)
        problems += self.write_specs(call, items, planning, plan, repository)
        if problems:
            self.block([root.id], "planning is incomplete: " + "; ".join(problems))
        return render_plan(
            self.store,
            root.id,
            plan.proposal.summary,
            plan.unpinned,
            problems,
            [reply.describe() for reply in replies],
        )

    # -- Phases --------------------------------------------------------------------------------

    def inspect(self, goal: str) -> tuple[RepositoryContext, RepositoryFacts | None]:
        """Refresh the V1-05 index and inspect around the goal, before any question is asked."""
        root = self.config.paths.project_root
        if root is None:
            return RepositoryContext(problem="not inside a Git repository"), None
        try:
            if self._index is None:
                self._index = RepositoryIndex.open(root)
            inspection = TaskInspector(self._index).inspect(goal_anchors(goal), MAX_FILES)
        except RepositoryError as error:
            return RepositoryContext(problem=f"repository inspection failed: {error}"), None
        return repository_context(inspection), inspection.facts()

    def planning_input(
        self, root: Task, repository: RepositoryContext, pins: Mapping[PinKind, str]
    ) -> PlanningInput:
        requirements = [r for r in self.store.task_requirements(root.id) if r.source == "user"]
        decisions = self.store.task_decisions(root.id)
        return PlanningInput(
            goal=root.goal,
            requirements=tuple(UserText(r.id, r.text) for r in requirements),
            decisions=tuple(PriorDecision(d.question, d.decision, d.decided_by) for d in decisions),
            answers=tuple(UserText(d.id, d.decision) for d in decisions if d.decided_by == "user"),
            repository=repository,
            constraints=tuple(f"{kind}={value}" for kind, value in pins.items()),
        )

    def route_skills(
        self,
        config: Config,
        guard: BudgetGuard,
        registry: ModelRegistry,
        items: Mapping[str, WorkItem],
        facts: RepositoryFacts | None,
    ) -> list[str]:
        """Route every child's skills in one V1-06 batch, after roles are fixed."""
        try:
            if self.classifier is not None:
                classifier = self.classifier(config, guard)
            else:
                classifier = self.configured_classifier(config, guard, registry)
            router = SkillRouter.from_config(self.config, classifier, self.store, self.home)
            router.route(
                [
                    RoutingRequest(task_id, item.role, item.goal, facts=self.facts_for(item, facts))
                    for task_id, item in items.items()
                ]
            )
        except (RoutingError, SkillError, ConfigError, TaskStateError) as error:
            reason = f"skill routing failed: {error}"
            self.block(items, reason)
            return [reason]
        return []

    def configured_classifier(
        self, config: Config, guard: BudgetGuard, registry: ModelRegistry
    ) -> SignalClassifier | None:
        runtime = runtime_paths(self.adapters, config)
        choice = select_classifier(
            config.routing.classifier, registry, runtime, config.access.preference
        )
        return build_classifier(choice, self.adapters, guard.invoke_classifier)

    def facts_for(self, item: WorkItem, root: RepositoryFacts | None) -> RepositoryFacts | None:
        """Facts for the child's own areas when it names any, else the root inspection's."""
        anchors = goal_anchors(" ".join(item.areas))
        if self._index is None or not (anchors.paths or anchors.symbols):
            return root
        try:
            return TaskInspector(self._index).inspect(anchors, MAX_FILES, refresh=False).facts()
        except RepositoryError:
            return root

    def write_specs(
        self,
        call: DirectModelCall,
        items: Mapping[str, WorkItem],
        planning: PlanningInput,
        plan: ValidatedPlan,
        repository: RepositoryContext,
    ) -> list[str]:
        """One spec-author batch for the root task's non-trivial implementer children."""
        targets = {task_id: item for task_id, item in items.items() if item.needs_spec}
        if not targets:
            return []
        goals = {item.id: item.goal for item in items.values()}
        requests = [
            SpecRequest(
                task_id,
                item.goal,
                item.why,
                item.areas,
                tuple(goals[key] for key in item.depends_on),
            )
            for task_id, item in targets.items()
        ]
        context = SpecContext(
            planning.requirements,
            planning.answers,
            planning.decisions,
            plan.proposal.assumptions,
            repository,
        )
        try:
            drafts = match_drafts(requests, self.spec_author.draft(requests, context, call))
        except (PlanningUnavailable, ModelOutputError) as error:
            reason = f"spec authoring failed: {error}"
            self.block(targets, reason)
            return [reason]
        problems = []
        for task_id, item in targets.items():
            draft = drafts.get(task_id)
            issues = (
                ["no spec was written"]
                if draft is None
                else draft_problems(draft, lambda area: self.known(area, repository.paths))
            )
            if issues:
                reason = f"spec for {item.goal!r} rejected: {'; '.join(issues)}"
                self.block([task_id], reason)
                problems.append(reason)
            else:
                assert draft is not None
                self.store.add_spec_version(task_id, render_spec(draft))
        return problems

    def known(self, area: str, inspected: Sequence[str]) -> bool:
        """Whether the repository supports ``area``: the index when open, else the inspection."""
        index = self._index

        def file(path: str) -> bool:
            return index.file(path) is not None if index else path in inspected

        def directory(path: str) -> bool:
            if index:
                return bool(index.files(path))
            return any(known.startswith(path + "/") for known in inspected)

        try:
            return known_area(area, file, directory)
        except RepositoryError:
            return False

    # -- Blocking ------------------------------------------------------------------------------

    def stop(self, root: Task, reason: str) -> str:
        """Keep the root task and its requirement; record why planning stopped."""
        self.block([root.id], reason)
        return render_stopped(reason)

    def block(self, task_ids: Iterable[str], reason: str) -> None:
        for task_id in task_ids:
            with suppress(TaskStateError):  # the reason is still shown to the user
                self.store.block_task(task_id, reason)


def honored(
    replies: Sequence[ModelReply], pins: Mapping[PinKind, str], registry: ModelRegistry
) -> bool:
    """Whether every call in ``replies`` ran on the pinned planning model and provider."""
    model, provider = pins.get("model"), pins.get("provider")
    entry = None if model is None else registry.get(model)
    names = {n.casefold() for n in (entry.names if entry else (model,) if model else ())}
    return all(
        (model is None or reply.model_id.casefold() in names)
        and (provider is None or reply.provider_id == provider)
        for reply in replies
    )


def failure(error: Exception) -> str:
    if isinstance(error, PlanningUnavailable):
        return "no planning model can run"
    if isinstance(error, ModelOutputError):
        return "the planner's reply was malformed"
    return "the proposed plan was invalid"


def missing(task_id: str) -> Task:
    raise TaskStateError(f"unknown task: {task_id}")

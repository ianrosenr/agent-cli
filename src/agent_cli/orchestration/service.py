"""Supervisor-side orchestration, preparation, authorization, and worker handoff.

Planning is conditional. Prepared scouts may launch with diagnostic authority. Prepared ships pass
through the V1-10 authorization boundary before any implementation-authority worker can launch.
Pending planning questions and checkpoint authorization gates both survive restart.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import RenderableType

from agent_cli.adapters import DIAGNOSTIC_AUTHORITY, IMPLEMENTATION_AUTHORITY, AdapterRegistry
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
from agent_cli.orchestration.auth import HarnessAuthCoordinator, LoginHarnesses
from agent_cli.orchestration.authorization import (
    authorization_answer,
    authorization_scope,
    decide_authorization,
    render_authorization_request,
    render_authorization_result,
)
from agent_cli.orchestration.calls import DirectModelCall, ModelReply, PlanningUnavailable
from agent_cli.orchestration.domain import (
    PlanningInput,
    PlanProposal,
    PlanRejected,
    PriorDecision,
    RepositoryContext,
    TaskBrief,
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
from agent_cli.state import Approval, PinKind, Task, TaskAuthorization, TaskStateError, TaskStatus, TaskStore
from agent_cli.terms import TaskComplexity
from agent_cli.ui import error, info
from agent_cli.workers import WorkerExecutor, WorkerLaunchError

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
    login_harnesses: LoginHarnesses | None = None
    """Interactive native-login callback supplied by the shell; absent in noninteractive uses."""
    worker_notify: Callable[[str], None] | None = None
    """Optional shell-safe notification sink for background worker completion."""
    _index: RepositoryIndex | None = None

    def handle(self, text: str, handoff: TaskBrief | None = None) -> RenderableType:
        """Plan ``text`` as a new request, or take it as the answer to a pending question.

        A ``handoff`` carries the request as the root-prompt router resolved it: its goal
        becomes the root goal, ``text`` stays the user requirement, and the conversation it was
        resolved from is kept as derived context.
        """
        try:
            session = self.store.latest_session() if self.store.exists() else None
            if session is None or not session.open:
                session = self.store.start_session()
            authorization = self.store.pending_authorization(session.id)
            if authorization is not None:
                return self.answer_authorization(authorization, text)

            pending = self.store.pending_planning_question(session.id)
            if pending is None and handoff is not None and handoff.goal.strip():
                context = (handoff.context,) if handoff.context.strip() else ()
                root, _ = self.store.start_root_task(
                    session.id,
                    handoff.goal.strip(),
                    request=text,
                    context=context,
                    kind=handoff.kind,
                    complexity=handoff.complexity,
                )
                if handoff.complexity == "trivial":
                    return self.prepare_single_task(root, handoff)
            elif pending is None:
                root, _ = self.store.start_root_task(session.id, text)
            else:
                self.store.answer_planning_question(pending.id, text)
                root = self.store.task(pending.task_id) or missing(pending.task_id)
            return self.plan(root)
        except TaskStateError as problem:
            return error("Cannot use task state.", str(problem))

    def prepare_single_task(self, root: Task, handoff: TaskBrief) -> RenderableType:
        """Persist one bounded worker task without paying for a decomposition model call."""
        try:
            config = self.config.resolve()
            registry = self.registry if self.registry is not None else load_registry()
        except (ConfigError, RegistryError) as error:
            return self.stop(root, str(error))

        guard = BudgetGuard.from_config(config, self.ledger, self.estimator, self.store)
        repository, facts = self.inspect(root.goal)
        pins = {kind: pin.value for kind, pin in self.store.pins(root.id).items()}
        planning = self.planning_input(root, repository, pins)
        if not planning.requirements:
            return self.stop(root, "the request has no authoritative user requirement")

        role = "research_investigator" if handoff.kind == "scout" else "implementer"
        item = WorkItem(
            id="work",
            goal=handoff.goal,
            role=role,
            depends_on=(),
            changes_code=handoff.kind == "ship",
            why="The request is bounded and does not need decomposition.",
            requirements=(planning.requirements[0].id,),
            trivial=True,
        )
        try:
            plan = validate_proposal(
                PlanProposal(
                    summary=f"Single {handoff.kind} task; no decomposition needed.",
                    work_items=(item,),
                ),
                planning,
            )
            if handoff.kind == "ship":
                auth = HarnessAuthCoordinator(self.adapters, config, self.login_harnesses)
                auth.ensure_any_ready()
            ids = self.store.persist_plan(plan.write(root.id))
        except (PlanRejected, TaskStateError) as error:
            return self.stop(root, f"the direct task could not be saved: {error}")

        items = {ids[item.id]: item}
        problems = self.route_skills(config, guard, registry, items, facts)
        if problems:
            self.block([root.id], "task preparation is incomplete: " + "; ".join(problems))

        if not problems:
            child = self.store.task(ids[item.id])
            project_root = self.config.paths.project_root
            if child is not None and project_root is not None:
                if handoff.kind == "scout":
                    try:
                        started = WorkerExecutor(
                            self.store,
                            self.adapters,
                            registry,
                            config,
                            project_root,
                            notify=self.worker_notify,
                        ).launch(
                            child,
                            "scout",
                            handoff.complexity,
                            child.goal,
                            DIAGNOSTIC_AUTHORITY,
                        )
                        return info("Scout started.", started)
                    except WorkerLaunchError as problem:
                        return render_plan(
                            self.store,
                            root.id,
                            plan.proposal.summary,
                            plan.unpinned,
                            (f"Scout prepared but could not start: {problem}",),
                            (),
                            config.ui.verbosity,
                            ready="Scout prepared but not started.",
                        )
                return self.authorize_ship(child, config, registry, handoff.complexity)

        return render_plan(
            self.store,
            root.id,
            plan.proposal.summary,
            plan.unpinned,
            problems,
            (),
            config.ui.verbosity,
            ready="Ready for worker execution. Nothing has been run yet.",
        )

    def authorize_ship(
        self,
        task: Task,
        config: Config,
        registry: ModelRegistry,
        complexity: TaskComplexity,
    ) -> RenderableType:
        """Grant, checkpoint, or block implementation authority for one prepared ship."""
        spec = self.store.latest_spec(task.id)
        decision = decide_authorization(config, complexity, spec)
        version = None if spec is None else spec.version
        if decision.action == "block":
            authorization = self.store.add_authorization(
                task.id,
                config.implementation.mode,
                Approval.REJECTED,
                decision.reason,
                spec_version=version,
                decided_by=f"policy:{config.implementation.mode}",
            )
            self.store.set_status(task.id, TaskStatus.BLOCKED, decision.reason)
            return render_authorization_result(authorization, decision.reason)

        if decision.action == "checkpoint":
            authorization = self.store.request_authorization(
                task.id,
                config.implementation.mode,
                decision.reason,
                spec_version=version,
            )
            return render_authorization_request(authorization, task, spec)

        authorization = self.store.add_authorization(
            task.id,
            config.implementation.mode,
            Approval.APPROVED,
            decision.reason,
            spec_version=version,
            decided_by="policy:autopilot",
        )
        return self.launch_authorized_ship(task, config, registry, complexity, authorization)

    def answer_authorization(
        self,
        authorization: TaskAuthorization,
        text: str,
    ) -> RenderableType:
        """Resolve a persisted checkpoint gate from the user's next conversational turn."""
        task = self.store.task(authorization.task_id)
        if task is None:
            raise TaskStateError(f"unknown authorization task: {authorization.task_id}")
        spec = self.store.latest_spec(task.id)
        answer = authorization_answer(text)
        if answer == "unclear":
            return render_authorization_request(authorization, task, spec)
        if answer == "reject":
            rejected = self.store.decide_authorization(
                authorization.id,
                Approval.REJECTED,
                "user",
                "user rejected implementation authority",
            )
            self.settle_canceled_parent(task)
            return render_authorization_result(
                rejected,
                "No source-changing worker was launched.",
            )

        approved = self.store.decide_authorization(
            authorization.id,
            Approval.APPROVED,
            "user",
            "user approved bounded implementation authority",
        )
        try:
            config = self.config.resolve()
            registry = self.registry if self.registry is not None else load_registry()
        except (ConfigError, RegistryError) as problem:
            return error("Authorized, but cannot launch implementation.", str(problem))
        root = self.store.task(self.store.root_task(task.id))
        complexity = "moderate" if root is None or root.complexity is None else root.complexity
        return self.launch_authorized_ship(task, config, registry, complexity, approved)

    def launch_authorized_ship(
        self,
        task: Task,
        config: Config,
        registry: ModelRegistry,
        complexity: TaskComplexity,
        authorization: TaskAuthorization,
    ) -> RenderableType:
        """Launch only after canonical state records current implementation authorization."""
        dependencies = [self.store.task(task_id) for task_id in self.store.dependencies(task.id)]
        if any(dependency is None or dependency.status is not TaskStatus.COMPLETED for dependency in dependencies):
            return render_authorization_result(
                authorization,
                "Implementation is authorized and waiting for its dependencies.",
            )
        project_root = self.config.paths.project_root
        if project_root is None:
            return error("Authorized, but cannot launch implementation.", "No repository is open.")
        spec = self.store.latest_spec(task.id)
        scope = authorization_scope(task, spec)
        try:
            started = WorkerExecutor(
                self.store,
                self.adapters,
                registry,
                config,
                project_root,
                notify=self.worker_notify,
            ).launch(task, "ship", complexity, scope, IMPLEMENTATION_AUTHORITY)
        except WorkerLaunchError as problem:
            return error("Implementation authorized but could not start.", str(problem))
        return info("Implementation started.", started)

    def settle_canceled_parent(self, task: Task) -> None:
        if task.parent_id is None:
            return
        children = self.store.subtasks(task.parent_id)
        if children and all(child.status.finished for child in children):
            self.store.set_status(task.parent_id, TaskStatus.CANCELED)

    def plan(self, root: Task) -> RenderableType:
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

        if plan.proposal.work_items:
            auth = HarnessAuthCoordinator(self.adapters, config, self.login_harnesses)
            harness_pins = tuple(pin.value for pin in plan.delegated_pins if pin.kind == "harness")
            if harness_pins:
                ready, reason = auth.repair_pinned(harness_pins)
                if not ready:
                    return self.stop(root, reason)
            else:
                auth.ensure_any_ready()

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
            config.ui.verbosity,
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
        current = self.store.task_requirements(root.id)
        requirements = [r for r in current if r.source == "user"]
        decisions = self.store.task_decisions(root.id)
        return PlanningInput(
            goal=root.goal,
            requirements=tuple(UserText(r.id, r.text) for r in requirements),
            decisions=tuple(PriorDecision(d.question, d.decision, d.decided_by) for d in decisions),
            answers=tuple(UserText(d.id, d.decision) for d in decisions if d.decided_by == "user"),
            repository=repository,
            constraints=tuple(f"{kind}={value}" for kind, value in pins.items()),
            context=tuple(r.text for r in current if r.source == "derived"),
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

    def stop(self, root: Task, reason: str) -> RenderableType:
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

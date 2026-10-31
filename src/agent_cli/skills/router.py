"""Routing skills for orchestrated tasks, recording each decision in canonical task state.

``SkillRouter.route`` routes one orchestration cycle's tasks with at most one batched classifier
call. ``SkillRouter.escalate`` reroutes a single task after a newly discovered fact. Neither
launches workers, assembles prompts, nor changes a task's role or scope: later orchestration
decides how newly selected skills reach a running worker.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from agent_cli.config import ConfigService
from agent_cli.config.model import SkillsConfig
from agent_cli.repository import RepositoryFacts
from agent_cli.skills.classifier import (
    CLASSIFIER_INPUT_VERSION,
    Classification,
    ClassificationInput,
    ClassifierMode,
    SignalClassifier,
    classify,
)
from agent_cli.skills.discovery import SkillCatalog, load_catalog, skill_sources
from agent_cli.skills.models import RoutingError, Skill
from agent_cli.skills.policy import RoutingPolicy, Strength, load_policy
from agent_cli.skills.selection import SkillSelection, select_skills
from agent_cli.skills.signals import (
    DetectedSignal,
    SignalSource,
    repository_signals,
    union,
)
from agent_cli.skills.tokens import DEFAULT_ESTIMATOR, TokenEstimator
from agent_cli.state import RoutedSignal, RoutedSkill, RoutingKind, SkillRouting, TaskStore


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    """One task to route. The orchestrator owns the task, its role, and its scope."""

    task_id: str
    role: str
    statement: str = ""
    spec_summary: str = ""
    facts: RepositoryFacts | None = None
    """V1-05 facts for the task's affected repository surfaces."""
    requested: tuple[str, ...] = ()
    """Skills the user explicitly asked for (the only way to select ``explicit`` skills)."""


@dataclass(frozen=True, slots=True)
class Discovery:
    """A newly discovered fact that may cross an uncovered policy domain."""

    reason: str
    """The fact, in ordinary language, with its evidence."""
    facts: RepositoryFacts | None = None
    signals: tuple[str, ...] = ()
    """Normalized signals the orchestrator could derive directly from the report."""


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    task_id: str
    role: str
    signals: tuple[DetectedSignal, ...]
    selection: SkillSelection
    policy_version: str
    estimator: str
    classifier: str | None
    classifier_mode: ClassifierMode
    rejected_signals: tuple[str, ...] = ()
    kind: RoutingKind = "initial"
    reason: str = ""
    record_id: str | None = None
    """The persisted ``SkillRouting`` ID, when a task store recorded this decision."""
    added: tuple[str, ...] = ()
    """For an escalation: skills selected now that the previous decision did not select."""

    @property
    def skills(self) -> tuple[str, ...]:
        """Selected skill names in injection order."""
        return tuple(item.name for item in self.selection.selected)

    @property
    def deterministic_signals(self) -> tuple[DetectedSignal, ...]:
        return tuple(s for s in self.signals if s.source is not SignalSource.CLASSIFIER)

    @property
    def classifier_signals(self) -> tuple[DetectedSignal, ...]:
        return tuple(s for s in self.signals if s.source is SignalSource.CLASSIFIER)

    def record(self, previous_id: str | None = None) -> SkillRouting:
        """The canonical task-state record for this decision (ID and time set by the store)."""

        def routed(skill: Skill, reason: str) -> RoutedSkill:
            return RoutedSkill(
                skill.name,
                skill.version,
                skill.source.kind,
                str(skill.source.root.resolve()),
                skill.tokens,
                reason,
            )

        return SkillRouting(
            id="",
            task_id=self.task_id,
            kind=self.kind,
            role=self.role,
            policy_version=self.policy_version,
            classifier=self.classifier,
            classifier_mode=self.classifier_mode,
            classifier_input_version=CLASSIFIER_INPUT_VERSION,
            estimator=self.estimator,
            budget_tokens=self.selection.budget_tokens,
            total_tokens=self.selection.total_tokens,
            signals=tuple(
                RoutedSignal(
                    s.signal,
                    s.source,
                    None if s.strength is None else s.strength.name.lower(),
                    s.evidence,
                )
                for s in self.signals
            ),
            selected=tuple(routed(i.skill, i.reason) for i in self.selection.selected),
            dropped=tuple(routed(i.skill, i.reason) for i in self.selection.dropped),
            rejected_signals=self.rejected_signals,
            reason=self.reason,
            previous_id=previous_id,
            created_at="",
        )


@dataclass(slots=True)
class SkillRouter:
    catalog: SkillCatalog
    policy: RoutingPolicy
    config: SkillsConfig
    classifier: SignalClassifier | None = None
    """Optional. Without one, routing uses role defaults plus repository facts."""
    store: TaskStore | None = None
    """Canonical task state; when set, every decision is recorded."""

    def __post_init__(self) -> None:
        self.policy.check_catalog(self.catalog)

    @classmethod
    def from_config(
        cls,
        config: ConfigService,
        classifier: SignalClassifier | None = None,
        store: TaskStore | None = None,
        home: Path | None = None,
        estimator: TokenEstimator = DEFAULT_ESTIMATOR,
        task: dict[str, object] | None = None,
    ) -> SkillRouter:
        """Load the current catalog, policy, and effective skill config: always fresh, so edits
        to skill files apply without restarting Agent CLI."""
        settings = config.resolve(task).skills
        sources = skill_sources(settings.paths, config.paths.project_root, home)
        return cls(load_catalog(sources, estimator), load_policy(), settings, classifier, store)

    def route(self, requests: Sequence[RoutingRequest]) -> list[RoutingDecision]:
        """Route a batch of tasks: one classifier call at most, then deterministic selection."""
        ids = [request.task_id for request in requests]
        if len(set(ids)) != len(ids):
            raise RoutingError("each task may appear only once in a routing batch")
        deterministic = {
            request.task_id: self._repository_signals(request.facts) for request in requests
        }
        classification = classify(
            self.classifier,
            [
                self._classification_input(request, deterministic[request.task_id])
                for request in requests
            ],
            self.policy.vocabulary,
        )
        decisions = [
            self._decide(request, deterministic[request.task_id], classification)
            for request in requests
        ]
        if self.store is None:
            return decisions
        stored = self.store.record_skill_routings([decision.record() for decision in decisions])
        return [
            replace(decision, record_id=record.id)
            for decision, record in zip(decisions, stored, strict=True)
        ]

    def escalate(
        self, previous: RoutingDecision, request: RoutingRequest, discovery: Discovery
    ) -> RoutingDecision:
        """Reroute one task after ``discovery``, keeping its role and every earlier signal."""
        if not self.policy.dynamic_escalation:
            raise RoutingError("dynamic skill escalation is disabled by the routing policy")
        if (request.task_id, request.role) != (previous.task_id, previous.role):
            raise RoutingError("escalation cannot change a task's identity or role authority")
        if self.store is not None and previous.record_id is None:
            raise RoutingError("cannot record an escalation of a decision that was never recorded")
        unknown = [s for s in discovery.signals if s not in self.policy.vocabulary]
        if unknown:
            raise RoutingError(f"unknown signal(s) {', '.join(unknown)}")

        reported = tuple(
            DetectedSignal(s, SignalSource.ESCALATION, Strength.STRONG, (discovery.reason,))
            for s in discovery.signals
        )
        deterministic = union(
            previous.deterministic_signals,
            self._repository_signals(request.facts),
            self._repository_signals(discovery.facts),
            reported,
        )
        statement = f"{request.statement}\nNew finding: {discovery.reason}".strip()
        classification = classify(
            self.classifier,
            [
                self._classification_input(
                    request,
                    deterministic,
                    statement=statement,
                    found=() if discovery.facts is None else discovery.facts.paths,
                )
            ],
            self.policy.vocabulary,
        )
        decision = self._decide(
            request,
            union(deterministic, previous.classifier_signals),
            classification,
            kind="escalation",
            reason=discovery.reason,
        )
        added = tuple(name for name in decision.skills if name not in previous.skills)
        return self._persist(replace(decision, added=added), previous.record_id)

    def _repository_signals(self, facts: RepositoryFacts | None) -> tuple[DetectedSignal, ...]:
        return () if facts is None else repository_signals(facts, self.policy)

    def _classification_input(
        self,
        request: RoutingRequest,
        known: Sequence[DetectedSignal],
        statement: str | None = None,
        found: Sequence[str] = (),
    ) -> ClassificationInput:
        return ClassificationInput(
            task_id=request.task_id,
            role=request.role,
            statement=request.statement if statement is None else statement,
            spec_summary=request.spec_summary,
            paths=tuple(
                dict.fromkeys((*(() if request.facts is None else request.facts.paths), *found))
            ),
            known_signals=tuple(sorted({s.signal for s in known})),
        )

    def _decide(
        self,
        request: RoutingRequest,
        deterministic: Sequence[DetectedSignal],
        classification: Classification,
        kind: RoutingKind = "initial",
        reason: str = "",
    ) -> RoutingDecision:
        emitted = tuple(
            DetectedSignal(
                signal,
                SignalSource.CLASSIFIER,
                None,
                (classification.evidence.get(request.task_id, ""),)
                if classification.evidence.get(request.task_id)
                else (),
            )
            for signal in classification.signals.get(request.task_id, ())
        )
        signals = union(deterministic, emitted)
        selection = select_skills(
            request.role,
            self.catalog,
            self.policy,
            self.config.activation,
            self.config.context_budget_tokens,
            signals,
            request.requested,
        )
        return RoutingDecision(
            task_id=request.task_id,
            role=request.role,
            signals=signals,
            selection=selection,
            policy_version=self.policy.version,
            estimator=self.catalog.estimator_id,
            classifier=classification.identity,
            classifier_mode=classification.mode,
            rejected_signals=classification.rejected.get(request.task_id, ()),
            kind=kind,
            reason=reason,
        )

    def _persist(
        self, decision: RoutingDecision, previous_id: str | None = None
    ) -> RoutingDecision:
        if self.store is None:
            return decision
        stored = self.store.record_skill_routing(decision.record(previous_id))
        return replace(decision, record_id=stored.id)

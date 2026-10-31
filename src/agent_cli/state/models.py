"""Typed canonical task-state records.

These are the orchestration layer's source of truth (see docs/MEMORY.md). Records are plain
data: they hold IDs and explicit relationships, never adapter objects or chat transcripts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, get_args

from agent_cli.adapters.domain import Selection, WorkerStatus

type PinKind = Literal["model", "provider", "harness"]
type RequirementSource = Literal["user", "derived"]
type ReviewKind = Literal["spec", "code"]
type RoutingKind = Literal["initial", "escalation"]

PIN_KINDS: tuple[PinKind, ...] = get_args(PinKind.__value__)


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def finished(self) -> bool:
        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED)


class Approval(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    VOIDED = "voided"
    """Approval was withdrawn, for example by a material requirement change."""


@dataclass(frozen=True, slots=True)
class Session:
    id: str
    title: str
    open: bool
    created_at: str


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    session_id: str
    parent_id: str | None
    goal: str
    status: TaskStatus
    role: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    session_id: str
    task_id: str | None
    text: str
    source: RequirementSource
    created_at: str
    superseded_by: str | None


@dataclass(frozen=True, slots=True)
class Assumption:
    id: str
    task_id: str
    text: str
    made_by: str
    confirmed: bool | None
    """``None`` until confirmed or rejected. An unconfirmed assumption is never a fact."""
    created_at: str


@dataclass(frozen=True, slots=True)
class Decision:
    id: str
    session_id: str
    task_id: str | None
    question: str
    decision: str
    decided_by: str
    rationale: str
    created_at: str
    superseded_by: str | None


@dataclass(frozen=True, slots=True)
class SpecVersion:
    task_id: str
    version: int
    body: str
    diff: str
    """Unified diff from the previous version; empty for the first version."""
    approval: Approval
    created_at: str


@dataclass(frozen=True, slots=True)
class Pin:
    """A model, provider, or harness constraint on one task."""

    id: str
    task_id: str
    kind: PinKind
    value: str
    """A normalized ID, as used by ``agent_cli.adapters`` (never a vendor-native object)."""
    user_pinned: bool
    """Whether the user chose it explicitly. User pins persist until the user replaces them."""
    created_at: str

    def selection(self) -> Selection:
        return Selection(self.value, pinned=self.user_pinned)


@dataclass(frozen=True, slots=True)
class Worker:
    id: str
    task_id: str
    role: str
    model: str | None
    provider: str | None
    harness: str | None
    worktree: str | None
    status: WorkerStatus
    created_at: str
    heartbeat_at: str


@dataclass(frozen=True, slots=True)
class Finding:
    id: str
    task_id: str
    source: str
    severity: str
    summary: str
    requirement_id: str | None
    resolution: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class Blocker:
    id: str
    task_id: str
    reason: str
    owner: str
    """Who must act: for example ``user`` or ``orchestrator``."""
    resolution: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class Review:
    id: str
    task_id: str
    kind: ReviewKind
    round: int
    result: str
    reviewer: str
    spec_version: int | None
    created_at: str


@dataclass(frozen=True, slots=True)
class Artifact:
    """A produced artifact. Large contents live at ``reference``, not in the database."""

    id: str
    task_id: str
    kind: str
    reference: str
    metadata: dict[str, str]
    created_at: str


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    session_id: str
    task_id: str | None
    kind: str
    subject_id: str
    detail: dict[str, str]
    created_at: str


@dataclass(frozen=True, slots=True)
class RoutedSignal:
    signal: str
    source: str
    """``repository``, ``classifier``, or ``escalation``."""
    strength: str | None
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoutedSkill:
    name: str
    version: str
    source: str
    """Where the effective package came from: ``builtin``, ``global``, ``project``, ..."""
    tokens: int
    reason: str


@dataclass(frozen=True, slots=True)
class SkillRouting:
    """One skill-routing decision for a task: enough to reproduce and diagnose it.

    Records are append-only. An escalation links to the decision it extends; a routing miss is
    recorded here as a regression candidate and never changes the routing policy.
    """

    id: str
    task_id: str
    kind: RoutingKind
    role: str
    policy_version: str
    classifier: str | None
    """``name@version`` of the classifier, when one was wired."""
    classifier_mode: str
    """``none``, ``used``, or ``failed``."""
    classifier_input_version: str
    estimator: str
    budget_tokens: int
    total_tokens: int
    signals: tuple[RoutedSignal, ...]
    selected: tuple[RoutedSkill, ...]
    """In injection order."""
    dropped: tuple[RoutedSkill, ...]
    rejected_signals: tuple[str, ...]
    """Classifier output outside the vocabulary, ignored."""
    reason: str
    """Why an escalation happened (the newly discovered fact); empty for initial routing."""
    previous_id: str | None
    created_at: str

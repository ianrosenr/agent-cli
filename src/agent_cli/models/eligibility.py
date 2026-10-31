"""Role capability floors, deterministic eligibility and ranking, and explicit model pins.

Automatic selection only ever considers active registry models that meet the role's floor on its
primary capability. A user pin bypasses ranking and the floor for its scope, but never runtime
availability, and is never replaced by another model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import cast

from agent_cli.config.model import RoleFloors, Tier
from agent_cli.models.access import ModelPath, RuntimePaths, choose_path, unusable_reason, usable_paths
from agent_cli.models.domain import (
    ModelCapability,
    ModelEntry,
    ModelRegistry,
    ModelRoutingError,
)
from agent_cli.terms import CANONICAL_ROLES, AccessKind, Role

C = ModelCapability

FLOOR_SCORES: Mapping[Tier, int] = MappingProxyType({"low": 2, "mid": 3, "high": 4})

ROLE_CAPABILITIES: Mapping[Role, tuple[ModelCapability, ModelCapability]] = MappingProxyType(
    {
        "orchestrator": (C.REASONING, C.ARCHITECTURE),
        "spec_author": (C.REASONING, C.REPOSITORY_ENGINEERING),
        "spec_reviewer": (C.REASONING, C.REPOSITORY_ENGINEERING),
        "architect": (C.ARCHITECTURE, C.REASONING),
        "implementer": (C.CODING, C.REPOSITORY_ENGINEERING),
        "code_reviewer": (C.CODING, C.DEBUGGING),
        "research_investigator": (C.REASONING, C.TOOL_USE),
        "security_investigator": (C.SECURITY_REASONING, C.REASONING),
        "skill_author": (C.REASONING, C.REPOSITORY_ENGINEERING),
        "skill_evaluator": (C.REASONING, C.DEBUGGING),
    }
)
"""Role → (primary capability the floor applies to, secondary capability for ranking)."""


@dataclass(frozen=True, slots=True)
class RoleRequirement:
    role: Role
    primary: ModelCapability
    secondary: ModelCapability
    floor: Tier

    @property
    def minimum(self) -> int:
        return FLOOR_SCORES[self.floor]

    def met_by(self, entry: ModelEntry) -> bool:
        return entry.scores[self.primary] >= self.minimum


def requirement(role: str, floors: RoleFloors) -> RoleRequirement:
    """The floor for ``role``. Accepts any string so untyped input fails clearly here."""
    if role not in ROLE_CAPABILITIES:
        raise ModelRoutingError(
            f"unknown role {role!r} (expected one of {', '.join(CANONICAL_ROLES)})"
        )
    known = cast(Role, role)
    primary, secondary = ROLE_CAPABILITIES[known]
    floor: Tier = getattr(floors, known)
    return RoleRequirement(known, primary, secondary, floor)


def rank_key(entry: ModelEntry, need: RoleRequirement) -> tuple[int, int, int, int, int, str]:
    """Higher primary, higher secondary, higher confidence, lower latency, lower cost, ID."""
    return (
        -entry.scores[need.primary],
        -entry.scores[need.secondary],
        -entry.confidence.rank,
        entry.latency.rank,
        entry.cost.rank,
        entry.id,
    )


def eligible_models(registry: ModelRegistry, need: RoleRequirement) -> tuple[ModelEntry, ...]:
    """Active models meeting the floor, best first. Capability ranking only: budget, health,
    and harness constraints may still remove or reorder them later."""
    eligible = [entry for entry in registry.active() if need.met_by(entry)]
    return tuple(sorted(eligible, key=lambda entry: rank_key(entry, need)))


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    """One capability-ranked automatic worker model with every currently usable access path.

    V1-07 does not finalize an automatic model/provider pair. Later budget, provider-health,
    access, and harness policy may remove or reorder these candidates before execution.
    """

    requirement: RoleRequirement
    model_id: str
    entry: ModelEntry
    paths: tuple[ModelPath, ...]


def ranked_model_candidates(
    registry: ModelRegistry,
    role: Role,
    floors: RoleFloors,
    runtime: RuntimePaths,
    preference: Sequence[AccessKind],
    context_tokens: int | None = None,
) -> tuple[ModelCandidate, ...]:
    """Capability-ranked automatic candidates without committing to a model/provider pair."""
    need = requirement(role, floors)
    result: list[ModelCandidate] = []
    for entry in eligible_models(registry, need):
        paths = usable_paths(runtime.serving(entry.names), preference, context_tokens)
        if paths:
            result.append(ModelCandidate(need, entry.id, entry, paths))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ModelPin:
    """A model the user named for delegated work. A hard constraint, not routing evidence."""

    requested: str
    scope: str
    """What the pin covers, such as ``task:<id>`` or ``task:<id>/role:implementer``."""
    source: str = "user"
    """Where the pin came from, such as the user's message or a command."""


class SelectionKind(StrEnum):
    PINNED = "pinned"
    PINNED_UNSCORED = "pinned_unscored"
    """Pinned to a model the registry has no active scores for; no scores are invented."""


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """The model chosen for one role, with everything needed to record why."""

    requirement: RoleRequirement
    kind: SelectionKind
    model_id: str
    """The resolved canonical registry ID, or the requested ID for an unscored pin."""
    path: ModelPath
    entry: ModelEntry | None = None
    pin: ModelPin | None = None
    warnings: tuple[str, ...] = ()

    @property
    def pinned(self) -> bool:
        return self.pin is not None


class NoModelAvailable(ModelRoutingError):
    """Nothing can run the role. With a pin, the user must be asked: never substitute."""

    def __init__(self, message: str, pin: ModelPin | None = None) -> None:
        super().__init__(message)
        self.pin = pin


def select_model(
    registry: ModelRegistry,
    role: Role,
    floors: RoleFloors,
    runtime: RuntimePaths,
    preference: Sequence[AccessKind],
    pin: ModelPin,
    context_tokens: int | None = None,
) -> ModelSelection:
    """Resolve an explicit worker-model pin to one usable path.

    Automatic routing intentionally uses ``ranked_model_candidates`` instead so V1-08,
    V1-13, and the full router can apply budget, health, access, and harness policy before
    finalizing a model/provider pair.
    """
    need = requirement(role, floors)
    return select_pinned(registry, need, runtime, preference, pin, context_tokens)


def select_pinned(
    registry: ModelRegistry,
    need: RoleRequirement,
    runtime: RuntimePaths,
    preference: Sequence[AccessKind],
    pin: ModelPin,
    context_tokens: int | None,
) -> ModelSelection:
    entry = registry.get(pin.requested)
    names = (pin.requested,) if entry is None else entry.names
    serving = runtime.serving(names)
    path = choose_path(serving, preference, context_tokens)
    if path is None:
        reason = unusable_reason(serving, runtime, preference, context_tokens)
        raise NoModelAvailable(
            f"pinned model {pin.requested!r} ({pin.scope}) cannot run: {reason}; "
            "ask the user how to proceed",
            pin,
        )

    if entry is None or not entry.active:
        # Candidate scores are unreviewed, so a pinned candidate is treated as unscored.
        model_id = pin.requested if entry is None else entry.id
        warning = (
            f"{model_id} has no active registry scores; using it unscored because the user "
            f"pinned it for {pin.scope}"
        )
        return ModelSelection(
            need, SelectionKind.PINNED_UNSCORED, model_id, path, entry, pin, (warning,)
        )

    warnings: tuple[str, ...] = ()
    if not need.met_by(entry):
        warnings = (
            f"{entry.id} is below the {need.role} floor ({need.primary} "
            f"{entry.scores[need.primary]} < {need.minimum}); using it because the user pinned "
            f"it for {pin.scope}",
        )
    return ModelSelection(need, SelectionKind.PINNED, entry.id, path, entry, pin, warnings)


def problems_suffix(runtime: RuntimePaths) -> str:
    return f" ({'; '.join(runtime.problems)})" if runtime.problems else ""

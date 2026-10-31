"""Normalized routing signals and their provenance.

Deterministic signals come from V1-05 ``RepositoryFacts`` through the policy's
``repository_signals`` rules; the repository is never rescanned here. Classifier signals come
from the optional classifier seam. Both are unioned before skill selection, and each keeps its
provenance for telemetry.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum, StrEnum

from agent_cli.repository import RepositoryFacts
from agent_cli.skills.policy import Derivation, RoutingPolicy, Strength

EVIDENCE_PER_SIGNAL = 3


class SignalSource(StrEnum):
    REPOSITORY = "repository"
    """Derived deterministically from repository facts."""
    CLASSIFIER = "classifier"
    """Emitted by the injected semantic classifier."""
    ESCALATION = "escalation"
    """Reported, with evidence, when work turned out to cross a new policy domain."""


class Priority(IntEnum):
    """Why a skill was selected, strongest first. Lower values survive the budget longer."""

    ROLE_DEFAULT = 1
    REQUESTED = 2
    ALWAYS = 3
    STRONG_EVIDENCE = 4
    CLASSIFIER = 5
    WEAK_EVIDENCE = 6

    @property
    def required(self) -> bool:
        """Required skills are never dropped to fit the budget."""
        return self in (Priority.ROLE_DEFAULT, Priority.REQUESTED)


@dataclass(frozen=True, slots=True)
class DetectedSignal:
    signal: str
    source: SignalSource
    strength: Strength | None
    """Evidence strength for repository and escalation signals; ``None`` for the classifier."""
    evidence: tuple[str, ...] = ()

    @property
    def priority(self) -> Priority:
        if self.source is SignalSource.CLASSIFIER:
            return Priority.CLASSIFIER
        if self.strength is Strength.WEAK:
            return Priority.WEAK_EVIDENCE
        return Priority.STRONG_EVIDENCE


def repository_signals(facts: RepositoryFacts, policy: RoutingPolicy) -> tuple[DetectedSignal, ...]:
    """Signals implied by repository facts, each with the facts that produced it."""
    found: dict[str, DetectedSignal] = {}

    def add(fact: str, derivation: Derivation) -> None:
        evidence = tuple(
            f"{item.path}: {item.evidence}" for item in facts.evidence if item.fact == fact
        )[:EVIDENCE_PER_SIGNAL]
        for signal in derivation.signals:
            current = found.get(signal)
            if current is None or (current.strength or 0) < derivation.strength:
                found[signal] = DetectedSignal(
                    signal, SignalSource.REPOSITORY, derivation.strength, evidence
                )

    for category in facts.categories:
        if category in policy.categories:
            add(category, policy.categories[category])
    for name in facts.signals:
        if name in policy.facts:
            add(f"signal:{name}", policy.facts[name])
    return tuple(found.values())


def union(*groups: Iterable[DetectedSignal]) -> tuple[DetectedSignal, ...]:
    """Every distinct (signal, source) pair, keeping the strongest evidence for each."""
    merged: dict[tuple[str, SignalSource], DetectedSignal] = {}
    for group in groups:
        for detected in group:
            key = (detected.signal, detected.source)
            current = merged.get(key)
            if current is None or detected.priority < current.priority:
                merged[key] = detected
    return tuple(merged.values())

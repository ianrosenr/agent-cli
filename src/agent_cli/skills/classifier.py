"""The optional semantic-classifier seam.

A classifier maps compact subtask context to normalized signal IDs from the active vocabulary. It
never selects skills, never sees skill bodies, and never changes role authority. The protocol is
batch-first: classify every new or changed subtask of one orchestration cycle in one call.

No production classifier ships in V1-06; choosing its model and provider belongs to the model
registry (V1-07). With no classifier, routing uses role defaults plus repository facts, which is a
supported mode rather than an error.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from agent_cli.skills.policy import SignalVocabulary

CLASSIFIER_INPUT_VERSION = "1"
"""Bump when ``ClassificationInput`` changes, so routing evaluations compare like with like."""


@dataclass(frozen=True, slots=True)
class ClassificationInput:
    """Only what classification needs: no skill bodies, chat history, source, or catalogs."""

    task_id: str
    role: str
    statement: str
    spec_summary: str = ""
    paths: tuple[str, ...] = ()
    """Affected repository paths from V1-05 inspection."""
    known_signals: tuple[str, ...] = ()
    """Signals already derived deterministically."""


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    task_id: str
    signals: tuple[str, ...]
    evidence: str = ""


class SignalClassifier(Protocol):
    @property
    def identity(self) -> str:
        """``name@version`` of the classifier, recorded with every decision it informs."""
        ...

    def classify(
        self, inputs: Sequence[ClassificationInput], vocabulary: SignalVocabulary
    ) -> Sequence[ClassificationResult]: ...


class ClassifierMode(StrEnum):
    NONE = "none"
    """No classifier is wired: role defaults plus repository facts only."""
    USED = "used"
    FAILED = "failed"
    """The classifier raised; routing fell back as if none were wired."""


@dataclass(frozen=True, slots=True)
class Classification:
    """Accepted classifier output for one batch."""

    mode: ClassifierMode
    identity: str | None
    signals: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Task ID → accepted signals, in emitted order."""
    evidence: Mapping[str, str] = field(default_factory=dict)
    rejected: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Task ID → emitted IDs outside the vocabulary (including skill names), ignored."""
    error: str = ""


def classify(
    classifier: SignalClassifier | None,
    inputs: Sequence[ClassificationInput],
    vocabulary: SignalVocabulary,
) -> Classification:
    """Run one batched classification and keep only vocabulary signals for known tasks.

    A classifier failure is recorded and treated as no classifier: skill routing never escalates
    to an expensive model just to improve its own accuracy.
    """
    if classifier is None:
        return Classification(ClassifierMode.NONE, None)
    if not inputs:
        return Classification(ClassifierMode.USED, classifier.identity)
    try:
        results = list(classifier.classify(inputs, vocabulary))
    except Exception as error:  # an injected classifier must never break routing
        return Classification(ClassifierMode.FAILED, classifier.identity, error=str(error))

    expected = {item.task_id for item in inputs}
    signals: dict[str, tuple[str, ...]] = {}
    evidence: dict[str, str] = {}
    rejected: dict[str, tuple[str, ...]] = {}
    for result in results:
        if result.task_id not in expected:
            continue
        accepted = [s for s in dict.fromkeys(result.signals) if s in vocabulary]
        unknown = [s for s in dict.fromkeys(result.signals) if s not in vocabulary]
        signals[result.task_id] = (*signals.get(result.task_id, ()), *accepted)
        if unknown:
            rejected[result.task_id] = (*rejected.get(result.task_id, ()), *unknown)
        if result.evidence:
            evidence[result.task_id] = result.evidence
    return Classification(ClassifierMode.USED, classifier.identity, signals, evidence, rejected)


@dataclass(slots=True)
class FakeClassifier:
    """A deterministic classifier for tests: fixed signals per task ID, recording each batch."""

    answers: Mapping[str, Sequence[str]] = field(default_factory=dict)
    identity: str = "fake-classifier@1"
    batches: list[tuple[ClassificationInput, ...]] = field(default_factory=list)
    fail_with: Callable[[], Exception] | None = None

    def classify(
        self, inputs: Sequence[ClassificationInput], vocabulary: SignalVocabulary
    ) -> Sequence[ClassificationResult]:
        self.batches.append(tuple(inputs))
        if self.fail_with is not None:
            raise self.fail_with()
        return [
            ClassificationResult(item.task_id, tuple(self.answers.get(item.task_id, ())), "fake")
            for item in inputs
        ]

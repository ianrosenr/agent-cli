"""Choosing the model, if any, behind the optional semantic skill classifier (V1-06 seam).

A classifier only improves skill routing; it is never required. ``auto`` uses the cheapest
qualifying low-cost model and otherwise falls back to deterministic routing, never promoting an
expensive model to classify. ``selected`` uses exactly the user's model or none. ``disabled``
never classifies.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agent_cli.adapters import AdapterRegistry
from agent_cli.config.model import ClassifierConfig, ClassifierSelection
from agent_cli.models.access import ModelPath, RuntimePaths, choose_path, unusable_reason
from agent_cli.models.domain import Cost, ModelCapability, ModelEntry, ModelRegistry
from agent_cli.models.provider_classifier import ProviderInvoke, ProviderSignalClassifier
from agent_cli.skills.classifier import SignalClassifier
from agent_cli.terms import AccessKind

CLASSIFIER_CAPABILITY = ModelCapability.REASONING
CLASSIFIER_MINIMUM = 2
"""Fixed, intentionally low: classification is not planning. Never raised automatically."""
AUTO_COSTS = frozenset({Cost.FREE, Cost.LOW})

LOW_COST_RECOMMENDATION = (
    "No low-cost routing model is configured.",
    "Agent CLI will continue using repository facts and deterministic routing.",
    "",
    "For better semantic routing, configure a low-cost model",
    "or explicitly select one of your existing models for classification.",
)


@dataclass(frozen=True, slots=True)
class ClassifierChoice:
    """The effective classifier. ``path`` is ``None`` for deterministic-only routing."""

    mode: ClassifierSelection
    path: ModelPath | None = None
    model_id: str | None = None
    """The canonical registry ID (or the selected ID, when unscored)."""
    reason: str = ""
    """Why routing is deterministic only, when it is."""

    @property
    def deterministic(self) -> bool:
        return self.path is None

    @property
    def explicit(self) -> bool:
        return self.mode == "selected"

    @property
    def recommend_low_cost(self) -> bool:
        """``auto`` found no low-cost path: suggest configuring one (never required)."""
        return self.mode == "auto" and self.deterministic


def auto_candidates(registry: ModelRegistry) -> tuple[ModelEntry, ...]:
    """Active low-cost models meeting the classifier floor: cheapest, fastest, ablest first."""
    candidates = [
        entry
        for entry in registry.active()
        if entry.cost in AUTO_COSTS and entry.scores[CLASSIFIER_CAPABILITY] >= CLASSIFIER_MINIMUM
    ]
    return tuple(
        sorted(
            candidates,
            key=lambda entry: (
                entry.cost.rank,
                entry.latency.rank,
                -entry.scores[CLASSIFIER_CAPABILITY],
                -entry.confidence.rank,
                entry.id,
            ),
        )
    )


def select_classifier(
    settings: ClassifierConfig,
    registry: ModelRegistry,
    runtime: RuntimePaths,
    preference: Sequence[AccessKind],
) -> ClassifierChoice:
    if settings.mode == "disabled":
        return ClassifierChoice("disabled", reason="classifier disabled in config")

    if settings.mode == "selected":
        requested = settings.selected_model or ""
        entry = registry.get(requested)
        model_id = requested if entry is None else entry.id
        names = (requested,) if entry is None else entry.names
        serving = runtime.serving(names)
        path = choose_path(serving, preference)
        if path is None:
            reason = unusable_reason(serving, runtime, preference)
            return ClassifierChoice(
                "selected",
                model_id=model_id,
                reason=f"selected classifier model {model_id!r} cannot run: {reason}",
            )
        return ClassifierChoice("selected", path, model_id)

    for entry in auto_candidates(registry):
        path = choose_path(runtime.serving(entry.names), preference)
        if path is not None:
            return ClassifierChoice("auto", path, entry.id)
    return ClassifierChoice("auto", reason="no low-cost classifier model is reachable")


def build_classifier(
    choice: ClassifierChoice,
    adapters: AdapterRegistry,
    budgeted_invoke: ProviderInvoke | None = None,
) -> SignalClassifier | None:
    """The classifier to inject into ``SkillRouter``, or ``None`` for deterministic routing."""
    if choice.path is None:
        return None
    adapter = adapters.provider(choice.path.provider.id)
    if adapter is None:
        return None
    return ProviderSignalClassifier(
        adapter, choice.path, choice.mode, budgeted_invoke=budgeted_invoke
    )

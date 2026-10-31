"""The model registry, role capability floors, and model selection for workers and the
skill classifier.

The registry holds model capability facts only; provider adapters report how each model can be
reached at runtime. Selection here is capability-based and deterministic; budget (V1-08),
provider health (V1-13), harness choice, and discovery (V1-14) live elsewhere. See
docs/ROUTING.md.
"""

from agent_cli.models.access import (
    ModelPath,
    RuntimePaths,
    choose_path,
    runtime_paths,
    usable_paths,
)
from agent_cli.models.classifier_selection import (
    CLASSIFIER_MINIMUM,
    ClassifierChoice,
    auto_candidates,
    build_classifier,
    select_classifier,
)
from agent_cli.models.domain import (
    Confidence,
    Cost,
    Latency,
    ModelCapability,
    ModelEntry,
    ModelRegistry,
    ModelRoutingError,
    ModelStatus,
    RegistryError,
    Scores,
)
from agent_cli.models.eligibility import (
    FLOOR_SCORES,
    ROLE_CAPABILITIES,
    ModelCandidate,
    ModelPin,
    ModelSelection,
    NoModelAvailable,
    RoleRequirement,
    SelectionKind,
    eligible_models,
    ranked_model_candidates,
    requirement,
    select_model,
)
from agent_cli.models.provider_classifier import ProviderSignalClassifier
from agent_cli.models.registry import load_registry, parse_registry
from agent_cli.models.status import models_lines

__all__ = [
    "CLASSIFIER_MINIMUM",
    "FLOOR_SCORES",
    "ROLE_CAPABILITIES",
    "ClassifierChoice",
    "Confidence",
    "Cost",
    "Latency",
    "ModelCapability",
    "ModelCandidate",
    "ModelEntry",
    "ModelPath",
    "ModelPin",
    "ModelRegistry",
    "ModelRoutingError",
    "ModelSelection",
    "ModelStatus",
    "NoModelAvailable",
    "ProviderSignalClassifier",
    "RegistryError",
    "RoleRequirement",
    "RuntimePaths",
    "Scores",
    "SelectionKind",
    "auto_candidates",
    "build_classifier",
    "choose_path",
    "eligible_models",
    "load_registry",
    "models_lines",
    "parse_registry",
    "ranked_model_candidates",
    "requirement",
    "runtime_paths",
    "select_classifier",
    "select_model",
    "usable_paths",
]

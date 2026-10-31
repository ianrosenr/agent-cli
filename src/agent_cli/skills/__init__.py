"""Skill discovery, deterministic skill routing, and skill-context budgeting.

Roles define authority; skills only add policy. Discovery reads packages, the policy maps signals
to skills, an optional classifier emits signals, and the selector combines them. Nothing here
chooses models, providers, or harnesses. See docs/SKILLS.md and docs/SKILL_ROUTING.md.
"""

from agent_cli.assets import builtin_root
from agent_cli.skills.classifier import (
    CLASSIFIER_INPUT_VERSION,
    ClassificationInput,
    ClassificationResult,
    ClassifierMode,
    FakeClassifier,
    SignalClassifier,
)
from agent_cli.skills.discovery import SkillCatalog, load_catalog, skill_sources
from agent_cli.skills.models import RoutingError, Skill, SkillError, SkillSource, SourceKind
from agent_cli.skills.policy import RoutingPolicy, SignalVocabulary, Strength, load_policy
from agent_cli.skills.router import Discovery, RoutingDecision, RoutingRequest, SkillRouter
from agent_cli.skills.selection import DroppedSkill, SelectedSkill, SkillSelection
from agent_cli.skills.signals import DetectedSignal, Priority, SignalSource
from agent_cli.skills.status import skills_view
from agent_cli.skills.tokens import DEFAULT_ESTIMATOR, TokenEstimator, Utf8ByteEstimator

__all__ = [
    "CLASSIFIER_INPUT_VERSION",
    "DEFAULT_ESTIMATOR",
    "ClassificationInput",
    "ClassificationResult",
    "ClassifierMode",
    "DetectedSignal",
    "Discovery",
    "DroppedSkill",
    "FakeClassifier",
    "Priority",
    "RoutingDecision",
    "RoutingError",
    "RoutingPolicy",
    "RoutingRequest",
    "SelectedSkill",
    "SignalClassifier",
    "SignalSource",
    "SignalVocabulary",
    "Skill",
    "SkillCatalog",
    "SkillError",
    "SkillRouter",
    "SkillSelection",
    "SkillSource",
    "SourceKind",
    "Strength",
    "TokenEstimator",
    "Utf8ByteEstimator",
    "builtin_root",
    "load_catalog",
    "load_policy",
    "skill_sources",
    "skills_view",
]

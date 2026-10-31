"""The typed skill-routing policy (``builtins/policies/skill-routing.yaml``).

The policy maps normalized signals to skills and roles to their required default skills. It never
calls a model, and it is never injected into a worker prompt.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

import yaml

from agent_cli.skills.discovery import POLICIES_DIR, SkillCatalog, builtin_root
from agent_cli.skills.models import SkillError
from agent_cli.skills.parser import is_skill_name

type Fail = Callable[[str], SkillError]

POLICY_FILE = "skill-routing.yaml"
TOP_LEVEL_KEYS = (
    "version",
    "dynamic_escalation",
    "signals",
    "role_defaults",
    "repository_signals",
    "skills",
)
REPOSITORY_KINDS = ("categories", "facts")


class Strength(IntEnum):
    """How strongly repository evidence implies a signal. Higher is stronger."""

    WEAK = 1
    STRONG = 2


@dataclass(frozen=True, slots=True)
class Derivation:
    """Signals implied by one repository category or fact."""

    signals: tuple[str, ...]
    strength: Strength


@dataclass(frozen=True, slots=True)
class SignalVocabulary:
    """The normalized signal IDs a classifier may emit, with the policy version that owns them."""

    version: str
    signals: frozenset[str]

    def __contains__(self, signal: object) -> bool:
        return signal in self.signals


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    version: str
    dynamic_escalation: bool
    signals: tuple[str, ...]
    role_defaults: dict[str, tuple[str, ...]]
    categories: dict[str, Derivation]
    """Repository path categories (``RepositoryFacts.categories``) → signals."""
    facts: dict[str, Derivation]
    """Repository signal facts (``RepositoryFacts.signals``) → signals."""
    skills: dict[str, tuple[str, ...]]
    """Skill → the signals that make it eligible, in policy (tie-breaking) order."""

    vocabulary: SignalVocabulary = field(init=False)
    ranks: dict[str, int] = field(init=False)
    """Skill → position in the ``skills`` mapping, for tie-breaking."""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "vocabulary", SignalVocabulary(self.version, frozenset(self.signals))
        )
        object.__setattr__(self, "ranks", {skill: rank for rank, skill in enumerate(self.skills)})

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(self.role_defaults)

    def order(self, skill: str) -> int:
        """Tie-breaking rank: position in the policy's ``skills`` mapping, unmapped skills last."""
        return self.ranks.get(skill, len(self.ranks))

    def skills_for(self, signal: str) -> tuple[str, ...]:
        return tuple(skill for skill, signals in self.skills.items() if signal in signals)

    def check_catalog(self, catalog: SkillCatalog) -> None:
        """Every skill the policy names must exist in the effective catalog."""
        named = [skill for defaults in self.role_defaults.values() for skill in defaults]
        named += list(self.skills)
        missing = sorted({skill for skill in named if skill not in catalog})
        if missing:
            raise SkillError(
                f"routing policy {self.version} names skills missing from the catalog: "
                + ", ".join(missing)
            )


def default_policy_path() -> Path:
    return builtin_root() / POLICIES_DIR / POLICY_FILE


def load_policy(path: Path | None = None) -> RoutingPolicy:
    path = default_policy_path() if path is None else path
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise SkillError(f"{path}: cannot read routing policy: {error}") from error
    return parse_policy(data, str(path))


def parse_policy(data: object, where: str) -> RoutingPolicy:
    def fail(problem: str) -> SkillError:
        return SkillError(f"{where}: {problem}")

    top = mapping(data, "policy", fail)
    unknown = [str(key) for key in top if key not in TOP_LEVEL_KEYS]
    if unknown:
        raise fail(f"unknown key(s) {', '.join(unknown)}")
    missing = [key for key in TOP_LEVEL_KEYS if key not in top]
    if missing:
        raise fail(f"missing key(s) {', '.join(missing)}")

    version = top["version"]
    if isinstance(version, bool) or not isinstance(version, (int, str)) or str(version) == "":
        raise fail("version must be an integer or string")
    if not isinstance(top["dynamic_escalation"], bool):
        raise fail("dynamic_escalation must be true or false")

    signals = names(top["signals"], "signals", fail)
    vocabulary = set(signals)
    if not signals:
        raise fail("signals must not be empty")

    def known_signals(value: object, key: str) -> tuple[str, ...]:
        found = names(value, key, fail)
        unknown = [signal for signal in found if signal not in vocabulary]
        if unknown:
            raise fail(f"{key}: unknown signal(s) {', '.join(unknown)}")
        return found

    def skill_names(value: object, key: str) -> tuple[str, ...]:
        found = names(value, key, fail)
        invalid = [skill for skill in found if not is_skill_name(skill)]
        if invalid:
            raise fail(f"{key}: invalid skill name(s) {', '.join(invalid)}")
        return found

    role_defaults: dict[str, tuple[str, ...]] = {}
    for role, defaults in mapping(top["role_defaults"], "role_defaults", fail).items():
        role_defaults[str(role)] = skill_names(defaults, f"role_defaults.{role}")
    if not role_defaults:
        raise fail("role_defaults must name at least one role")

    skills: dict[str, tuple[str, ...]] = {}
    for skill, entry in mapping(top["skills"], "skills", fail).items():
        key = f"skills.{skill}"
        if not is_skill_name(str(skill)):
            raise fail(f"{key}: invalid skill name")
        fields = mapping(entry, key, fail)
        if set(fields) != {"signals"}:
            raise fail(f"{key}: expected exactly one key, signals")
        skills[str(skill)] = known_signals(fields["signals"], f"{key}.signals")

    unused = sorted(vocabulary - {signal for mapped in skills.values() for signal in mapped})
    if unused:
        raise fail(f"signals mapped to no skill: {', '.join(unused)}")

    repository = mapping(top["repository_signals"], "repository_signals", fail)
    if set(repository) != set(REPOSITORY_KINDS):
        raise fail(f"repository_signals must have exactly {', '.join(REPOSITORY_KINDS)}")
    derived: dict[str, dict[str, Derivation]] = {}
    for kind in REPOSITORY_KINDS:
        derived[kind] = {}
        for fact, entry in mapping(repository[kind], f"repository_signals.{kind}", fail).items():
            key = f"repository_signals.{kind}.{fact}"
            fields = mapping(entry, key, fail)
            if set(fields) != {"signals", "strength"}:
                raise fail(f"{key}: expected exactly signals and strength")
            strength = fields["strength"]
            if strength not in ("strong", "weak"):
                raise fail(f"{key}.strength: must be strong or weak")
            derived[kind][str(fact)] = Derivation(
                known_signals(fields["signals"], f"{key}.signals"), Strength[strength.upper()]
            )

    return RoutingPolicy(
        version=str(version),
        dynamic_escalation=top["dynamic_escalation"],
        signals=signals,
        role_defaults=role_defaults,
        categories=derived["categories"],
        facts=derived["facts"],
        skills=skills,
    )


def mapping(value: object, key: str, fail: Fail) -> Mapping[Any, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise fail(f"{key} must be a mapping")
    return value


def names(value: object, key: str, fail: Fail) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise fail(f"{key} must be a list of names")
    if len(set(value)) != len(value):
        raise fail(f"{key} must not repeat entries")
    return tuple(value)

"""Deterministic skill selection and context budgeting.

Combines role defaults, explicit requests, activation, and detected signals into the skills for
one task, in injection order, then fits optional skills to the skill-context budget. It never
asks a model which skills to load and never truncates a skill.

Priority, strongest first (see ``Priority``): role defaults, explicit requests, ``always``
activation, strong repository evidence, classifier signals, weak repository evidence. Role
defaults and requests keep their declared order; other ties break by policy order, then by name.
Filesystem order never matters.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from agent_cli.config.model import SkillActivation
from agent_cli.skills.discovery import SkillCatalog
from agent_cli.skills.models import RoutingError, Skill
from agent_cli.skills.policy import RoutingPolicy
from agent_cli.skills.signals import DetectedSignal, Priority


@dataclass(frozen=True, slots=True)
class SelectedSkill:
    skill: Skill
    priority: Priority
    reason: str

    @property
    def name(self) -> str:
        return self.skill.name

    @property
    def tokens(self) -> int:
        return self.skill.tokens


@dataclass(frozen=True, slots=True)
class DroppedSkill:
    skill: Skill
    reason: str

    @property
    def name(self) -> str:
        return self.skill.name


@dataclass(frozen=True, slots=True)
class SkillSelection:
    selected: tuple[SelectedSkill, ...]
    """In injection order."""
    dropped: tuple[DroppedSkill, ...]
    """Matched but not selected: excluded by activation, or dropped to fit the budget."""
    budget_tokens: int

    @property
    def total_tokens(self) -> int:
        return sum(item.tokens for item in self.selected)


def activation_of(name: str, activation: Mapping[str, SkillActivation]) -> SkillActivation:
    return activation.get(name, "auto")


def select_skills(
    role: str,
    catalog: SkillCatalog,
    policy: RoutingPolicy,
    activation: Mapping[str, SkillActivation],
    budget_tokens: int,
    signals: Sequence[DetectedSignal] = (),
    requested: Sequence[str] = (),
) -> SkillSelection:
    if role not in policy.role_defaults:
        raise RoutingError(f"unknown role {role!r} (expected one of {', '.join(policy.roles)})")

    chosen: dict[str, SelectedSkill] = {}
    dropped: list[DroppedSkill] = []

    def lookup(name: str, why: str) -> Skill:
        skill = catalog.get(name)
        if skill is None:
            raise RoutingError(f"{why} skill {name!r} is not in the skill catalog")
        return skill

    def choose(skill: Skill, priority: Priority, reason: str) -> None:
        if skill.name not in chosen:
            chosen[skill.name] = SelectedSkill(skill, priority, reason)

    for name in policy.role_defaults[role]:
        skill = lookup(name, f"required {role}")
        if activation_of(name, activation) == "disabled":
            raise RoutingError(
                f"skill {name!r} is required for the {role} role but is disabled in "
                "skills.activation; re-enable it rather than weakening the role's policy"
            )
        choose(skill, Priority.ROLE_DEFAULT, f"role default for {role}")

    for name in requested:
        skill = lookup(name, "requested")
        if activation_of(name, activation) == "disabled":
            raise RoutingError(f"requested skill {name!r} is disabled in skills.activation")
        choose(skill, Priority.REQUESTED, "explicitly requested")

    for name, mode in sorted(activation.items()):
        always = catalog.get(name) if mode == "always" else None
        if always is not None:  # unknown names are ignored, as for every activation value
            choose(always, Priority.ALWAYS, "activation: always")

    matches: dict[str, list[DetectedSignal]] = {}
    for detected in signals:
        for name in policy.skills_for(detected.signal):
            matches.setdefault(name, []).append(detected)
    for name in sorted(matches, key=lambda n: (policy.order(n), n)):
        candidate = catalog.get(name)
        if candidate is None or name in chosen:
            continue
        matched = sorted(matches[name], key=lambda d: (d.priority, d.signal, d.source))
        mode = activation_of(name, activation)
        if mode in ("explicit", "disabled"):
            dropped.append(DroppedSkill(candidate, f"activation: {mode}"))
            continue
        reason = "signals: " + ", ".join(f"{d.signal} ({d.source})" for d in matched)
        choose(candidate, matched[0].priority, reason)

    defaults = policy.role_defaults[role]

    def rank(item: SelectedSkill) -> tuple[int, int, str]:
        if item.priority is Priority.ROLE_DEFAULT:
            return (item.priority, defaults.index(item.name), item.name)
        if item.priority is Priority.REQUESTED:
            return (item.priority, list(requested).index(item.name), item.name)
        return (item.priority, policy.order(item.name), item.name)

    ordered = sorted(chosen.values(), key=rank)
    return fit_budget(ordered, dropped, budget_tokens)


def fit_budget(
    ordered: list[SelectedSkill], dropped: list[DroppedSkill], budget_tokens: int
) -> SkillSelection:
    """Drop the weakest optional skills until the selection fits. Required skills always stay."""
    required = sum(item.tokens for item in ordered if item.priority.required)
    if required > budget_tokens:
        names = ", ".join(item.name for item in ordered if item.priority.required)
        raise RoutingError(
            f"required skills ({names}) need about {required} tokens, over the "
            f"skills.context_budget_tokens budget of {budget_tokens}; raise the budget"
        )
    kept = list(ordered)
    total = sum(item.tokens for item in kept)
    for item in reversed(ordered):
        if total <= budget_tokens:
            break
        if item.priority.required:
            continue
        kept.remove(item)
        dropped.append(
            DroppedSkill(
                item.skill,
                f"context budget: needed {item.tokens} tokens; selection was {total} "
                f"of {budget_tokens}",
            )
        )
        total -= item.tokens
    return SkillSelection(tuple(kept), tuple(dropped), budget_tokens)

"""Vocabulary shared by configuration and adapters, so neither package depends on the other."""

from __future__ import annotations

from typing import Literal, get_args

type ImplementationMode = Literal["autopilot", "checkpoint", "plan-only"]
type AccessKind = Literal["subscription", "local", "api"]
type Role = Literal[
    "orchestrator",
    "spec_author",
    "spec_reviewer",
    "architect",
    "implementer",
    "code_reviewer",
    "research_investigator",
    "security_investigator",
    "skill_author",
    "skill_evaluator",
]
"""The canonical delegated roles. Skill routing and model routing share exactly this set."""

IMPLEMENTATION_MODES: tuple[str, ...] = get_args(ImplementationMode.__value__)
ACCESS_KINDS: tuple[str, ...] = get_args(AccessKind.__value__)
CANONICAL_ROLES: tuple[str, ...] = get_args(Role.__value__)

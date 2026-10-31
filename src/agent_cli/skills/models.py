"""Typed skill records and errors.

A skill is policy, knowledge, or methods for a worker. It never grants authority: authority comes
from the role (see docs/SKILLS.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class SkillError(Exception):
    """A skill package, source, or the routing policy is invalid."""


class RoutingError(Exception):
    """Skills could not be routed for a task without weakening required policy."""


class SourceKind(StrEnum):
    """Where a skill came from, in precedence order: later kinds replace earlier ones."""

    BUILTIN = "builtin"
    GLOBAL = "global"
    PROJECT = "project"
    CONFIGURED = "configured"


@dataclass(frozen=True, slots=True)
class SkillSource:
    kind: SourceKind
    root: Path
    """A directory of skill packages: ``<root>/<skill-name>/SKILL.md``."""


@dataclass(frozen=True, slots=True)
class Skill:
    """One resolved skill package. Same-named packages from later sources replace it whole."""

    name: str
    description: str
    version: str
    source: SkillSource
    path: Path
    """The package's ``SKILL.md``."""
    body: str
    """Worker-facing policy text, without front matter. Never truncated."""
    tokens: int
    """Estimated tokens of ``body``, from the catalog's token estimator."""

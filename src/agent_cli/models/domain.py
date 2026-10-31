"""Model capability facts: what the registry knows about a model, independent of access paths.

A registry entry describes one canonical model. How that model is reached right now
(subscription, local, API; which provider) is runtime state reported by provider adapters and is
never stored here.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, fields
from enum import StrEnum

MIN_SCORE = 1
MAX_SCORE = 5
"""Capability scores are integers: 1 weak, 2 usable for bounded work, 3 competent, 4 strong,
5 exceptional."""


class RegistryError(Exception):
    """The model registry asset is invalid."""


class ModelRoutingError(Exception):
    """A routing question cannot be answered, such as one about an unknown role."""


class Ranked(StrEnum):
    """A category whose declaration order is its order: earlier members rank lower."""

    @property
    def rank(self) -> int:
        return list(type(self)).index(self)


class ModelCapability(StrEnum):
    REASONING = "reasoning"
    CODING = "coding"
    REPOSITORY_ENGINEERING = "repository_engineering"
    DEBUGGING = "debugging"
    TOOL_USE = "tool_use"
    ARCHITECTURE = "architecture"
    SECURITY_REASONING = "security_reasoning"


class ModelStatus(StrEnum):
    ACTIVE = "active"
    CANDIDATE = "candidate"
    """Known but never selected automatically."""


class Latency(Ranked):
    FAST = "fast"
    MEDIUM = "medium"
    SLOW = "slow"


class Cost(Ranked):
    """A coarse routing hint, not provider pricing or the spend ledger (V1-08)."""

    FREE = "free"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Confidence(Ranked):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class Scores:
    """One integer score per ``ModelCapability``; field names match its values."""

    reasoning: int
    coding: int
    repository_engineering: int
    debugging: int
    tool_use: int
    architecture: int
    security_reasoning: int

    def __getitem__(self, capability: ModelCapability) -> int:
        value: int = getattr(self, capability.value)
        return value

    def __iter__(self) -> Iterator[tuple[ModelCapability, int]]:
        return ((ModelCapability(f.name), getattr(self, f.name)) for f in fields(self))


@dataclass(frozen=True, slots=True)
class ModelEntry:
    id: str
    """The canonical model ID."""
    display_name: str
    status: ModelStatus
    scores: Scores
    latency: Latency
    cost: Cost
    confidence: Confidence
    sources: tuple[str, ...]
    """Concise evidence labels for the scores."""
    aliases: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return self.status is ModelStatus.ACTIVE

    @property
    def names(self) -> tuple[str, ...]:
        """Every ID a provider may report this model as: the canonical ID, then aliases."""
        return (self.id, *self.aliases)


@dataclass(frozen=True, slots=True)
class ModelRegistry:
    version: str
    models: tuple[ModelEntry, ...]
    lookup: dict[str, ModelEntry] = field(init=False, repr=False, compare=False)
    """Case-folded canonical ID or alias → entry."""

    def __post_init__(self) -> None:
        lookup: dict[str, ModelEntry] = {}
        for entry in self.models:
            for name in entry.names:
                key = name.casefold()
                if key in lookup:
                    other = lookup[key].id
                    raise RegistryError(
                        f"model {entry.id!r}: name {name!r} is already used by {other!r}"
                        if other != entry.id
                        else f"model {entry.id!r}: name {name!r} is repeated"
                    )
                lookup[key] = entry
        object.__setattr__(self, "lookup", lookup)

    def __iter__(self) -> Iterator[ModelEntry]:
        return iter(self.models)

    def __len__(self) -> int:
        return len(self.models)

    def get(self, name: str) -> ModelEntry | None:
        """The entry whose canonical ID or alias is ``name``, ignoring case."""
        return self.lookup.get(name.strip().casefold())

    def active(self) -> tuple[ModelEntry, ...]:
        return tuple(entry for entry in self.models if entry.active)

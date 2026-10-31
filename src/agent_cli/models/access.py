"""Matching registry models to the provider paths that can run them right now.

Paths are runtime facts read from READY provider adapters on demand and never persisted. Model
identity (the registry entry) stays separate from provider identity (the path).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from agent_cli.adapters import AdapterRegistry, ProviderIdentity
from agent_cli.config import Config
from agent_cli.terms import AccessKind


@dataclass(frozen=True, slots=True)
class ModelPath:
    """One model reachable through one READY provider."""

    model_id: str
    """The model ID as the provider reports it; requests use exactly this."""
    provider: ProviderIdentity
    context_limit: int | None = None

    @property
    def access(self) -> AccessKind | None:
        return self.provider.access

    def describe(self) -> str:
        return f"{self.provider.id} ({self.access or 'unknown access'})"


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    paths: tuple[ModelPath, ...] = ()
    problems: tuple[str, ...] = ()
    """Why enabled providers contributed no paths, for status and error messages."""

    def serving(self, names: Collection[str]) -> tuple[ModelPath, ...]:
        """Paths whose reported model ID is one of ``names`` (canonical ID or aliases)."""
        wanted = {name.strip().casefold() for name in names}
        return tuple(path for path in self.paths if path.model_id.casefold() in wanted)


def runtime_paths(adapters: AdapterRegistry, config: Config) -> RuntimePaths:
    """Every available model on every enabled, READY provider. A misbehaving adapter is
    reported and skipped, never allowed to break routing."""
    paths: list[ModelPath] = []
    problems: list[str] = []
    for provider_id, adapter in sorted(adapters.providers().items()):
        configured = config.providers.get(provider_id)
        if configured is not None and not configured.enabled:
            continue
        try:
            readiness = adapter.readiness()
            if not readiness.ready:
                problems.append(f"{provider_id}: {readiness.describe()}")
                continue
            identity = adapter.identity()
            models = adapter.list_models()
        except Exception as error:
            problems.append(f"{provider_id}: adapter error: {error}")
            continue
        paths += [
            ModelPath(model.model_id, identity, model.context_limit)
            for model in models
            if model.available
        ]
    return RuntimePaths(tuple(paths), tuple(problems))


def choose_path(
    candidates: Sequence[ModelPath],
    preference: Sequence[AccessKind],
    context_tokens: int | None = None,
) -> ModelPath | None:
    """The preferred path for one model, by access preference then provider ID.

    Paths whose access kind is not in ``preference`` (or unknown) are never used, so a
    preference without ``api`` never reaches a paid API. A path with an unknown context limit
    is assumed to fit.
    """
    order = {kind: rank for rank, kind in enumerate(preference)}
    ranked = [
        (order[access], path.provider.id, path)
        for path in candidates
        if exclusion(path, preference, context_tokens) is None
        and (access := path.access) is not None
    ]
    if not ranked:
        return None
    return min(ranked, key=lambda item: item[:2])[2]


def exclusion(
    path: ModelPath, preference: Sequence[AccessKind], context_tokens: int | None
) -> str | None:
    """Why ``path`` cannot be used, or ``None`` when it can."""
    if path.access is None:
        return f"{path.provider.id} serves it, but does not report its access kind"
    if path.access not in preference:
        return f"{path.provider.id} serves it over {path.access}, which access.preference excludes"
    if (
        context_tokens is not None
        and path.context_limit is not None
        and path.context_limit < context_tokens
    ):
        return (
            f"{path.provider.id} serves it with a {path.context_limit}-token context, "
            f"below the {context_tokens} needed"
        )
    return None


def unusable_reason(
    candidates: Sequence[ModelPath],
    runtime: RuntimePaths,
    preference: Sequence[AccessKind],
    context_tokens: int | None = None,
) -> str:
    """Why none of ``candidates`` (the paths serving one model) can be used."""
    reasons = [
        reason
        for path in candidates
        if (reason := exclusion(path, preference, context_tokens)) is not None
    ]
    if not reasons:
        reasons = ["no ready provider serves it"]
    return "; ".join([*reasons, *runtime.problems])

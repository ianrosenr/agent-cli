"""Candidate-path economics: filter and order V1-07 candidate paths by budget and access.

Quoting reads a spend snapshot but never reserves: reservation happens only immediately before
the chosen path is actually invoked or launched, after health and harness policy pick it.

Order of admissible paths:

1. ``access.preference`` (by default subscription, then local, then API);
2. subscription and local paths keep their V1-07 capability rank, then provider ID;
3. API paths prefer the lower reserved cost, then capability rank, then provider and model ID.

So a lower-ranked but capable subscription model can beat a stronger paid API model.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from agent_cli.budget.domain import (
    PAID_ACCESS,
    USER_ACTION,
    BudgetReason,
    CostEstimate,
    Limits,
    PaidCall,
    Rejection,
    SpendUsage,
)
from agent_cli.budget.policy import check_estimate, check_limits
from agent_cli.budget.pricing import CostEstimator
from agent_cli.models import ModelCandidate, ModelPath, NoModelAvailable, PinnedPaths
from agent_cli.terms import AccessKind

HARNESS_BOUND_SOURCE = "harness-enforced-bound"


@dataclass(frozen=True, slots=True)
class CallBounds:
    """What every path for one unit of work would be asked to do, for quoting."""

    purpose: str
    input_tokens: int | None
    """A conservative input upper bound; ``None`` when unknown."""
    max_output_tokens: int | None
    root_task_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None

    def call(self, path: ModelPath) -> PaidCall:
        return PaidCall(
            provider_id=path.provider.id,
            access=path.access or PAID_ACCESS,
            model_id=path.model_id,
            purpose=self.purpose,
            input_tokens=self.input_tokens,
            max_output_tokens=self.max_output_tokens,
            root_task_id=self.root_task_id,
            task_id=self.task_id,
            session_id=self.session_id,
        )


@dataclass(frozen=True, slots=True)
class PathQuote:
    """One model/provider path with its budget verdict."""

    model_id: str
    """The canonical registry ID (or the pinned name for an unscored pin)."""
    path: ModelPath
    capability_rank: int
    """The model's V1-07 position, 0 best."""
    call: PaidCall
    estimate: CostEstimate | None
    """The conservative bound reserved if this path runs; ``None`` for unpaid access."""
    rejection: Rejection | None = None

    @property
    def paid(self) -> bool:
        return self.call.paid

    @property
    def admissible(self) -> bool:
        return self.rejection is None

    @property
    def cost_micros(self) -> int:
        return 0 if self.estimate is None else self.estimate.usd_micros

    def describe(self) -> str:
        return f"{self.model_id} via {self.path.describe()}"


class BudgetBlocked(Exception):
    """No candidate path fits the hard budget rules. The user must act; approval cannot help."""

    def __init__(self, rejected: Sequence[PathQuote], subject: str = "this work") -> None:
        self.rejected = tuple(rejected)
        reasons = "; ".join(
            f"{quote.describe()}: {quote.rejection}" for quote in rejected if quote.rejection
        )
        detail = reasons or "no usable path"
        super().__init__(f"no budget-compliant path for {subject} ({detail}); {USER_ACTION}")

    @property
    def reasons(self) -> tuple[BudgetReason, ...]:
        return tuple(q.rejection.reason for q in self.rejected if q.rejection is not None)


@dataclass(frozen=True, slots=True)
class BudgetedPaths:
    paths: tuple[PathQuote, ...]
    """Admissible paths, best first. Nothing has been reserved."""
    rejected: tuple[PathQuote, ...]
    """Paths removed by budget rules, with their reasons, for logging and user messages."""

    def first(self, subject: str = "this work") -> PathQuote:
        """The preferred admissible path; ``BudgetBlocked`` when there is none."""
        if not self.paths:
            raise BudgetBlocked(self.rejected, subject)
        return self.paths[0]


def quote_path(
    model_id: str,
    path: ModelPath,
    capability_rank: int,
    bounds: CallBounds,
    estimator: CostEstimator,
    limits: Limits,
    usage: SpendUsage,
) -> PathQuote:
    """A direct-call path's verdict. Unpaid access is always $0; a paid path needs a
    conservative quote that fits every limit given ``usage``."""
    call = bounds.call(path)
    if not call.paid:
        return PathQuote(model_id, path, capability_rank, call, None)
    estimate = None
    if call.input_tokens is not None and call.max_output_tokens is not None:
        estimate = estimator.estimate(call)
    rejection = check_estimate(call, estimate)
    if rejection is None and estimate is not None:
        rejection = check_limits(limits, usage, estimate.usd_micros)
    return PathQuote(model_id, path, capability_rank, call, estimate, rejection)


def quote_harness_path(
    model_id: str,
    path: ModelPath,
    capability_rank: int,
    bounds: CallBounds,
    enforceable_bound_micros: int | None,
    limits: Limits,
    usage: SpendUsage,
    bound_version: str = "1",
) -> PathQuote:
    """An opaque-harness path's verdict.

    A harness may make internal model calls Agent CLI cannot see, so per-call token pricing does
    not apply. A paid harness path is admissible only when its integration enforces a total cost
    bound that can be reserved before launch; otherwise it is ``paid_harness_unenforceable``.
    """
    call = bounds.call(path)
    if not call.paid:
        return PathQuote(model_id, path, capability_rank, call, None)
    if enforceable_bound_micros is None:
        unenforceable = Rejection(
            BudgetReason.PAID_HARNESS_UNENFORCEABLE,
            f"the harness cannot enforce a spend bound for {path.model_id} via "
            f"{path.provider.id}, so its paid calls cannot be held to hard limits",
        )
        return PathQuote(model_id, path, capability_rank, call, None, unenforceable)
    estimate = CostEstimate(
        usd_micros=enforceable_bound_micros,
        input_tokens=call.input_tokens or 0,
        output_tokens=call.max_output_tokens or 0,
        source=HARNESS_BOUND_SOURCE,
        source_version=bound_version,
        conservative=True,
    )
    rejection = check_limits(limits, usage, enforceable_bound_micros)
    return PathQuote(model_id, path, capability_rank, call, estimate, rejection)


def order_key(quote: PathQuote, preference: Sequence[AccessKind]) -> tuple[int, int, int, str, str]:
    access = quote.call.access
    access_rank = preference.index(access) if access in preference else len(preference)
    # Unpaid paths all cost 0, so they fall through to capability rank and provider ID.
    return (
        access_rank,
        quote.cost_micros,
        quote.capability_rank,
        quote.path.provider.id,
        quote.model_id,
    )


def rank_quotes(quotes: Iterable[PathQuote], preference: Sequence[AccessKind]) -> BudgetedPaths:
    """Split ``quotes`` into admissible paths (ordered) and rejected ones (input order)."""
    quotes = tuple(quotes)
    allowed = sorted((q for q in quotes if q.admissible), key=lambda q: order_key(q, preference))
    return BudgetedPaths(tuple(allowed), tuple(q for q in quotes if not q.admissible))


def budget_candidates(
    candidates: Sequence[ModelCandidate],
    bounds: CallBounds,
    estimator: CostEstimator,
    limits: Limits,
    usage: SpendUsage,
    preference: Sequence[AccessKind],
) -> BudgetedPaths:
    """Every path of every V1-07 candidate, filtered by budget and ordered by access.

    ``candidates`` are already capable and access-filtered; their order is their capability
    rank. Nothing is reserved.
    """
    quotes = [
        quote_path(candidate.model_id, path, rank, bounds, estimator, limits, usage)
        for rank, candidate in enumerate(candidates)
        for path in candidate.paths
    ]
    return rank_quotes(quotes, preference)


def budget_pinned(
    pinned: PinnedPaths,
    bounds: CallBounds,
    estimator: CostEstimator,
    limits: Limits,
    usage: SpendUsage,
    preference: Sequence[AccessKind],
    provider_id: str | None = None,
) -> BudgetedPaths:
    """The pinned model's usable paths (from ``pinned_paths``), filtered by budget and ordered
    by access. ``provider_id`` restricts them to a pinned provider.

    A pin never substitutes another model, and never bypasses a hard limit: when no path for
    the pinned model fits, ``first()`` raises ``BudgetBlocked``. Raises ``NoModelAvailable``
    when the pinned provider does not serve the model.
    """
    paths = pinned.paths
    if provider_id is not None:
        paths = tuple(path for path in paths if path.provider.id == provider_id)
        if not paths:
            raise NoModelAvailable(
                f"pinned model {pinned.model_id!r} ({pinned.pin.scope}) is not served by pinned "
                f"provider {provider_id!r}; ask the user how to proceed",
                pinned.pin,
            )
    quotes = [
        quote_path(pinned.model_id, path, 0, bounds, estimator, limits, usage) for path in paths
    ]
    return rank_quotes(quotes, preference)

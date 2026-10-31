"""Budget vocabulary: integer micro-dollars, effective limits, paid-call intents, estimates, and
stable rejection reasons.

Ledger arithmetic never uses binary floating point. Dollar values from configuration or provider
usage are converted through their decimal string, then compared as integer ``usd_micros``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from enum import StrEnum

from agent_cli.config.model import CostConfig
from agent_cli.terms import AccessKind

MICROS_PER_USD = 1_000_000
"""1 USD = 1,000,000 ``usd_micros``."""

PAID_ACCESS: AccessKind = "api"
"""The only access kind charged to the V1 paid-API ledger. Subscription and local cost $0."""


class BudgetError(Exception):
    """The spend ledger could not be read or written. It was not discarded."""


def to_micros(usd: float | int | str | Decimal, *, round_up: bool) -> int:
    """Convert dollars to ``usd_micros`` using the value's decimal string, never its binary form.

    Estimates and charges round up so they stay conservative; limits round down so they never
    grow. Raises ``ValueError`` for negative or non-finite values.
    """
    try:
        value = Decimal(str(usd))
    except InvalidOperation as error:
        raise ValueError(f"not a dollar amount: {usd!r}") from error
    if not value.is_finite() or value < 0:
        raise ValueError(f"not a non-negative dollar amount: {usd!r}")
    rounding = ROUND_CEILING if round_up else ROUND_FLOOR
    return int((value * MICROS_PER_USD).to_integral_value(rounding=rounding))


def format_usd(micros: int) -> str:
    """``$1.50`` for whole cents, otherwise every significant micro-dollar digit (``$0.0042``)."""
    whole, fraction = divmod(micros, MICROS_PER_USD)
    if fraction % 10_000 == 0:
        return f"${whole}.{fraction // 10_000:02d}"
    return f"${whole}.{fraction:06d}".rstrip("0")


def format_limit(micros: int | None) -> str:
    return "none" if micros is None else format_usd(micros)


class BudgetReason(StrEnum):
    """Machine-readable reasons a paid path is not admitted. Values are stable: they are
    persisted in the ledger and may be matched by callers."""

    PER_CALL_LIMIT = "per_call_limit"
    PER_TASK_LIMIT = "per_task_limit"
    DAILY_LIMIT = "daily_limit"
    MONTHLY_LIMIT = "monthly_limit"
    PRICING_UNKNOWN = "pricing_unknown"
    UNBOUNDED_OUTPUT = "unbounded_output"
    PAID_HARNESS_UNENFORCEABLE = "paid_harness_unenforceable"


USER_ACTION = (
    "choose a subscription or local path, reduce the call's scope or output bound, "
    "or change the budget configuration and retry"
)
"""What the user can do about a rejection. A plain approval never bypasses a hard limit."""


@dataclass(frozen=True, slots=True)
class Rejection:
    reason: BudgetReason
    message: str
    """A human-readable explanation; ``reason`` is what policy and callers act on."""

    def __str__(self) -> str:
        return f"{self.reason}: {self.message}"


class BudgetRejected(Exception):
    """A paid call was refused by a hard budget rule. Nothing was dispatched or charged."""

    def __init__(self, rejection: Rejection) -> None:
        self.rejection = rejection
        super().__init__(f"{rejection}; {USER_ACTION}")

    @property
    def reason(self) -> BudgetReason:
        return self.rejection.reason


@dataclass(frozen=True, slots=True)
class Limits:
    """Effective hard limits in ``usd_micros``; ``None`` means no limit."""

    monthly: int | None = None
    daily: int | None = None
    per_task: int | None = None
    per_call: int | None = None

    @classmethod
    def from_config(cls, cost: CostConfig) -> Limits:
        def micros(usd: float | None) -> int | None:
            return None if usd is None else to_micros(usd, round_up=False)

        return cls(
            micros(cost.monthly_limit_usd),
            micros(cost.daily_limit_usd),
            micros(cost.per_task_limit_usd),
            micros(cost.per_call_limit_usd),
        )


@dataclass(frozen=True, slots=True)
class PaidCall:
    """One intended paid call, described before anything is estimated or dispatched.

    Never holds prompt text or credentials: only bounds and identifiers.
    """

    provider_id: str
    access: AccessKind
    model_id: str
    purpose: str
    """Why the call is made, such as ``classification`` or ``implementer``."""
    input_tokens: int | None
    """A conservative upper bound on input tokens; ``None`` when it cannot be bounded."""
    max_output_tokens: int | None
    """An enforceable output bound; ``None`` means output is unbounded."""
    root_task_id: str | None = None
    """The root user task whose per-task limit applies; ``None`` for taskless calls."""
    task_id: str | None = None
    """The (sub)task making the call, for diagnosis."""
    session_id: str | None = None

    @property
    def paid(self) -> bool:
        return self.access == PAID_ACCESS


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """A conservative upper bound on what one call can cost."""

    usd_micros: int
    input_tokens: int
    output_tokens: int
    source: str
    """The pricing or estimator identity, such as a price table name."""
    source_version: str
    conservative: bool
    """The estimator's assertion that ``usd_micros`` cannot be exceeded. Only conservative
    estimates admit a paid call."""


class ReservationStatus(StrEnum):
    RESERVED = "reserved"
    """Admitted, not yet dispatched."""
    DISPATCHED = "dispatched"
    """Execution may have happened; still consumes headroom until settled."""
    SETTLED = "settled"
    RELEASED = "released"
    """Released before dispatch: provably never ran."""

    @property
    def active(self) -> bool:
        return self in (ReservationStatus.RESERVED, ReservationStatus.DISPATCHED)


class SettlementSource(StrEnum):
    """Where a settled charge came from, best first."""

    PROVIDER_COST = "provider_cost"
    TOKEN_PRICING = "token_pricing"
    RESERVED_BOUND = "reserved_bound"
    """Actual usage was unknown, so the full reserved upper bound is charged."""


@dataclass(frozen=True, slots=True)
class Reservation:
    id: str
    call: PaidCall
    estimate: CostEstimate
    status: ReservationStatus
    reserved_micros: int
    settled_micros: int | None
    settlement_source: SettlementSource | None
    day: str
    """UTC date the reservation counts toward (``YYYY-MM-DD``)."""
    month: str
    """UTC month the reservation counts toward (``YYYY-MM``)."""
    created_at: str
    updated_at: str
    reference: str | None = None
    note: str = ""
    """Why it was released or how it was settled."""


@dataclass(frozen=True, slots=True)
class Totals:
    """Settled spend and active (reserved or dispatched) amounts in one budget scope."""

    spent: int = 0
    reserved: int = 0

    @property
    def committed(self) -> int:
        return self.spent + self.reserved

    def remaining(self, limit: int | None) -> int | None:
        return None if limit is None else max(limit - self.committed, 0)


@dataclass(frozen=True, slots=True)
class SpendUsage:
    """Global spend for the current UTC month and day, and for one root task when given."""

    month: Totals
    day: Totals
    task: Totals | None = None
    active: int = 0
    """Active reservations, globally."""
    dispatched: int = 0
    """Of those, dispatched ones awaiting settlement: they may have run."""
    oldest_dispatched_at: str | None = None
    task_ceiling: TaskCeiling | None = None
    """The root task's recorded per-task ceiling, once it has admitted a paid call."""


@dataclass(frozen=True, slots=True)
class TaskCeiling:
    """The per-task limit a root task was first admitted under, tightened by any later, lower
    limit. Every subtask and process checks this same ceiling, so config built for one subtask
    can never raise the budget of the root task it belongs to."""

    limit: int | None
    """``None`` when the root task was admitted with no per-task limit."""


def tighter(first: int | None, second: int | None) -> int | None:
    """The stricter of two limits, where ``None`` means no limit."""
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)

"""Deterministic paid-API budget guardrails and access-preference routing (V1-08).

Pricing estimates cost; the ledger owns reservations and settlement; policy checks limits;
routing filters and orders candidate paths without reserving; the guard reserves immediately
before a real paid invocation. See docs/ROUTING.md#cost-guardrails.
"""

from agent_cli.budget.domain import (
    MICROS_PER_USD,
    USER_ACTION,
    BudgetError,
    BudgetReason,
    BudgetRejected,
    CostEstimate,
    Limits,
    PaidCall,
    Rejection,
    Reservation,
    ReservationStatus,
    SettlementSource,
    SpendUsage,
    Totals,
    format_usd,
    to_micros,
)
from agent_cli.budget.guard import BudgetGuard
from agent_cli.budget.ledger import Admission, SpendLedger, settlement_charge
from agent_cli.budget.policy import check_estimate, check_limits
from agent_cli.budget.pricing import (
    NO_PRICING,
    CostEstimator,
    Price,
    PriceTable,
    input_bound,
)
from agent_cli.budget.routing import (
    BudgetBlocked,
    BudgetedPaths,
    CallBounds,
    PathQuote,
    budget_candidates,
    budget_pinned,
    quote_harness_path,
    quote_path,
    rank_quotes,
)
from agent_cli.budget.status import budget_lines

__all__ = [
    "MICROS_PER_USD",
    "NO_PRICING",
    "USER_ACTION",
    "Admission",
    "BudgetBlocked",
    "BudgetError",
    "BudgetGuard",
    "BudgetReason",
    "BudgetRejected",
    "BudgetedPaths",
    "CallBounds",
    "CostEstimate",
    "CostEstimator",
    "Limits",
    "PaidCall",
    "PathQuote",
    "Price",
    "PriceTable",
    "Rejection",
    "Reservation",
    "ReservationStatus",
    "SettlementSource",
    "SpendLedger",
    "SpendUsage",
    "Totals",
    "budget_candidates",
    "budget_lines",
    "budget_pinned",
    "check_estimate",
    "check_limits",
    "format_usd",
    "input_bound",
    "quote_harness_path",
    "quote_path",
    "rank_quotes",
    "settlement_charge",
    "to_micros",
]

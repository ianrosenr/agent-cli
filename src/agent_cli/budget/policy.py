"""Hard-limit admission: deterministic integer checks, nothing a model or approval can override.

A call fits when its conservative estimate, added to settled spend and active reservations in
every scope, stays at or under each limit. Exactly reaching a limit is allowed.
"""

from __future__ import annotations

from dataclasses import replace

from agent_cli.budget.domain import (
    BudgetReason,
    CostEstimate,
    Limits,
    PaidCall,
    Rejection,
    SpendUsage,
    Totals,
    format_usd,
    tighter,
)


def effective_limits(limits: Limits, usage: SpendUsage) -> Limits:
    """``limits`` with the per-task limit tightened to the root task's recorded ceiling."""
    if usage.task_ceiling is None:
        return limits
    return replace(limits, per_task=tighter(limits.per_task, usage.task_ceiling.limit))


def check_limits(limits: Limits, usage: SpendUsage, estimate: int) -> Rejection | None:
    """The first limit ``estimate`` would break, narrowest scope first; ``None`` if it fits."""
    limits = effective_limits(limits, usage)
    if limits.per_call is not None and estimate > limits.per_call:
        return Rejection(
            BudgetReason.PER_CALL_LIMIT,
            f"estimated {format_usd(estimate)} is above the per-call limit of "
            f"{format_usd(limits.per_call)}",
        )
    scopes: tuple[tuple[BudgetReason, str, Totals | None, int | None], ...] = (
        (BudgetReason.PER_TASK_LIMIT, "root task", usage.task, limits.per_task),
        (BudgetReason.DAILY_LIMIT, "UTC day", usage.day, limits.daily),
        (BudgetReason.MONTHLY_LIMIT, "UTC month", usage.month, limits.monthly),
    )
    for reason, scope, totals, limit in scopes:
        if totals is None or limit is None or totals.committed + estimate <= limit:
            continue
        return Rejection(
            reason,
            f"estimated {format_usd(estimate)} would exceed the {scope} limit of "
            f"{format_usd(limit)} (spent {format_usd(totals.spent)}, reserved "
            f"{format_usd(totals.reserved)}, remaining {format_usd(totals.remaining(limit) or 0)})",
        )
    return None


def check_estimate(call: PaidCall, estimate: CostEstimate | None) -> Rejection | None:
    """Whether ``estimate`` can admit ``call`` at all: a paid call needs a bounded output and a
    conservative upper-bound price."""
    if call.max_output_tokens is None:
        return Rejection(
            BudgetReason.UNBOUNDED_OUTPUT,
            f"{call.model_id} via {call.provider_id} has no output bound, so its cost cannot be "
            "bounded",
        )
    if call.input_tokens is None:
        return Rejection(
            BudgetReason.PRICING_UNKNOWN,
            f"{call.model_id} via {call.provider_id} has no input bound, so its cost cannot be "
            "bounded",
        )
    if estimate is None:
        return Rejection(
            BudgetReason.PRICING_UNKNOWN,
            f"no trusted pricing for {call.model_id} via {call.provider_id}",
        )
    if not estimate.conservative:
        return Rejection(
            BudgetReason.PRICING_UNKNOWN,
            f"pricing for {call.model_id} via {call.provider_id} ({estimate.source} "
            f"{estimate.source_version}) is not a conservative upper bound",
        )
    return None

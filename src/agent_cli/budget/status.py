"""A concise view of paid-API spend and hard limits for ``/budget``.

``spent`` is settled charges, ``reserved`` is active (reserved or dispatched) amounts, and
``remaining`` is ``max(limit - spent - reserved, 0)``. Never dumps the ledger.
"""

from __future__ import annotations

from agent_cli.budget.domain import (
    BudgetError,
    Limits,
    Totals,
    format_limit,
    format_usd,
)
from agent_cli.budget.ledger import SpendLedger
from agent_cli.budget.policy import effective_limits
from agent_cli.config import Config, ConfigError, ConfigService
from agent_cli.state import Task, TaskStateError, TaskStore

FOOTER = "Subscription and local access do not count toward these limits."


def budget_lines(
    config: ConfigService | None,
    tasks: TaskStore | None,
    ledger: SpendLedger | None = None,
) -> list[str]:
    try:
        settings = Config() if config is None else config.resolve()
    except ConfigError as error:
        return [f"Cannot read configuration: {error}"]
    limits = Limits.from_config(settings.cost)
    try:
        task = active_root_task(tasks)
    except TaskStateError as error:
        return [f"Cannot read task state: {error}"]
    ledger = SpendLedger.default() if ledger is None else ledger
    try:
        usage = ledger.usage(None if task is None else task.id)
    except BudgetError as error:
        return [f"Cannot read spend ledger: {error}", "No paid API calls will be admitted."]
    limits = effective_limits(limits, usage)

    rows = [("Month (UTC):", usage.month, limits.monthly), ("Day (UTC):", usage.day, limits.daily)]
    if task is not None and usage.task is not None:
        rows.append((f"Task {task.id}:", usage.task, limits.per_task))
    width = max(len(label) for label, _, _ in rows)
    lines = ["Paid API budget"]
    lines += [f"{label:<{width}} {scope_line(totals, limit)}" for label, totals, limit in rows]
    lines.append(f"{'Per call:':<{width}} limit {format_limit(limits.per_call)}")
    if task is None:
        lines.append(
            f"{'Per task:':<{width}} limit {format_limit(limits.per_task)} (no active task)"
        )
    lines.append(f"Active reservations: {usage.active}")
    if usage.dispatched:
        lines.append(
            f"Unresolved dispatched calls: {usage.dispatched} "
            f"(oldest {usage.oldest_dispatched_at}); each counts at its full reserved amount "
            "until settled"
        )
    lines += ["", FOOTER]
    return lines


def scope_line(totals: Totals, limit: int | None) -> str:
    parts = [
        f"spent {format_usd(totals.spent)}",
        f"reserved {format_usd(totals.reserved)}",
        f"limit {format_limit(limit)}",
    ]
    remaining = totals.remaining(limit)
    if remaining is not None:
        parts.append(f"remaining {format_usd(remaining)}")
    return " · ".join(parts)


def active_root_task(tasks: TaskStore | None) -> Task | None:
    """The newest unfinished root task in the open session, if any. Never creates state."""
    if tasks is None or not tasks.exists():
        return None
    session = tasks.latest_session()
    if session is None or not session.open:
        return None
    roots = [
        task
        for task in tasks.tasks(session.id)
        if task.parent_id is None and not task.status.finished
    ]
    return roots[-1] if roots else None

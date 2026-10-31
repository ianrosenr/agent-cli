"""A concise view of paid-API spend and hard limits for ``/budget``.

``spent`` is settled charges, ``reserved`` is active (reserved or dispatched) amounts, and
``remaining`` is ``max(limit - spent - reserved, 0)``. Never dumps the ledger.
"""

from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text

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
from agent_cli.ui import blocks, error, fields, heading, muted, table, warning

HEADERS = ("Scope", "Spent", "Reserved", "Limit", "Remaining")
FOOTER = "Subscription and local access do not count toward these limits."


def budget_view(
    config: ConfigService | None,
    tasks: TaskStore | None,
    ledger: SpendLedger | None = None,
) -> RenderableType:
    try:
        settings = Config() if config is None else config.resolve()
    except ConfigError as problem:
        return error("Cannot read configuration.", str(problem))
    limits = Limits.from_config(settings.cost)
    try:
        task = active_root_task(tasks)
    except TaskStateError as problem:
        return error("Cannot read task state.", str(problem))
    ledger = SpendLedger.default() if ledger is None else ledger
    try:
        usage = ledger.usage(None if task is None else task.id)
    except BudgetError as problem:
        return error(
            "Cannot read spend ledger.", str(problem), "No paid API calls will be admitted."
        )
    limits = effective_limits(limits, usage)

    rows = [
        scope_row("Month (UTC)", usage.month, limits.monthly),
        scope_row("Day (UTC)", usage.day, limits.daily),
    ]
    if task is not None and usage.task is not None:
        rows.append(scope_row(f"Task {task.id}", usage.task, limits.per_task))
    else:
        rows.append(limit_row("Per task", limits.per_task, "no active task"))
    rows.append(limit_row("Per call", limits.per_call))
    unresolved = (
        warning(
            f"Unresolved dispatched calls: {usage.dispatched}.",
            f"Oldest dispatched at {usage.oldest_dispatched_at}; each counts at its full "
            "reserved amount until settled.",
        )
        if usage.dispatched
        else None
    )
    return blocks(
        heading("Paid API budget"),
        table(HEADERS, rows, styles=(None, "money", "money"), right={1, 2, 3, 4}),
        fields([("Active reservations", str(usage.active))], indent=1),
        unresolved,
        muted(FOOTER),
    )


def scope_row(label: str, totals: Totals, limit: int | None) -> tuple[str | Text, ...]:
    remaining = totals.remaining(limit)
    return (
        label,
        format_usd(totals.spent),
        format_usd(totals.reserved),
        format_limit(limit),
        "—" if remaining is None else Text(format_usd(remaining), style="money"),
    )


def limit_row(label: str, limit: int | None, note: str = "") -> tuple[str | Text, ...]:
    return (label, "—", "—", format_limit(limit), Text(note, style="muted"))


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

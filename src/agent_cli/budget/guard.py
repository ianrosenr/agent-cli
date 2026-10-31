"""The paid-call boundary: reserve immediately before a direct provider call, settle after.

::

    estimate -> reserve atomically -> mark dispatched -> invoke -> settle

Only this boundary reserves spend. Subscription and local providers pass straight through at $0.
A provider whose access kind is unknown is treated as paid, so it fails closed without trusted
pricing.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field, replace

from agent_cli.adapters import ProviderAdapter, ProviderRequest, ProviderResponse
from agent_cli.budget.domain import (
    PAID_ACCESS,
    BudgetError,
    BudgetRejected,
    Limits,
    PaidCall,
    Rejection,
    Reservation,
    format_usd,
)
from agent_cli.budget.ledger import SpendLedger
from agent_cli.budget.policy import check_estimate
from agent_cli.budget.pricing import NO_PRICING, CostEstimator
from agent_cli.config import Config
from agent_cli.state import TaskStateError, TaskStore
from agent_cli.terms import AccessKind

UNPAID_ACCESS: frozenset[AccessKind | None] = frozenset({"subscription", "local"})


@dataclass(slots=True)
class BudgetGuard:
    """Enforces hard paid-API limits around direct provider calls.

    Build it from the effective configuration for the scope of the work, including any session
    or task overrides, which can only tighten limits.
    """

    ledger: SpendLedger
    limits: Limits
    estimator: CostEstimator = NO_PRICING
    tasks: TaskStore | None = None
    """Resolves root tasks and receives concise budget events; required for task-scoped calls."""
    unlinked: list[str] = field(default_factory=list)
    """Budget events that could not be linked to task state. The ledger still holds them."""

    @classmethod
    def from_config(
        cls,
        config: Config,
        ledger: SpendLedger | None = None,
        estimator: CostEstimator = NO_PRICING,
        tasks: TaskStore | None = None,
    ) -> BudgetGuard:
        return cls(
            SpendLedger.default() if ledger is None else ledger,
            Limits.from_config(config.cost),
            estimator,
            tasks,
        )

    def paid_call(
        self,
        adapter: ProviderAdapter,
        request: ProviderRequest,
        purpose: str,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> PaidCall:
        """Describe ``request`` for budgeting, with its root-task scope resolved."""
        identity = adapter.identity()
        root = None
        if task_id is not None:
            if self.tasks is None:
                raise ValueError("task-scoped paid calls need task state to find the root task")
            root = self.tasks.root_task(task_id)
        call = PaidCall(
            provider_id=identity.id,
            access=identity.access or PAID_ACCESS,
            model_id=request.model_id,
            purpose=purpose,
            input_tokens=None,
            max_output_tokens=request.max_output_tokens,
            root_task_id=root,
            task_id=task_id,
            session_id=session_id,
        )
        return replace(call, input_tokens=self.estimator.input_bound(call, request))

    def invoke_classifier(
        self, adapter: ProviderAdapter, request: ProviderRequest, task_ids: tuple[str, ...]
    ) -> ProviderResponse:
        """Budget one classifier batch without mixing root-task spend scopes."""
        task_id: str | None = None
        if task_ids:
            if self.tasks is None:
                raise BudgetError("paid classifier tasks need task state for budget scoping")
            roots = {self.tasks.root_task(candidate) for candidate in task_ids}
            if len(roots) != 1:
                raise BudgetError(
                    "paid classifier batch spans multiple root-task budgets; split it before "
                    "invocation"
                )
            task_id = task_ids[0]
        return self.invoke(adapter, request, "skill_classification", task_id=task_id)

    def invoke(
        self,
        adapter: ProviderAdapter,
        request: ProviderRequest,
        purpose: str,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> ProviderResponse:
        """Call ``adapter`` within the hard limits.

        Raises ``BudgetRejected`` before dispatch when a paid call has no output bound, no
        trusted price, or does not fit a limit; nothing is charged then. Provider errors
        propagate after the call is settled at its full reserved bound, because whether it
        consumed tokens is unknown.
        """
        if adapter.identity().access in UNPAID_ACCESS:
            # Never budgeted, so neither the ledger nor task state is needed.
            return adapter.invoke(request)
        call = self.paid_call(adapter, request, purpose, task_id, session_id)

        estimate = None
        if call.max_output_tokens is not None:
            estimate = self.estimator.estimate(call)
        rejection = check_estimate(call, estimate)
        if rejection is not None:
            self.ledger.record_rejection(call, rejection, self.limits, estimate)
            self._rejected(call, rejection)
            raise BudgetRejected(rejection)
        assert estimate is not None

        try:
            reservation = self.ledger.reserve(call, estimate, self.limits)
        except BudgetRejected as error:
            self._rejected(call, error.rejection)
            raise
        self._dispatch(reservation)
        try:
            response = adapter.invoke(request)
        except BaseException as error:
            # If settling fails too, it stays dispatched and counting; /budget reports it.
            with suppress(BudgetError):
                self._settle(reservation, None, f"failed after dispatch: {type(error).__name__}")
            raise
        self._settle(reservation, response, "")
        return response

    def _dispatch(self, reservation: Reservation) -> None:
        try:
            self.ledger.mark_dispatched(reservation.id)
        except BaseException:
            # Not recorded as dispatched, and the provider was never called: safe to release.
            # If releasing fails too, it stays reserved and counting: the safe failure.
            with suppress(BudgetError):
                self.ledger.release(reservation.id, "dispatch could not be recorded")
            raise

    def _settle(
        self, reservation: Reservation, response: ProviderResponse | None, note: str
    ) -> None:
        usage = None if response is None else response.usage
        settled = self.ledger.settle(reservation.id, usage, self.estimator, note)
        call = reservation.call
        if call.task_id is not None:
            self._link(
                call.task_id,
                "settled",
                settled.id,
                provider=call.provider_id,
                model=call.model_id,
                reserved=format_usd(settled.reserved_micros),
                charged=format_usd(settled.settled_micros or 0),
                source=str(settled.settlement_source),
            )

    def _rejected(self, call: PaidCall, rejection: Rejection) -> None:
        if call.task_id is not None:
            self._link(
                call.task_id,
                "rejected",
                call.root_task_id or call.task_id,
                provider=call.provider_id,
                model=call.model_id,
                reason=str(rejection.reason),
                message=rejection.message,
            )

    def _link(self, task_id: str, kind: str, subject_id: str, **detail: str) -> None:
        assert self.tasks is not None
        try:
            self.tasks.record_budget_event(task_id, kind, subject_id, **detail)
        except TaskStateError as error:
            # Task events are a convenience link; never let them mask the call's outcome.
            self.unlinked.append(f"{kind} {subject_id}: {error}")

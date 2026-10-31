from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_cli.adapters import (
    AdapterError,
    AdapterRegistry,
    ErrorKind,
    ModelDescriptor,
    ProviderIdentity,
    ProviderRequest,
    Usage,
)
from agent_cli.adapters.fake import FakeProviderAdapter
from agent_cli.budget import (
    NO_PRICING,
    USER_ACTION,
    BudgetBlocked,
    BudgetError,
    BudgetGuard,
    BudgetReason,
    BudgetRejected,
    CallBounds,
    CostEstimate,
    Limits,
    PaidCall,
    Price,
    PriceTable,
    ReservationStatus,
    SettlementSource,
    SpendLedger,
    SpendUsage,
    Totals,
    budget_candidates,
    budget_lines,
    budget_pinned,
    check_limits,
    format_usd,
    input_bound,
    quote_harness_path,
    to_micros,
)
from agent_cli.budget.domain import TaskCeiling
from agent_cli.budget.ledger import SPEND_FILE
from agent_cli.commands import CommandContext, build_registry
from agent_cli.config import Config, ConfigPaths, ConfigService
from agent_cli.config.model import CostConfig, RoleFloors
from agent_cli.models import (
    ClassifierChoice,
    ModelPath,
    ModelPin,
    NoModelAvailable,
    PinnedPaths,
    build_classifier,
    pinned_paths,
    ranked_model_candidates,
    runtime_paths,
)
from agent_cli.skills import ClassificationInput, ClassifierMode, load_policy
from agent_cli.skills.classifier import classify
from agent_cli.state import TaskStateError, TaskStatus, TaskStore
from agent_cli.terms import AccessKind
from test_models import model, registry

# Fixture prices: deterministic test data, not real provider pricing.
PREFERENCE: tuple[AccessKind, ...] = ("subscription", "local", "api")
USD = 1_000_000
CENT = 10_000
PRICES = PriceTable.of(
    "fixture-prices",
    "test-1",
    [
        # $100 per million tokens each way: 1,000 in + 1,000 out costs $0.20.
        ("paid", "m", Price(100 * USD, 100 * USD, 1, 256)),
        ("paid", "best", Price(100 * USD, 100 * USD, 1, 256)),
        ("paid", "cheap", Price(10 * USD, 10 * USD, 1, 256)),
        ("pricey", "best", Price(200 * USD, 200 * USD, 1, 256)),
    ],
)


@dataclass
class Clock:
    now: datetime = field(default_factory=lambda: datetime(2026, 10, 15, 12, 0, tzinfo=UTC))

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def ledger(tmp_path: Path, clock: Clock) -> SpendLedger:
    return SpendLedger(tmp_path / "home" / ".agent-cli" / "state" / SPEND_FILE, clock=clock)


@pytest.fixture
def tasks(tmp_path: Path) -> TaskStore:
    return TaskStore.for_repository(tmp_path / "repo")


def call(root_task_id: str | None = None, **overrides: Any) -> PaidCall:
    fields: dict[str, Any] = {
        "provider_id": "paid",
        "access": "api",
        "model_id": "m",
        "purpose": "test",
        "input_tokens": 1000,
        "max_output_tokens": 1000,
        "root_task_id": root_task_id,
    }
    fields.update(overrides)
    return PaidCall(**fields)


def estimate(micros: int) -> CostEstimate:
    return CostEstimate(micros, 1000, 1000, "fixture-prices", "test-1", conservative=True)


def limits(**overrides: int | None) -> Limits:
    values: dict[str, int | None] = {
        "monthly": None,
        "daily": None,
        "per_task": None,
        "per_call": None,
    }
    values.update(overrides)
    return Limits(**values)


def provider(
    id: str,
    access: AccessKind | None,
    *model_ids: str,
    usage: Usage | None = None,
    reply: str = '{"results": []}',
) -> FakeProviderAdapter:
    return FakeProviderAdapter(
        provider_identity=ProviderIdentity(id, id.title(), "fake", access),
        models=[ModelDescriptor(id, model_id) for model_id in model_ids],
        usage=usage,
        reply=reply,
    )


CALL_COST = 126_600
"""``request()`` priced by ``PRICES``: 10 prompt bytes + 256 overhead in, 1,000 out, $100/Mtok."""


def request(model_id: str = "m", max_output_tokens: int | None = 1000) -> ProviderRequest:
    return ProviderRequest(model_id, "x" * 10, max_output_tokens=max_output_tokens)


def guard(ledger: SpendLedger, tasks: TaskStore | None = None, **limit: int | None) -> BudgetGuard:
    return BudgetGuard(ledger, limits(**limit), PRICES, tasks)


def path(provider_id: str, access: AccessKind, model_id: str) -> ModelPath:
    return ModelPath(model_id, ProviderIdentity(provider_id, provider_id, "fake", access))


def bounds(**overrides: Any) -> CallBounds:
    values: dict[str, Any] = {"purpose": "implementer", "input_tokens": 1000}
    values["max_output_tokens"] = 1000
    values.update(overrides)
    return CallBounds(**values)


def empty() -> SpendUsage:
    return SpendUsage(Totals(), Totals(), Totals())


# -- Money -------------------------------------------------------------------------------------


def test_dollars_convert_to_integer_micros_through_their_decimal_string() -> None:
    assert to_micros(0.3, round_up=False) == 300_000
    assert to_micros("20.00", round_up=False) == 20 * USD
    # 0.1 + 0.2 is 0.30000000000000004 in binary floating point.
    assert to_micros(0.1 + 0.2, round_up=False) == 300_000
    assert to_micros(0.1 + 0.2, round_up=True) == 300_001
    assert to_micros("0.0000001", round_up=True) == 1
    assert to_micros("0.0000001", round_up=False) == 0
    assert isinstance(to_micros(1.5, round_up=True), int)


@pytest.mark.parametrize("bad", [-0.01, float("nan"), float("inf"), "abc"])
def test_negative_or_non_finite_dollars_are_rejected(bad: object) -> None:
    with pytest.raises(ValueError):
        to_micros(bad, round_up=True)  # type: ignore[arg-type]


def test_default_config_limits_become_exact_micros() -> None:
    assert Limits.from_config(CostConfig()) == Limits(20 * USD, None, 1 * USD, 300_000)


def test_prices_round_up_to_the_next_micro_dollar() -> None:
    assert Price(1, 0).cost(1, 0) == 1  # 1e-6 of a micro-dollar still costs one.
    assert Price(3 * USD, 15 * USD).cost(1000, 1000) == 18_000
    assert PRICES.estimate(call()) == estimate(200_000)


def test_price_tables_match_ids_case_insensitively_and_are_read_only() -> None:
    table = PriceTable("fixture", "1", {("Paid", "Model-X"): Price(1, 1)})
    assert table.price("paid", "model-x") == Price(1, 1)
    with pytest.raises(TypeError):
        table.prices[("paid", "y")] = Price(1, 1)  # type: ignore[index]
    with pytest.raises(ValueError, match="duplicate price"):
        PriceTable("fixture", "1", {("paid", "m"): Price(1, 1), ("PAID", "M"): Price(2, 2)})
    with pytest.raises(ValueError, match="duplicate price"):
        PriceTable.of("fixture", "1", [("paid", "m", Price(1, 1)), ("paid", "m", Price(1, 1))])


def test_input_bound_is_at_least_one_token_per_byte() -> None:
    text = "héllo wörld"
    bound = input_bound(
        ProviderRequest("m", text, system="sys"),
        max_tokens_per_utf8_byte=1,
        request_overhead_tokens=256,
    )
    assert bound >= len(text.encode()) + len(b"sys")


def test_paid_guard_rejects_price_source_without_a_trusted_input_bound(
    ledger: SpendLedger,
) -> None:
    rates_only = PriceTable.of(
        "rates-only", "1", [("paid", "m", Price(100 * USD, 100 * USD))]
    )
    paid = provider("paid", "api", "m")

    with pytest.raises(BudgetRejected) as rejected:
        BudgetGuard(ledger, limits(), rates_only).invoke(paid, request(), "test")

    assert rejected.value.reason is BudgetReason.PRICING_UNKNOWN
    assert paid.requests == []

def test_amounts_are_formatted_exactly() -> None:
    assert format_usd(20 * USD) == "$20.00"
    assert format_usd(300_000) == "$0.30"
    assert format_usd(4_200) == "$0.0042"
    assert format_usd(1) == "$0.000001"


# -- Ledger ------------------------------------------------------------------------------------


def test_ledger_stores_money_only_as_integers(ledger: SpendLedger) -> None:
    reservation = ledger.reserve(call(), estimate(123_457), limits())
    ledger.mark_dispatched(reservation.id)
    ledger.settle(reservation.id, Usage(cost_usd=0.1 + 0.2))

    db = sqlite3.connect(ledger.path)
    columns = {row[1]: row[2] for row in db.execute("PRAGMA table_info(reservations)")}
    assert columns["reserved_micros"] == columns["settled_micros"] == "INTEGER"
    kinds = db.execute("SELECT typeof(reserved_micros), typeof(settled_micros) FROM reservations")
    assert kinds.fetchone() == ("integer", "integer")
    usage = ledger.usage()
    assert usage.month == Totals(spent=300_001, reserved=0)
    assert all(isinstance(value, int) for value in (usage.month.spent, usage.month.reserved))


def test_reading_usage_does_not_create_the_ledger(ledger: SpendLedger) -> None:
    assert ledger.usage("task-x") == SpendUsage(Totals(), Totals(), Totals())
    assert ledger.admissions() == []
    assert not ledger.path.exists()


def test_ledger_lives_in_the_global_state_directory(isolated_home: Path) -> None:
    assert SpendLedger.default().path == isolated_home / ".agent-cli" / "state" / "spend.sqlite"


def test_monthly_spend_is_shared_across_repositories() -> None:
    first = BudgetGuard.from_config(Config(), estimator=PRICES)
    second = BudgetGuard.from_config(Config(), estimator=PRICES)
    paid = provider("paid", "api", "m")

    first.invoke(paid, request(), "repo-a work")
    assert second.ledger.path == first.ledger.path
    assert second.ledger.usage().month.spent == CALL_COST


def test_utc_day_and_month_boundaries(ledger: SpendLedger, clock: Clock) -> None:
    clock.now = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)
    ledger.reserve(call("root"), estimate(100), limits())
    assert ledger.usage("root").day.reserved == ledger.usage().month.reserved == 100

    clock.now = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
    usage = ledger.usage("root")
    assert usage.day == usage.month == Totals()
    assert usage.task == Totals(reserved=100)  # The root-task scope does not reset.


def test_periods_use_utc_even_for_a_local_clock(ledger: SpendLedger, clock: Clock) -> None:
    # 01:00 on November 1 at UTC+2 is still October 31 in UTC.
    clock.now = datetime(2026, 11, 1, 1, 0, tzinfo=timezone(timedelta(hours=2)))
    reservation = ledger.reserve(call(), estimate(100), limits())
    assert (reservation.day, reservation.month) == ("2026-10-31", "2026-10")


def test_active_reservations_consume_headroom(ledger: SpendLedger) -> None:
    first = ledger.reserve(call(), estimate(600_000), limits(monthly=USD))
    with pytest.raises(BudgetRejected) as rejected:
        ledger.reserve(call(), estimate(500_000), limits(monthly=USD))
    assert rejected.value.reason is BudgetReason.MONTHLY_LIMIT

    ledger.mark_dispatched(first.id)
    with pytest.raises(BudgetRejected):
        ledger.reserve(call(), estimate(500_000), limits(monthly=USD))
    assert ledger.usage().month == Totals(spent=0, reserved=600_000)


def test_releasing_before_dispatch_restores_headroom(ledger: SpendLedger) -> None:
    first = ledger.reserve(call(), estimate(600_000), limits(monthly=USD))
    ledger.release(first.id, "never sent")

    ledger.reserve(call(), estimate(1 * USD), limits(monthly=USD))
    released = ledger.reservation(first.id)
    assert released is not None and released.status is ReservationStatus.RELEASED
    assert released.note == "never sent"


def test_dispatched_reservations_can_never_be_released(ledger: SpendLedger) -> None:
    reservation = ledger.reserve(call(), estimate(600_000), limits())
    ledger.mark_dispatched(reservation.id)

    with pytest.raises(BudgetError, match="may have run"):
        ledger.release(reservation.id, "timed out")
    assert ledger.usage().month.reserved == 600_000
    assert ledger.usage().dispatched == 1


def test_only_dispatched_reservations_can_be_settled(ledger: SpendLedger) -> None:
    reservation = ledger.reserve(call(), estimate(100), limits())
    with pytest.raises(BudgetError, match="never dispatched"):
        ledger.settle(reservation.id, None)
    with pytest.raises(BudgetError, match="unknown reservation"):
        ledger.mark_dispatched("spend-missing")


def test_concurrent_reservations_cannot_oversubscribe(ledger: SpendLedger, clock: Clock) -> None:
    ledger.usage()  # Create the schema before the race.
    ledger.reserve(call(), estimate(1), limits())
    start = threading.Barrier(20)

    def attempt(_: int) -> bool:
        # Separate connections, as separate Agent CLI processes would have.
        own = SpendLedger(ledger.path, clock=clock)
        start.wait()
        try:
            own.reserve(call(), estimate(100_000), limits(monthly=1 * USD + 1))
        except BudgetRejected:
            return False
        finally:
            own.close()
        return True

    with ThreadPoolExecutor(max_workers=20) as pool:
        admitted = sum(pool.map(attempt, range(20)))

    assert admitted == 10
    assert ledger.usage().month.reserved == 1 * USD + 1


def test_unusable_ledger_fails_closed_and_is_kept(ledger: SpendLedger) -> None:
    ledger.path.parent.mkdir(parents=True)
    ledger.path.write_bytes(b"not a database at all, just bytes" * 100)
    paid = provider("paid", "api", "m")

    with pytest.raises(BudgetError):
        guard(ledger).invoke(paid, request(), "test")
    assert paid.requests == []
    assert ledger.path.read_bytes().startswith(b"not a database")


def test_newer_ledger_schema_is_refused(ledger: SpendLedger) -> None:
    ledger.usage()
    ledger.reserve(call(), estimate(1), limits())
    ledger.close()
    db = sqlite3.connect(ledger.path)
    db.execute("PRAGMA user_version = 99")
    db.close()

    with pytest.raises(BudgetError, match="newer"):
        SpendLedger(ledger.path).usage()


# -- Limits ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "usage", "reason"),
    [
        ({"per_call": 200_000}, empty(), BudgetReason.PER_CALL_LIMIT),
        (
            {"per_task": 500_000},
            SpendUsage(Totals(), Totals(), Totals(spent=200_000, reserved=100_000)),
            BudgetReason.PER_TASK_LIMIT,
        ),
        (
            {"daily": 500_000},
            SpendUsage(Totals(), Totals(spent=300_000), Totals()),
            BudgetReason.DAILY_LIMIT,
        ),
        (
            {"monthly": 500_000},
            SpendUsage(Totals(reserved=300_000), Totals(), Totals()),
            BudgetReason.MONTHLY_LIMIT,
        ),
    ],
)
def test_each_limit_admits_exactly_its_boundary_and_rejects_one_micro_more(
    limit: dict[str, int], usage: SpendUsage, reason: BudgetReason
) -> None:
    assert check_limits(limits(**limit), usage, 200_000) is None
    rejection = check_limits(limits(**limit), usage, 200_001)
    assert rejection is not None and rejection.reason is reason


def test_no_limits_admit_anything() -> None:
    assert check_limits(limits(), SpendUsage(Totals(10**12), Totals(10**12)), 10**12) is None


def test_ledger_rejections_are_logged_with_limits_usage_and_estimate(
    ledger: SpendLedger,
) -> None:
    ledger.reserve(call("root"), estimate(300_000), limits(per_task=USD))
    with pytest.raises(BudgetRejected) as rejected:
        ledger.reserve(call("root"), estimate(800_000), limits(per_task=USD))
    assert rejected.value.reason is BudgetReason.PER_TASK_LIMIT
    assert USER_ACTION in str(rejected.value)

    allowed, refused = ledger.admissions()
    assert allowed.allowed and allowed.reservation_id is not None and allowed.reason is None
    assert not refused.allowed and refused.reservation_id is None
    assert refused.reason is BudgetReason.PER_TASK_LIMIT
    assert refused.estimate_micros == 800_000
    assert (refused.estimator, refused.estimator_version) == ("fixture-prices", "test-1")
    assert refused.limits["per_task"] == USD
    assert refused.usage["task"] == {"spent": 0, "reserved": 300_000}
    assert refused.call.root_task_id == "root"


# -- Guarded provider calls --------------------------------------------------------------------


def test_paid_call_reserves_dispatches_and_settles(ledger: SpendLedger) -> None:
    paid = provider("paid", "api", "m", usage=Usage(cost_usd=0.05, reference="req-1"))

    response = guard(ledger, monthly=USD).invoke(paid, request(), "classification")

    assert response.text == "ok"
    (reservation,) = ledger.reservations()
    assert reservation.status is ReservationStatus.SETTLED
    assert reservation.reserved_micros == CALL_COST
    assert reservation.settled_micros == 50_000
    assert reservation.settlement_source is SettlementSource.PROVIDER_COST
    assert reservation.reference == "req-1"
    kinds = [event.kind for event in ledger.events(reservation.id)]
    assert kinds == ["reserved", "dispatched", "settled"]


@pytest.mark.parametrize(
    ("usage", "micros", "source"),
    [
        (
            Usage(input_tokens=100, output_tokens=100, cost_usd=0.07),
            70_000,
            SettlementSource.PROVIDER_COST,
        ),
        (Usage(input_tokens=100, output_tokens=100), 20_000, SettlementSource.TOKEN_PRICING),
        (Usage(input_tokens=100), 200_000, SettlementSource.RESERVED_BOUND),
        (Usage(total_tokens=200), 200_000, SettlementSource.RESERVED_BOUND),
        (Usage(cost_usd=-1.0), 200_000, SettlementSource.RESERVED_BOUND),
        (None, 200_000, SettlementSource.RESERVED_BOUND),
    ],
)
def test_settlement_prefers_provider_cost_then_tokens_then_the_reserved_bound(
    ledger: SpendLedger, usage: Usage | None, micros: int, source: SettlementSource
) -> None:
    reservation = ledger.reserve(call(), estimate(200_000), limits())
    ledger.mark_dispatched(reservation.id)

    settled = ledger.settle(reservation.id, usage, PRICES)

    assert (settled.settled_micros, settled.settlement_source) == (micros, source)


def test_token_settlement_needs_the_same_pricing_version(ledger: SpendLedger) -> None:
    reservation = ledger.reserve(call(), estimate(200_000), limits())
    ledger.mark_dispatched(reservation.id)
    newer = PriceTable(PRICES.source, "test-2", PRICES.prices)

    settled = ledger.settle(reservation.id, Usage(input_tokens=1, output_tokens=1), newer)

    assert settled.settlement_source is SettlementSource.RESERVED_BOUND
    assert settled.settled_micros == 200_000


def test_actual_cost_above_the_reservation_is_recorded_truthfully(ledger: SpendLedger) -> None:
    reservation = ledger.reserve(call(), estimate(200_000), limits(monthly=USD))
    ledger.mark_dispatched(reservation.id)

    settled = ledger.settle(reservation.id, Usage(cost_usd=0.9))

    assert settled.settled_micros == 900_000
    overrun = ledger.events(reservation.id)[-1]
    assert overrun.kind == "estimation_overrun"
    assert overrun.detail["reserved"] == 200_000 and overrun.detail["charged"] == 900_000
    with pytest.raises(BudgetRejected) as rejected:
        ledger.reserve(call(), estimate(200_000), limits(monthly=USD))
    assert rejected.value.reason is BudgetReason.MONTHLY_LIMIT


def test_unknown_pricing_fails_closed_without_calling_the_provider(ledger: SpendLedger) -> None:
    paid = provider("paid", "api", "unpriced")

    with pytest.raises(BudgetRejected) as rejected:
        guard(ledger).invoke(paid, request("unpriced"), "test")

    assert rejected.value.reason is BudgetReason.PRICING_UNKNOWN
    assert paid.requests == []
    assert ledger.reservations() == []
    (admission,) = ledger.admissions()
    assert admission.reason is BudgetReason.PRICING_UNKNOWN and not admission.allowed


def test_default_pricing_knows_no_real_prices(ledger: SpendLedger) -> None:
    paid = provider("paid", "api", "m")
    with pytest.raises(BudgetRejected) as rejected:
        BudgetGuard(ledger, limits(), NO_PRICING).invoke(paid, request(), "test")
    assert rejected.value.reason is BudgetReason.PRICING_UNKNOWN


def test_unbounded_output_fails_closed(ledger: SpendLedger) -> None:
    paid = provider("paid", "api", "m")

    with pytest.raises(BudgetRejected) as rejected:
        guard(ledger).invoke(paid, request(max_output_tokens=None), "test")

    assert rejected.value.reason is BudgetReason.UNBOUNDED_OUTPUT
    assert paid.requests == []


def test_non_conservative_estimates_cannot_admit_a_call(ledger: SpendLedger) -> None:
    class Optimistic:
        def input_bound(self, call: PaidCall, request: ProviderRequest) -> int:
            return 1

        def estimate(self, call: PaidCall) -> CostEstimate:
            return CostEstimate(1, 1, 1, "guess", "1", conservative=False)

        def price_usage(self, *args: object) -> int | None:
            return None

    with pytest.raises(BudgetRejected) as rejected:
        BudgetGuard(ledger, limits(), Optimistic()).invoke(
            provider("paid", "api", "m"), request(), "test"
        )
    assert rejected.value.reason is BudgetReason.PRICING_UNKNOWN


@pytest.mark.parametrize("access", ["subscription", "local"])
def test_subscription_and_local_calls_never_touch_the_ledger(
    ledger: SpendLedger, access: AccessKind
) -> None:
    free = provider("free", access, "m")

    guard(ledger, per_call=0, monthly=0).invoke(free, request(max_output_tokens=None), "test")

    assert len(free.requests) == 1
    assert not ledger.path.exists()


def test_unknown_access_is_budgeted_as_paid(ledger: SpendLedger) -> None:
    with pytest.raises(BudgetRejected):
        guard(ledger).invoke(provider("mystery", None, "m"), request(), "test")


def test_per_call_limit_blocks_before_dispatch(ledger: SpendLedger) -> None:
    paid = provider("paid", "api", "m")

    with pytest.raises(BudgetRejected) as rejected:
        guard(ledger, per_call=100_000).invoke(paid, request(), "test")

    assert rejected.value.reason is BudgetReason.PER_CALL_LIMIT
    assert paid.requests == []
    assert ledger.reservations() == []


def test_provider_failure_after_dispatch_is_charged_the_reserved_bound(
    ledger: SpendLedger,
) -> None:
    paid = provider("paid", "api", "m")
    paid.error = AdapterError(ErrorKind.TIMEOUT, "no response")

    with pytest.raises(AdapterError):
        guard(ledger).invoke(paid, request(), "test")

    (reservation,) = ledger.reservations()
    assert reservation.status is ReservationStatus.SETTLED
    assert reservation.settled_micros == reservation.reserved_micros > 0
    assert reservation.settlement_source is SettlementSource.RESERVED_BOUND
    assert reservation.note == "failed after dispatch: AdapterError"


def test_paid_classifier_runs_through_budget_guard(
    ledger: SpendLedger, tasks: TaskStore
) -> None:
    session = tasks.start_session()
    root = tasks.create_task(session.id, "Root")
    child = tasks.create_task(session.id, "Classify", parent_id=root.id)
    reply = (
        '{"results":[{"task_id":"' + child.id + '","signals":[],"evidence":"bounded"}]}'
    )
    paid = provider("paid", "api", "m", reply=reply)
    choice = ClassifierChoice("selected", ModelPath("m", paid.identity()), "m")
    budget = guard(ledger, tasks)
    classifier = build_classifier(
        choice,
        AdapterRegistry(providers=[paid]),
        budgeted_invoke=budget.invoke_classifier,
    )
    assert classifier is not None

    result = classify(
        classifier,
        [ClassificationInput(child.id, "implementer", "implement")],
        load_policy().vocabulary,
    )

    assert result.mode is ClassifierMode.USED
    assert len(paid.requests) == 1
    (reservation,) = ledger.reservations()
    assert reservation.call.root_task_id == root.id
    assert reservation.call.task_id == child.id
    assert reservation.call.purpose == "skill_classification"
    assert reservation.status is ReservationStatus.SETTLED


def test_paid_classifier_never_batches_different_root_budgets(
    ledger: SpendLedger, tasks: TaskStore
) -> None:
    session = tasks.start_session()
    first = tasks.create_task(session.id, "First")
    second = tasks.create_task(session.id, "Second")
    paid = provider("paid", "api", "m")
    choice = ClassifierChoice("selected", ModelPath("m", paid.identity()), "m")
    budget = guard(ledger, tasks)
    classifier = build_classifier(
        choice,
        AdapterRegistry(providers=[paid]),
        budgeted_invoke=budget.invoke_classifier,
    )
    assert classifier is not None

    result = classify(
        classifier,
        [
            ClassificationInput(first.id, "implementer", "one"),
            ClassificationInput(second.id, "implementer", "two"),
        ],
        load_policy().vocabulary,
    )

    assert result.mode is ClassifierMode.FAILED
    assert "multiple root-task budgets" in result.error
    assert paid.requests == []
    assert ledger.reservations() == []


def test_session_and_task_overrides_tighten_the_guard(
    service: ConfigService, ledger: SpendLedger
) -> None:
    service.set_session({"cost": {"per_call_limit_usd": 0.25}})
    paid = provider("paid", "api", "m")

    BudgetGuard.from_config(service.resolve(), ledger, PRICES).invoke(paid, request(), "test")
    task_config = service.resolve({"cost": {"per_call_limit_usd": 0.10}})
    with pytest.raises(BudgetRejected) as rejected:
        BudgetGuard.from_config(task_config, ledger, PRICES).invoke(paid, request(), "test")
    assert rejected.value.reason is BudgetReason.PER_CALL_LIMIT


def test_descendant_subtasks_share_one_root_task_ceiling(
    ledger: SpendLedger, tasks: TaskStore
) -> None:
    session = tasks.start_session()
    root = tasks.create_task(session.id, "Add caching")
    child = tasks.create_task(session.id, "Write spec", parent_id=root.id)
    grandchild = tasks.create_task(session.id, "Implement", parent_id=child.id)
    other = tasks.create_task(session.id, "Unrelated")
    paid = provider("paid", "api", "m")
    budget = guard(ledger, tasks, per_task=2 * CALL_COST + 1)

    budget.invoke(paid, request(), "spec", task_id=child.id)
    budget.invoke(paid, request(), "implement", task_id=grandchild.id)
    with pytest.raises(BudgetRejected) as rejected:
        budget.invoke(paid, request(), "implement", task_id=grandchild.id)
    budget.invoke(paid, request(), "other", task_id=other.id)

    assert rejected.value.reason is BudgetReason.PER_TASK_LIMIT
    assert {r.call.root_task_id for r in ledger.reservations()} == {root.id, other.id}
    assert ledger.usage(root.id).task == Totals(spent=2 * CALL_COST)
    kinds = [event.kind for event in tasks.events(session.id, grandchild.id)]
    assert kinds[-2:] == ["budget_settled", "budget_rejected"]


def test_subtasks_cannot_raise_their_root_task_ceiling(
    ledger: SpendLedger, tasks: TaskStore
) -> None:
    session = tasks.start_session()
    root = tasks.create_task(session.id, "Add caching")
    child = tasks.create_task(session.id, "Implement", parent_id=root.id)
    paid = provider("paid", "api", "m")

    guard(ledger, tasks, per_task=CALL_COST).invoke(paid, request(), "plan", task_id=root.id)
    # Config built for the subtask has a looser (or no) per-task limit: the root's ceiling holds.
    for looser in (10 * CALL_COST, None):
        with pytest.raises(BudgetRejected) as rejected:
            guard(ledger, tasks, per_task=looser).invoke(
                paid, request(), "implement", task_id=child.id
            )
        assert rejected.value.reason is BudgetReason.PER_TASK_LIMIT
    assert ledger.usage(root.id).task_ceiling == TaskCeiling(CALL_COST)


def test_root_task_ceiling_only_ever_tightens(ledger: SpendLedger) -> None:
    ledger.reserve(call("root"), estimate(100), limits(per_task=USD))
    ledger.reserve(call("root"), estimate(100), limits(per_task=500_000))
    ledger.reserve(call("root"), estimate(100), limits(per_task=None))
    assert ledger.usage("root").task_ceiling == TaskCeiling(500_000)

    ledger.reserve(call("unlimited"), estimate(100), limits())
    assert ledger.usage("unlimited").task_ceiling == TaskCeiling(None)
    ledger.reserve(call("unlimited"), estimate(100), limits(per_task=300))
    assert ledger.usage("unlimited").task_ceiling == TaskCeiling(300)
    assert ledger.usage("never-admitted").task_ceiling is None


def test_rejected_call_still_tightens_the_root_task_ceiling(ledger: SpendLedger) -> None:
    first = ledger.reserve(call("root"), estimate(100_000), limits(per_task=1 * USD))
    ledger.mark_dispatched(first.id)
    ledger.settle(first.id, Usage(cost_usd=0.10))

    with pytest.raises(BudgetRejected) as rejected:
        ledger.reserve(call("root"), estimate(450_000), limits(per_task=500_000))
    assert rejected.value.reason is BudgetReason.PER_TASK_LIMIT
    assert ledger.usage("root").task_ceiling == TaskCeiling(500_000)

    with pytest.raises(BudgetRejected) as sibling:
        ledger.reserve(call("root"), estimate(450_000), limits(per_task=1 * USD))
    assert sibling.value.reason is BudgetReason.PER_TASK_LIMIT

def test_budget_shows_the_root_task_ceiling(ledger: SpendLedger, tasks: TaskStore) -> None:
    session = tasks.start_session()
    root = tasks.create_task(session.id, "Current work")
    ledger.reserve(call(root.id), estimate(100_000), limits(per_task=250_000))

    lines = budget_lines(None, tasks, ledger)

    assert f"Task {root.id}: spent $0.00 · reserved $0.10 · limit $0.25 · remaining $0.15" in lines


@pytest.mark.parametrize("access", ["subscription", "local"])
def test_unpaid_task_calls_need_no_task_state_or_ledger(
    ledger: SpendLedger, access: AccessKind
) -> None:
    free = provider("free", access, "m")
    guard(ledger).invoke(free, request(), "implement", task_id="task-anything")
    assert len(free.requests) == 1
    assert not ledger.path.exists()


def test_task_scoped_calls_need_task_state(ledger: SpendLedger) -> None:
    with pytest.raises(ValueError, match="root task"):
        guard(ledger).invoke(provider("paid", "api", "m"), request(), "x", task_id="task-1")


def test_root_task_resolves_any_depth_and_rejects_unknown_tasks(tasks: TaskStore) -> None:
    session = tasks.start_session()
    root = tasks.create_task(session.id, "root")
    leaf = tasks.create_task(
        session.id, "leaf", parent_id=tasks.create_task(session.id, "mid", parent_id=root.id).id
    )
    assert tasks.root_task(leaf.id) == tasks.root_task(root.id) == root.id
    with pytest.raises(TaskStateError, match="unknown task"):
        tasks.root_task("task-missing")


# -- Candidate-path economics ------------------------------------------------------------------


def candidates(*adapters: FakeProviderAdapter, entries: Sequence[dict[str, Any]]) -> Any:
    runtime = runtime_paths(AdapterRegistry(providers=adapters), Config())
    return ranked_model_candidates(
        registry(*entries), "implementer", RoleFloors(), runtime, PREFERENCE
    )


def test_compliant_subscription_model_beats_a_stronger_api_model(ledger: SpendLedger) -> None:
    ranked = candidates(
        provider("paid", "api", "best"),
        provider("sub", "subscription", "weaker"),
        entries=[model("best", scores={"coding": 5}), model("weaker", scores={"coding": 3})],
    )
    assert [c.model_id for c in ranked] == ["best", "weaker"]

    result = budget_candidates(ranked, bounds(), PRICES, limits(), ledger.usage(), PREFERENCE)

    assert [(q.model_id, q.path.provider.id) for q in result.paths] == [
        ("weaker", "sub"),
        ("best", "paid"),
    ]


def test_api_paths_rank_by_lower_reserved_cost_before_capability(ledger: SpendLedger) -> None:
    ranked = candidates(
        provider("pricey", "api", "best"),
        provider("paid", "api", "best", "cheap"),
        entries=[model("best", scores={"coding": 5}), model("cheap", scores={"coding": 3})],
    )

    result = budget_candidates(ranked, bounds(), PRICES, limits(), ledger.usage(), PREFERENCE)

    assert [(q.model_id, q.path.provider.id, q.cost_micros) for q in result.paths] == [
        ("cheap", "paid", 20_000),
        ("best", "paid", 200_000),
        ("best", "pricey", 400_000),
    ]


def test_over_budget_and_unpriced_api_paths_are_removed_with_reasons(
    ledger: SpendLedger,
) -> None:
    ranked = candidates(
        provider("pricey", "api", "best"),
        provider("paid", "api", "unpriced", "cheap"),
        entries=[model("best"), model("unpriced"), model("cheap")],
    )

    result = budget_candidates(
        ranked, bounds(), PRICES, limits(per_call=300_000), ledger.usage(), PREFERENCE
    )

    assert [q.model_id for q in result.paths] == ["cheap"]
    assert {q.model_id: q.rejection.reason for q in result.rejected if q.rejection} == {
        "best": BudgetReason.PER_CALL_LIMIT,
        "unpriced": BudgetReason.PRICING_UNKNOWN,
    }


def test_quoting_uses_the_spend_snapshot_but_reserves_nothing(ledger: SpendLedger) -> None:
    ledger.reserve(call(), estimate(900_000), limits())
    ranked = candidates(provider("paid", "api", "best"), entries=[model("best")])

    result = budget_candidates(
        ranked, bounds(), PRICES, limits(monthly=USD), ledger.usage(), PREFERENCE
    )

    assert result.paths == ()
    assert result.rejected[0].rejection is not None
    assert result.rejected[0].rejection.reason is BudgetReason.MONTHLY_LIMIT
    assert len(ledger.reservations()) == 1
    assert len(ledger.admissions()) == 1


def test_ranking_never_creates_the_ledger(ledger: SpendLedger) -> None:
    ranked = candidates(provider("paid", "api", "best"), entries=[model("best")])
    budget_candidates(ranked, bounds(), PRICES, limits(), ledger.usage(), PREFERENCE)
    assert not ledger.path.exists()


def test_unbounded_work_leaves_only_unpaid_paths() -> None:
    ranked = candidates(
        provider("paid", "api", "best"),
        provider("local", "local", "best"),
        entries=[model("best")],
    )

    result = budget_candidates(
        ranked, bounds(max_output_tokens=None), PRICES, limits(), empty(), PREFERENCE
    )

    assert [q.path.provider.id for q in result.paths] == ["local"]
    assert result.rejected[0].rejection is not None
    assert result.rejected[0].rejection.reason is BudgetReason.UNBOUNDED_OUTPUT


def pin(
    *adapters: FakeProviderAdapter, requested: str = "best", entries: Sequence[dict[str, Any]] = ()
) -> PinnedPaths:
    runtime = runtime_paths(AdapterRegistry(providers=adapters), Config())
    reg = registry(*(entries or [model("best")]))
    return pinned_paths(reg, runtime, PREFERENCE, ModelPin(requested, "task:t1"))


def test_pinned_paths_keep_every_provider_for_budget_to_choose() -> None:
    pinned = pin(provider("paid", "api", "best"), provider("sub", "subscription", "best"))
    assert [p.provider.id for p in pinned.paths] == ["sub", "paid"]

    # A stronger, cheaper unpinned model is never substituted.
    other = pin(
        provider("paid", "api", "best", "cheap"),
        entries=[model("best"), model("cheap", scores={"coding": 5})],
    )
    result = budget_pinned(other, bounds(), PRICES, limits(), empty(), PREFERENCE)
    assert [q.model_id for q in result.paths] == ["best"]


def test_pins_rank_their_own_paths_but_never_bypass_hard_limits() -> None:
    pinned = pin(provider("paid", "api", "best"), provider("sub", "subscription", "best"))
    result = budget_pinned(pinned, bounds(), PRICES, limits(), empty(), PREFERENCE)
    assert [q.path.provider.id for q in result.paths] == ["sub", "paid"]

    blocked = budget_pinned(
        pinned, bounds(), PRICES, limits(per_call=100_000), empty(), PREFERENCE, "paid"
    )
    with pytest.raises(BudgetBlocked) as error:
        blocked.first("pinned model best")
    assert error.value.reasons == (BudgetReason.PER_CALL_LIMIT,)
    assert USER_ACTION in str(error.value) and "pinned model best" in str(error.value)

    unpriced = pin(provider("paid", "api", "mystery"), requested="mystery")
    with pytest.raises(BudgetBlocked) as error:
        budget_pinned(unpriced, bounds(), PRICES, limits(), empty(), PREFERENCE).first()
    assert error.value.reasons == (BudgetReason.PRICING_UNKNOWN,)


def test_pinned_provider_that_does_not_serve_the_model_is_reported() -> None:
    pinned = pin(provider("paid", "api", "best"))
    with pytest.raises(NoModelAvailable, match="pinned provider 'sub'"):
        budget_pinned(pinned, bounds(), PRICES, limits(), empty(), PREFERENCE, "sub")


def test_opaque_paid_harness_needs_an_enforceable_bound() -> None:
    paid = path("paid", "api", "best")
    unbounded = quote_harness_path("best", paid, 0, bounds(), None, limits(), empty())
    assert unbounded.rejection is not None
    assert unbounded.rejection.reason is BudgetReason.PAID_HARNESS_UNENFORCEABLE

    bounded = quote_harness_path("best", paid, 0, bounds(), 250_000, limits(), empty())
    assert bounded.admissible and bounded.cost_micros == 250_000

    too_big = quote_harness_path(
        "best", paid, 0, bounds(), 250_000, limits(per_call=200_000), empty()
    )
    assert too_big.rejection is not None
    assert too_big.rejection.reason is BudgetReason.PER_CALL_LIMIT

    sub = path("sub", "subscription", "best")
    assert quote_harness_path("best", sub, 0, bounds(), None, limits(), empty()).admissible


# -- /budget -----------------------------------------------------------------------------------


def test_budget_shows_defaults_with_no_spend(ledger: SpendLedger) -> None:
    lines = budget_lines(None, None, ledger)

    assert lines[:4] == [
        "Paid API budget",
        "Month (UTC): spent $0.00 · reserved $0.00 · limit $20.00 · remaining $20.00",
        "Day (UTC):   spent $0.00 · reserved $0.00 · limit none",
        "Per call:    limit $0.30",
    ]
    assert "Active reservations: 0" in lines
    assert not ledger.path.exists()


def test_budget_shows_spent_reserved_and_headroom(
    service: ConfigService, ledger: SpendLedger
) -> None:
    service.set_session({"cost": {"daily_limit_usd": 1.0}})
    settled = ledger.reserve(call(), estimate(250_000), limits())
    ledger.mark_dispatched(settled.id)
    ledger.settle(settled.id, Usage(cost_usd=0.25))
    ledger.reserve(call(), estimate(100_000), limits())
    unresolved = ledger.reserve(call(), estimate(50_000), limits())
    ledger.mark_dispatched(unresolved.id)

    lines = budget_lines(service, None, ledger)

    assert lines[1] == (
        "Month (UTC): spent $0.25 · reserved $0.15 · limit $20.00 · remaining $19.60"
    )
    assert lines[2] == "Day (UTC):   spent $0.25 · reserved $0.15 · limit $1.00 · remaining $0.60"
    assert "Active reservations: 2" in lines
    assert any(line.startswith("Unresolved dispatched calls: 1") for line in lines)


def test_budget_shows_the_active_root_task(ledger: SpendLedger, tasks: TaskStore) -> None:
    session = tasks.start_session()
    done = tasks.create_task(session.id, "Old work")
    tasks.set_status(done.id, TaskStatus.COMPLETED)
    root = tasks.create_task(session.id, "Current work")
    ledger.reserve(call(root.id), estimate(400_000), limits())

    lines = budget_lines(None, tasks, ledger)

    assert f"Task {root.id}: spent $0.00 · reserved $0.40 · limit $1.00 · remaining $0.60" in lines


def test_budget_reports_an_unusable_ledger(ledger: SpendLedger) -> None:
    ledger.path.parent.mkdir(parents=True)
    ledger.path.write_bytes(b"garbage" * 200)

    lines = budget_lines(None, None, ledger)

    assert lines[0].startswith("Cannot read spend ledger:")
    assert lines[1] == "No paid API calls will be admitted."


def test_budget_command_is_implemented(paths: ConfigPaths) -> None:
    context = CommandContext(registry=build_registry(), config=ConfigService(paths))
    output = context.registry.dispatch("/budget", context)
    assert output.startswith("Paid API budget\nMonth (UTC):")

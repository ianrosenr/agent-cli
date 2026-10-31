"""The global paid-spend ledger: ``~/.agent-cli/state/spend.sqlite``.

Monthly and daily limits span repositories, so the ledger is global and canonical: never a cache,
never deleted or recreated. A corrupt or newer database fails with ``BudgetError`` and is left
untouched, and with no usable ledger no paid call is admitted.

Admission is atomic across processes: one ``BEGIN IMMEDIATE`` transaction reads settled spend and
active reservations, checks every limit, and inserts the reservation. Lifecycle::

    reserved -> dispatched -> settled
    reserved -> released          (only when the call provably never ran)

Reserved and dispatched amounts both consume headroom. Prompts, credentials, and model reasoning
are never stored here.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_cli.adapters import Usage
from agent_cli.budget.domain import (
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
    TaskCeiling,
    Totals,
    tighter,
    to_micros,
)
from agent_cli.budget.policy import check_limits
from agent_cli.budget.pricing import CostEstimator
from agent_cli.state.migrations import Migration, MigrationError, migrate
from agent_cli.state.paths import ensure_private_dir, global_state_dir

SPEND_FILE = "spend.sqlite"
BUSY_TIMEOUT = 10.0
"""Seconds to wait for another process's admission transaction before failing."""

type Clock = Callable[[], datetime]

V1: Migration = (
    """
    CREATE TABLE reservations (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL
            CHECK (status IN ('reserved', 'dispatched', 'settled', 'released')),
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        access TEXT NOT NULL,
        purpose TEXT NOT NULL,
        root_task_id TEXT,
        task_id TEXT,
        session_id TEXT,
        estimator TEXT NOT NULL,
        estimator_version TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        reserved_micros INTEGER NOT NULL CHECK (reserved_micros >= 0),
        settled_micros INTEGER CHECK (settled_micros >= 0),
        settlement_source TEXT
            CHECK (settlement_source IN ('provider_cost', 'token_pricing', 'reserved_bound')),
        reference TEXT,
        note TEXT NOT NULL DEFAULT '',
        day TEXT NOT NULL,
        month TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK ((status = 'settled') = (settled_micros IS NOT NULL))
    )
    """,
    "CREATE INDEX reservations_by_month ON reservations (month)",
    "CREATE INDEX reservations_by_day ON reservations (day)",
    "CREATE INDEX reservations_by_root_task ON reservations (root_task_id)",
    "CREATE INDEX reservations_by_status ON reservations (status)",
    """
    CREATE TABLE admissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        access TEXT NOT NULL,
        purpose TEXT NOT NULL,
        root_task_id TEXT,
        task_id TEXT,
        session_id TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
        reason TEXT,
        message TEXT NOT NULL DEFAULT '',
        estimate_micros INTEGER,
        estimator TEXT,
        estimator_version TEXT,
        limits TEXT NOT NULL,
        usage TEXT NOT NULL,
        reservation_id TEXT REFERENCES reservations (id),
        created_at TEXT NOT NULL,
        CHECK ((allowed = 1) = (reason IS NULL))
    )
    """,
    """
    CREATE TABLE ledger_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reservation_id TEXT NOT NULL REFERENCES reservations (id),
        kind TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX ledger_events_by_reservation ON ledger_events (reservation_id)",
    """
    CREATE TABLE task_ceilings (
        root_task_id TEXT PRIMARY KEY,
        limit_micros INTEGER CHECK (limit_micros >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)
"""Reservations, every paid-path admission decision, append-only lifecycle events, and each
root task's per-task ceiling (``NULL`` when admitted with no per-task limit)."""

MIGRATIONS: tuple[Migration, ...] = (V1,)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Admission:
    """One logged paid-path admission decision."""

    id: int
    call: PaidCall
    allowed: bool
    reason: BudgetReason | None
    message: str
    estimate_micros: int | None
    estimator: str | None
    estimator_version: str | None
    limits: dict[str, int | None]
    usage: dict[str, Any]
    reservation_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    reservation_id: str
    kind: str
    detail: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class Charge:
    micros: int
    source: SettlementSource


def settlement_charge(
    reservation: Reservation, usage: Usage | None, estimator: CostEstimator | None
) -> Charge:
    """What a dispatched call is charged, best evidence first: the provider's reported cost,
    then reliable token usage priced by the same source that estimated it, then the full
    reserved upper bound. Unknown usage is never charged as zero."""
    if usage is not None and usage.cost_usd is not None:
        try:
            return Charge(to_micros(usage.cost_usd, round_up=True), SettlementSource.PROVIDER_COST)
        except ValueError:
            pass  # A negative or non-finite report is not evidence.
    if (
        usage is not None
        and estimator is not None
        and usage.input_tokens is not None
        and usage.output_tokens is not None
        and usage.input_tokens >= 0
        and usage.output_tokens >= 0
    ):
        micros = estimator.price_usage(
            reservation.call, reservation.estimate, usage.input_tokens, usage.output_tokens
        )
        if micros is not None:
            return Charge(micros, SettlementSource.TOKEN_PRICING)
    return Charge(reservation.reserved_micros, SettlementSource.RESERVED_BOUND)


class SpendLedger:
    """Typed, transactional access to the paid-spend ledger. Opened on first use."""

    def __init__(
        self,
        path: Path,
        migrations: tuple[Migration, ...] = MIGRATIONS,
        clock: Clock = utc_now,
    ) -> None:
        self.path = path
        self._migrations = migrations
        self._clock = clock
        self._connection: sqlite3.Connection | None = None

    @classmethod
    def default(cls, home: Path | None = None) -> SpendLedger:
        """The one global ledger every repository shares."""
        return cls(global_state_dir(home) / SPEND_FILE)

    def exists(self) -> bool:
        """Whether any spend has been recorded. Checking does not create the database."""
        return self.path.is_file()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    # -- Connection ----------------------------------------------------------------------------

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = self._open()
        return self._connection

    def _open(self) -> sqlite3.Connection:
        try:
            ensure_private_dir(self.path.parent)
            connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT, isolation_level=None)
        except (OSError, sqlite3.Error) as error:
            raise BudgetError(f"cannot open spend ledger at {self.path}: {error}") from error
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            migrate(connection, self._migrations, "spend ledger")
        except (MigrationError, sqlite3.Error) as error:
            connection.close()
            raise BudgetError(f"cannot use spend ledger at {self.path}: {error}") from error
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        db = self._db
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as error:
            raise BudgetError(f"cannot update spend ledger: {error}") from error
        try:
            yield db
            db.execute("COMMIT")
        except sqlite3.Error as error:
            rollback(db)
            raise BudgetError(f"cannot update spend ledger: {error}") from error
        except BaseException:
            rollback(db)
            raise

    def _read(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        try:
            return self._db.execute(sql, params).fetchall()
        except sqlite3.Error as error:
            raise BudgetError(f"cannot read spend ledger: {error}") from error

    # -- Admission -----------------------------------------------------------------------------

    def reserve(self, call: PaidCall, estimate: CostEstimate, limits: Limits) -> Reservation:
        """Atomically check every limit and reserve ``estimate`` for ``call``.

        Raises ``BudgetRejected`` (after logging the decision) when the call does not fit.
        Call only immediately before dispatching: a reservation consumes headroom until it is
        settled or released.
        """
        if not call.paid:
            raise ValueError(f"{call.access} access is not charged to the paid-API ledger")
        if not estimate.conservative:
            raise ValueError("only a conservative estimate can reserve spend")
        now = self._clock()
        stamp = timestamp(now)
        rejection: Rejection | None = None
        reservation: Reservation | None = None
        with self._write() as db:
            usage = current_usage(db, now, call.root_task_id)
            usage = tighten_task_ceiling(db, call.root_task_id, limits.per_task, usage, stamp)
            rejection = check_limits(limits, usage, estimate.usd_micros)
            if rejection is None:
                reservation = Reservation(
                    id=f"spend-{uuid.uuid4().hex[:12]}",
                    call=call,
                    estimate=estimate,
                    status=ReservationStatus.RESERVED,
                    reserved_micros=estimate.usd_micros,
                    settled_micros=None,
                    settlement_source=None,
                    day=day_of(now),
                    month=month_of(now),
                    created_at=stamp,
                    updated_at=stamp,
                )
                insert_reservation(db, reservation)
            log_admission(
                db,
                call,
                rejection,
                estimate,
                limits,
                usage,
                stamp,
                None if reservation is None else reservation.id,
            )
        if reservation is None:
            assert rejection is not None
            raise BudgetRejected(rejection)
        return reservation

    def record_rejection(
        self,
        call: PaidCall,
        rejection: Rejection,
        limits: Limits,
        estimate: CostEstimate | None = None,
    ) -> None:
        """Log a paid path refused before reservation, such as for unknown pricing."""
        now = self._clock()
        stamp = timestamp(now)
        with self._write() as db:
            usage = current_usage(db, now, call.root_task_id)
            usage = tighten_task_ceiling(db, call.root_task_id, limits.per_task, usage, stamp)
            log_admission(db, call, rejection, estimate, limits, usage, stamp, None)

    # -- Lifecycle -----------------------------------------------------------------------------

    def mark_dispatched(self, reservation_id: str) -> None:
        """Record that the call may now run. From here it can never be released."""
        self._transition(
            reservation_id,
            (ReservationStatus.RESERVED,),
            ReservationStatus.DISPATCHED,
            {},
            what="mark it dispatched",
        )

    def release(self, reservation_id: str, reason: str) -> None:
        """Release a reservation whose call provably never dispatched, restoring headroom."""
        self._transition(
            reservation_id,
            (ReservationStatus.RESERVED,),
            ReservationStatus.RELEASED,
            {"note": reason},
            what="release (it may have run; settle it instead)",
        )

    def settle(
        self,
        reservation_id: str,
        usage: Usage | None,
        estimator: CostEstimator | None = None,
        note: str = "",
    ) -> Reservation:
        """Record the final charge for a dispatched call (see ``settlement_charge``).

        A charge above the reservation is recorded truthfully, never truncated, and logged as an
        ``estimation_overrun``; later calls that no longer fit are then refused.
        """
        reservation = self.reservation(reservation_id)
        if reservation is None:
            raise BudgetError(f"unknown reservation: {reservation_id}")
        charge = settlement_charge(reservation, usage, estimator)
        reference = None if usage is None else usage.reference
        changes: dict[str, Any] = {
            "settled_micros": charge.micros,
            "settlement_source": charge.source,
            "note": note,
        }
        if reference is not None:
            changes["reference"] = reference
        with self._write() as db:
            self._transition_in(
                db,
                reservation_id,
                (ReservationStatus.DISPATCHED,),
                ReservationStatus.SETTLED,
                changes,
                "settle (it was never dispatched)",
            )
            if charge.micros > reservation.reserved_micros:
                event(
                    db,
                    reservation_id,
                    "estimation_overrun",
                    self._stamp(),
                    reserved=reservation.reserved_micros,
                    charged=charge.micros,
                    source=charge.source,
                    estimator=reservation.estimate.source,
                    estimator_version=reservation.estimate.source_version,
                )
        settled = self.reservation(reservation_id)
        assert settled is not None
        return settled

    def _transition(
        self,
        reservation_id: str,
        allowed: tuple[ReservationStatus, ...],
        status: ReservationStatus,
        changes: dict[str, Any],
        what: str,
    ) -> None:
        with self._write() as db:
            self._transition_in(db, reservation_id, allowed, status, changes, what)

    def _transition_in(
        self,
        db: sqlite3.Connection,
        reservation_id: str,
        allowed: tuple[ReservationStatus, ...],
        status: ReservationStatus,
        changes: dict[str, Any],
        what: str,
    ) -> None:
        row = db.execute(
            "SELECT status FROM reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise BudgetError(f"unknown reservation: {reservation_id}")
        current = ReservationStatus(row[0])
        if current not in allowed:
            raise BudgetError(f"cannot {what}: reservation {reservation_id} is {current}")
        stamp = self._stamp()
        columns = {"status": status, "updated_at": stamp, **changes}
        assignments = ", ".join(f"{column} = ?" for column in columns)
        db.execute(
            f"UPDATE reservations SET {assignments} WHERE id = ?",
            (*columns.values(), reservation_id),
        )
        detail = {key: value for key, value in changes.items() if value not in (None, "")}
        event(db, reservation_id, str(status), stamp, previous=current, **detail)

    def _stamp(self) -> str:
        return timestamp(self._clock())

    # -- Reading -------------------------------------------------------------------------------

    def usage(self, root_task_id: str | None = None) -> SpendUsage:
        """Current UTC month and day totals, and ``root_task_id``'s totals when given.

        Reading an absent ledger reports zero spend without creating it.
        """
        if not self.exists():
            return SpendUsage(Totals(), Totals(), None if root_task_id is None else Totals())
        try:
            return current_usage(self._db, self._clock(), root_task_id)
        except sqlite3.Error as error:
            raise BudgetError(f"cannot read spend ledger: {error}") from error

    def reservation(self, reservation_id: str) -> Reservation | None:
        rows = self._read("SELECT * FROM reservations WHERE id = ?", reservation_id)
        return to_reservation(rows[0]) if rows else None

    def reservations(self, status: ReservationStatus | None = None) -> list[Reservation]:
        if status is None:
            rows = self._read("SELECT * FROM reservations ORDER BY rowid")
        else:
            rows = self._read(
                "SELECT * FROM reservations WHERE status = ? ORDER BY rowid", str(status)
            )
        return [to_reservation(row) for row in rows]

    def admissions(self) -> list[Admission]:
        if not self.exists():
            return []
        return [to_admission(row) for row in self._read("SELECT * FROM admissions ORDER BY id")]

    def events(self, reservation_id: str) -> list[LedgerEvent]:
        rows = self._read(
            "SELECT * FROM ledger_events WHERE reservation_id = ? ORDER BY id", reservation_id
        )
        return [
            LedgerEvent(
                row["reservation_id"], row["kind"], json.loads(row["detail"]), row["created_at"]
            )
            for row in rows
        ]


# -- Helpers -----------------------------------------------------------------------------------


def timestamp(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="seconds")


def day_of(now: datetime) -> str:
    return now.astimezone(UTC).date().isoformat()


def month_of(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m")


ACTIVE = "status IN ('reserved', 'dispatched')"


def current_usage(db: sqlite3.Connection, now: datetime, root_task_id: str | None) -> SpendUsage:
    """Totals as one consistent read: settled charges are spent, active amounts are reserved."""

    def totals(scope: str) -> str:
        return (
            f"COALESCE(SUM(CASE WHEN {scope} AND status = 'settled' THEN settled_micros END), 0), "
            f"COALESCE(SUM(CASE WHEN {scope} AND {ACTIVE} THEN reserved_micros END), 0)"
        )

    row = db.execute(
        f"SELECT {totals('month = :month')}, {totals('day = :day')}, "
        f"{totals('root_task_id = :task')}, "
        f"COALESCE(SUM({ACTIVE}), 0), COALESCE(SUM(status = 'dispatched'), 0), "
        "MIN(CASE WHEN status = 'dispatched' THEN created_at END) FROM reservations",
        {"month": month_of(now), "day": day_of(now), "task": root_task_id},
    ).fetchone()
    ceiling = None
    if root_task_id is not None:
        stored = db.execute(
            "SELECT limit_micros FROM task_ceilings WHERE root_task_id = ?", (root_task_id,)
        ).fetchone()
        ceiling = None if stored is None else TaskCeiling(stored[0])
    return SpendUsage(
        month=Totals(row[0], row[1]),
        day=Totals(row[2], row[3]),
        task=None if root_task_id is None else Totals(row[4], row[5]),
        active=row[6],
        dispatched=row[7],
        oldest_dispatched_at=row[8],
        task_ceiling=ceiling,
    )


def tighten_task_ceiling(
    db: sqlite3.Connection,
    root_task_id: str | None,
    per_task: int | None,
    usage: SpendUsage,
    stamp: str,
) -> SpendUsage:
    """Persist the tightest root-task ceiling seen before evaluating a paid call."""
    if root_task_id is None:
        return usage
    previous = None if usage.task_ceiling is None else usage.task_ceiling.limit
    effective = per_task if usage.task_ceiling is None else tighter(previous, per_task)
    if usage.task_ceiling is None:
        db.execute(
            "INSERT INTO task_ceilings (root_task_id, limit_micros, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (root_task_id, effective, stamp, stamp),
        )
    elif effective != previous:
        db.execute(
            "UPDATE task_ceilings SET limit_micros = ?, updated_at = ? WHERE root_task_id = ?",
            (effective, stamp, root_task_id),
        )
    return replace(usage, task_ceiling=TaskCeiling(effective))


def insert_reservation(db: sqlite3.Connection, reservation: Reservation) -> None:
    call, estimate = reservation.call, reservation.estimate
    db.execute(
        "INSERT INTO reservations (id, status, provider_id, model_id, access, purpose, "
        "root_task_id, task_id, session_id, estimator, estimator_version, input_tokens, "
        "output_tokens, reserved_micros, day, month, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            reservation.id,
            reservation.status,
            call.provider_id,
            call.model_id,
            call.access,
            call.purpose,
            call.root_task_id,
            call.task_id,
            call.session_id,
            estimate.source,
            estimate.source_version,
            estimate.input_tokens,
            estimate.output_tokens,
            reservation.reserved_micros,
            reservation.day,
            reservation.month,
            reservation.created_at,
            reservation.updated_at,
        ),
    )
    event(
        db, reservation.id, "reserved", reservation.created_at, micros=reservation.reserved_micros
    )


def log_admission(
    db: sqlite3.Connection,
    call: PaidCall,
    rejection: Rejection | None,
    estimate: CostEstimate | None,
    limits: Limits,
    usage: SpendUsage,
    stamp: str,
    reservation_id: str | None,
) -> None:
    db.execute(
        "INSERT INTO admissions (provider_id, model_id, access, purpose, root_task_id, task_id, "
        "session_id, input_tokens, output_tokens, allowed, reason, message, estimate_micros, "
        "estimator, estimator_version, limits, usage, reservation_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            call.provider_id,
            call.model_id,
            call.access,
            call.purpose,
            call.root_task_id,
            call.task_id,
            call.session_id,
            call.input_tokens,
            call.max_output_tokens,
            int(rejection is None),
            None if rejection is None else str(rejection.reason),
            "" if rejection is None else rejection.message,
            None if estimate is None else estimate.usd_micros,
            None if estimate is None else estimate.source,
            None if estimate is None else estimate.source_version,
            json.dumps(asdict(limits)),
            json.dumps(asdict(usage)),
            reservation_id,
            stamp,
        ),
    )


def event(
    db: sqlite3.Connection, reservation_id: str, kind: str, stamp: str, **detail: Any
) -> None:
    db.execute(
        "INSERT INTO ledger_events (reservation_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
        (reservation_id, kind, json.dumps(detail), stamp),
    )


def rollback(db: sqlite3.Connection) -> None:
    with suppress(sqlite3.Error):
        db.execute("ROLLBACK")


def to_call(row: sqlite3.Row) -> PaidCall:
    return PaidCall(
        provider_id=row["provider_id"],
        access=row["access"],
        model_id=row["model_id"],
        purpose=row["purpose"],
        input_tokens=row["input_tokens"],
        max_output_tokens=row["output_tokens"],
        root_task_id=row["root_task_id"],
        task_id=row["task_id"],
        session_id=row["session_id"],
    )


def to_reservation(row: sqlite3.Row) -> Reservation:
    source = row["settlement_source"]
    return Reservation(
        id=row["id"],
        call=to_call(row),
        estimate=CostEstimate(
            usd_micros=row["reserved_micros"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            source=row["estimator"],
            source_version=row["estimator_version"],
            conservative=True,
        ),
        status=ReservationStatus(row["status"]),
        reserved_micros=row["reserved_micros"],
        settled_micros=row["settled_micros"],
        settlement_source=None if source is None else SettlementSource(source),
        day=row["day"],
        month=row["month"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        reference=row["reference"],
        note=row["note"],
    )


def to_admission(row: sqlite3.Row) -> Admission:
    reason = row["reason"]
    return Admission(
        id=row["id"],
        call=to_call(row),
        allowed=bool(row["allowed"]),
        reason=None if reason is None else BudgetReason(reason),
        message=row["message"],
        estimate_micros=row["estimate_micros"],
        estimator=row["estimator"],
        estimator_version=row["estimator_version"],
        limits=json.loads(row["limits"]),
        usage=json.loads(row["usage"]),
        reservation_id=row["reservation_id"],
        created_at=row["created_at"],
    )

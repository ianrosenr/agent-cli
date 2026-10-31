"""The canonical task-state store: ``<repo>/.agent-cli/state/tasks.sqlite``.

Only the orchestrator writes here (see docs/MEMORY.md). Records are append-friendly: superseded
requirements, decisions, pins, and spec versions are kept and linked to their replacements, and
every change appends an event. Nothing here interprets requests, plans work, or routes it.

The database is never deleted or recreated by Agent CLI. If it is corrupt, newer than this code,
or cannot be migrated, opening it fails with ``TaskStateError`` and the file is left untouched.
"""

from __future__ import annotations

import difflib
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from agent_cli.adapters import WorkerStatus
from agent_cli.state.migrations import MIGRATIONS, Migration, MigrationError, migrate
from agent_cli.state.models import (
    ALL_CHILDREN,
    PIN_KINDS,
    Approval,
    Artifact,
    Assumption,
    Blocker,
    Decision,
    Event,
    Finding,
    Pin,
    PinKind,
    PlannedPin,
    PlanningQuestion,
    PlanWrite,
    Requirement,
    RequirementSource,
    Review,
    ReviewKind,
    RoutedSignal,
    RoutedSkill,
    ScopedPin,
    Session,
    SkillRouting,
    SpecVersion,
    Task,
    TaskStatus,
    Worker,
)
from agent_cli.state.paths import ensure_private_dir, project_state_dir
from agent_cli.terms import CANONICAL_ROLES

TASKS_FILE = "tasks.sqlite"
BUSY_TIMEOUT = 5.0
"""Seconds to wait for another process's lock before failing."""

type Clock = Callable[[], str]


class TaskStateError(Exception):
    """Canonical task state could not be read or written. The database was not discarded."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class TaskStore:
    """Typed access to canonical task state. The database is opened on first use."""

    def __init__(
        self,
        path: Path,
        migrations: Sequence[Migration] = MIGRATIONS,
        clock: Clock = utc_now,
    ) -> None:
        self.path = path
        self._migrations = migrations
        self._clock = clock
        self._connection: sqlite3.Connection | None = None

    @classmethod
    def for_repository(cls, root: Path) -> TaskStore:
        return cls(project_state_dir(root) / TASKS_FILE)

    def exists(self) -> bool:
        """Whether any task state has been persisted. Checking does not create the database."""
        return self.path.is_file()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

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
            raise TaskStateError(f"cannot open task state at {self.path}: {error}") from error
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            migrate(connection, self._migrations)
        except (MigrationError, sqlite3.Error) as error:
            connection.close()
            raise TaskStateError(f"cannot use task state at {self.path}: {error}") from error
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        db = self._db
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as error:
            raise TaskStateError(f"cannot update task state: {error}") from error
        try:
            yield db
            db.execute("COMMIT")
        except sqlite3.Error as error:
            rollback(db)
            raise TaskStateError(f"cannot update task state: {error}") from error
        except BaseException:
            rollback(db)
            raise

    def _read(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        try:
            return self._db.execute(sql, params).fetchall()
        except sqlite3.Error as error:
            raise TaskStateError(f"cannot read task state: {error}") from error

    def _event(
        self,
        db: sqlite3.Connection,
        session_id: str,
        task_id: str | None,
        kind: str,
        subject_id: str,
        /,
        **detail: str,
    ) -> None:
        db.execute(
            "INSERT INTO events (session_id, task_id, kind, subject_id, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, task_id, kind, subject_id, json.dumps(detail), self._clock()),
        )

    def _check_session(self, db: sqlite3.Connection, task_id: str | None, session_id: str) -> None:
        if task_id is not None and self._session_of(db, task_id) != session_id:
            raise TaskStateError(f"task {task_id} belongs to another session")

    def _session_of(self, db: sqlite3.Connection, task_id: str) -> str:
        row = db.execute("SELECT session_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskStateError(f"unknown task: {task_id}")
        session_id: str = row[0]
        return session_id

    # -- Sessions ------------------------------------------------------------------------------

    def start_session(self, title: str = "") -> Session:
        session = Session(new_id("session"), title, True, self._clock())
        with self._write() as db:
            db.execute(
                "INSERT INTO sessions (id, title, status, created_at) VALUES (?, ?, 'open', ?)",
                (session.id, session.title, session.created_at),
            )
            self._event(db, session.id, None, "session_started", session.id, title=title)
        return session

    def close_session(self, session_id: str) -> None:
        with self._write() as db:
            if not changed(db, "UPDATE sessions SET status = 'closed' WHERE id = ?", session_id):
                raise TaskStateError(f"unknown session: {session_id}")
            self._event(db, session_id, None, "session_closed", session_id)

    def session(self, session_id: str) -> Session | None:
        rows = self._read("SELECT * FROM sessions WHERE id = ?", session_id)
        return to_session(rows[0]) if rows else None

    def latest_session(self) -> Session | None:
        """The most recent open session, or else the most recent session of any kind."""
        rows = self._read(
            "SELECT * FROM sessions ORDER BY status = 'open' DESC, rowid DESC LIMIT 1"
        )
        return to_session(rows[0]) if rows else None

    # -- Tasks ---------------------------------------------------------------------------------

    def create_task(
        self,
        session_id: str,
        goal: str,
        parent_id: str | None = None,
        role: str | None = None,
    ) -> Task:
        with self._write() as db:
            return self._insert_task(db, session_id, goal, parent_id, role)

    def _insert_task(
        self,
        db: sqlite3.Connection,
        session_id: str,
        goal: str,
        parent_id: str | None,
        role: str | None,
        status: TaskStatus = TaskStatus.PENDING,
    ) -> Task:
        now = self._clock()
        task = Task(new_id("task"), session_id, parent_id, goal, status, role, now, now)
        if parent_id is not None and self._session_of(db, parent_id) != session_id:
            raise TaskStateError(f"parent task {parent_id} belongs to another session")
        db.execute(
            "INSERT INTO tasks (id, session_id, parent_id, goal, status, role, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task.id, session_id, parent_id, goal, task.status, role, now, now),
        )
        self._event(
            db, session_id, task.id, "task_created", task.id, goal=goal, parent=parent_id or ""
        )
        return task

    def task(self, task_id: str) -> Task | None:
        rows = self._read("SELECT * FROM tasks WHERE id = ?", task_id)
        return to_task(rows[0]) if rows else None

    def tasks(self, session_id: str) -> list[Task]:
        """Every task in the session, in creation order."""
        rows = self._read("SELECT * FROM tasks WHERE session_id = ? ORDER BY rowid", session_id)
        return [to_task(row) for row in rows]

    def subtasks(self, task_id: str) -> list[Task]:
        rows = self._read("SELECT * FROM tasks WHERE parent_id = ? ORDER BY rowid", task_id)
        return [to_task(row) for row in rows]

    def root_task(self, task_id: str) -> str:
        """The ID of the root user task whose tree contains ``task_id`` (itself if a root).

        Budget scopes use it so decomposing a request never multiplies its per-task limit.
        """
        rows = self._read(
            "WITH RECURSIVE up (id, parent_id) AS ("
            "SELECT id, parent_id FROM tasks WHERE id = ? "
            "UNION SELECT t.id, t.parent_id FROM tasks t JOIN up ON t.id = up.parent_id) "
            "SELECT id FROM up WHERE parent_id IS NULL",
            task_id,
        )
        if not rows:
            raise TaskStateError(f"unknown task: {task_id}")
        root: str = rows[0][0]
        return root

    def record_budget_event(self, task_id: str, kind: str, subject_id: str, **detail: str) -> None:
        """Link a paid-spend decision to a task, as event ``budget_<kind>``.

        Concise only: the global spend ledger stays the authoritative financial record.
        """
        with self._write() as db:
            session_id = self._session_of(db, task_id)
            self._event(db, session_id, task_id, f"budget_{kind}", subject_id, **detail)

    def set_status(self, task_id: str, status: TaskStatus, reason: str = "") -> None:
        self._update_task(task_id, "status", status, "status_changed", reason=reason)

    def assign_role(self, task_id: str, role: str) -> None:
        self._update_task(task_id, "role", role, "role_assigned")

    def _update_task(self, task_id: str, column: str, value: str, kind: str, **detail: str) -> None:
        with self._write() as db:
            self._set_task(db, task_id, column, value, kind, **detail)

    def _set_task(
        self,
        db: sqlite3.Connection,
        task_id: str,
        column: str,
        value: str,
        kind: str,
        **detail: str,
    ) -> None:
        session_id = self._session_of(db, task_id)
        previous = db.execute(f"SELECT {column} FROM tasks WHERE id = ?", (task_id,)).fetchone()
        db.execute(
            f"UPDATE tasks SET {column} = ?, updated_at = ? WHERE id = ?",
            (value, self._clock(), task_id),
        )
        self._event(
            db, session_id, task_id, kind, task_id, old=previous[0] or "", new=value, **detail
        )

    def add_dependency(self, task_id: str, depends_on: str) -> None:
        with self._write() as db:
            self._insert_dependency(db, task_id, depends_on)

    def _insert_dependency(self, db: sqlite3.Connection, task_id: str, depends_on: str) -> None:
        if task_id == depends_on:
            raise TaskStateError(f"task {task_id} cannot depend on itself")
        session_id = self._session_of(db, task_id)
        if self._session_of(db, depends_on) != session_id:
            raise TaskStateError(f"task {depends_on} belongs to another session")
        db.execute(
            "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on, created_at) "
            "VALUES (?, ?, ?)",
            (task_id, depends_on, self._clock()),
        )
        self._event(db, session_id, task_id, "dependency_added", depends_on)

    def dependencies(self, task_id: str) -> list[str]:
        rows = self._read(
            "SELECT depends_on FROM task_dependencies WHERE task_id = ? ORDER BY rowid", task_id
        )
        return [row[0] for row in rows]

    # -- Requirements, assumptions, decisions --------------------------------------------------

    def add_requirement(
        self,
        session_id: str,
        text: str,
        source: RequirementSource = "user",
        task_id: str | None = None,
        supersedes: str | None = None,
    ) -> Requirement:
        """Record a requirement. Changing one supersedes it; the original intent is kept."""
        with self._write() as db:
            return self._insert_requirement(db, session_id, text, source, task_id, supersedes)

    def _insert_requirement(
        self,
        db: sqlite3.Connection,
        session_id: str,
        text: str,
        source: RequirementSource,
        task_id: str | None,
        supersedes: str | None = None,
    ) -> Requirement:
        requirement = Requirement(
            new_id("req"), session_id, task_id, text, source, self._clock(), None
        )
        self._check_session(db, task_id, session_id)
        db.execute(
            "INSERT INTO requirements (id, session_id, task_id, text, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (requirement.id, session_id, task_id, text, source, requirement.created_at),
        )
        if supersedes is not None:
            supersede(db, "requirements", supersedes, requirement.id, session_id)
        self._event(
            db,
            session_id,
            task_id,
            "requirement_added",
            requirement.id,
            source=source,
            supersedes=supersedes or "",
        )
        return requirement

    def requirements(self, session_id: str, current_only: bool = True) -> list[Requirement]:
        rows = self._read(
            "SELECT * FROM requirements WHERE session_id = ?"
            + (" AND superseded_by IS NULL" if current_only else "")
            + " ORDER BY rowid",
            session_id,
        )
        return [to_requirement(row) for row in rows]

    def add_assumption(self, task_id: str, text: str, made_by: str) -> Assumption:
        with self._write() as db:
            return self._insert_assumption(db, task_id, text, made_by)

    def _insert_assumption(
        self, db: sqlite3.Connection, task_id: str, text: str, made_by: str
    ) -> Assumption:
        assumption = Assumption(new_id("assume"), task_id, text, made_by, None, self._clock())
        session_id = self._session_of(db, task_id)
        db.execute(
            "INSERT INTO assumptions (id, task_id, text, made_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (assumption.id, task_id, text, made_by, assumption.created_at),
        )
        self._event(db, session_id, task_id, "assumption_added", assumption.id, by=made_by)
        return assumption

    def resolve_assumption(self, assumption_id: str, confirmed: bool) -> None:
        with self._write() as db:
            row = db.execute(
                "SELECT task_id FROM assumptions WHERE id = ?", (assumption_id,)
            ).fetchone()
            if row is None:
                raise TaskStateError(f"unknown assumption: {assumption_id}")
            db.execute(
                "UPDATE assumptions SET confirmed = ? WHERE id = ?", (int(confirmed), assumption_id)
            )
            kind = "assumption_confirmed" if confirmed else "assumption_rejected"
            self._event(db, self._session_of(db, row[0]), row[0], kind, assumption_id)

    def assumptions(self, task_id: str) -> list[Assumption]:
        rows = self._read("SELECT * FROM assumptions WHERE task_id = ? ORDER BY rowid", task_id)
        return [to_assumption(row) for row in rows]

    def record_decision(
        self,
        session_id: str,
        question: str,
        decision: str,
        decided_by: str,
        rationale: str = "",
        task_id: str | None = None,
        supersedes: str | None = None,
    ) -> Decision:
        with self._write() as db:
            return self._insert_decision(
                db, session_id, question, decision, decided_by, rationale, task_id, supersedes
            )

    def _insert_decision(
        self,
        db: sqlite3.Connection,
        session_id: str,
        question: str,
        decision: str,
        decided_by: str,
        rationale: str,
        task_id: str | None,
        supersedes: str | None = None,
    ) -> Decision:
        record = Decision(
            new_id("decision"),
            session_id,
            task_id,
            question,
            decision,
            decided_by,
            rationale,
            self._clock(),
            None,
        )
        self._check_session(db, task_id, session_id)
        db.execute(
            "INSERT INTO decisions (id, session_id, task_id, question, decision, decided_by, "
            "rationale, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                session_id,
                task_id,
                question,
                decision,
                decided_by,
                rationale,
                record.created_at,
            ),
        )
        if supersedes is not None:
            supersede(db, "decisions", supersedes, record.id, session_id)
        self._event(
            db,
            session_id,
            task_id,
            "decision_recorded",
            record.id,
            by=decided_by,
            supersedes=supersedes or "",
        )
        return record

    def decisions(self, session_id: str, current_only: bool = True) -> list[Decision]:
        rows = self._read(
            "SELECT * FROM decisions WHERE session_id = ?"
            + (" AND superseded_by IS NULL" if current_only else "")
            + " ORDER BY rowid",
            session_id,
        )
        return [to_decision(row) for row in rows]

    # -- Specs ---------------------------------------------------------------------------------

    def add_spec_version(self, task_id: str, body: str) -> SpecVersion:
        """Store a new spec version, awaiting approval. Earlier versions are never overwritten."""
        with self._write() as db:
            session_id = self._session_of(db, task_id)
            previous = db.execute(
                "SELECT version, body FROM spec_versions WHERE task_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            version = 1 if previous is None else previous["version"] + 1
            diff = "" if previous is None else spec_diff(previous["body"], body, version)
            spec = SpecVersion(task_id, version, body, diff, Approval.PENDING, self._clock())
            db.execute(
                "INSERT INTO spec_versions (task_id, version, body, diff, approval, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, version, body, diff, spec.approval, spec.created_at),
            )
            self._event(
                db, session_id, task_id, "spec_version_added", task_id, version=str(version)
            )
        return spec

    def set_spec_approval(self, task_id: str, version: int, approval: Approval) -> None:
        with self._write() as db:
            session_id = self._session_of(db, task_id)
            if not changed(
                db,
                "UPDATE spec_versions SET approval = ? WHERE task_id = ? AND version = ?",
                approval,
                task_id,
                version,
            ):
                raise TaskStateError(f"unknown spec version {version} for task {task_id}")
            self._event(
                db,
                session_id,
                task_id,
                "spec_approval_changed",
                task_id,
                version=str(version),
                approval=approval,
            )

    def void_spec_approvals(self, task_ids: Iterable[str], reason: str) -> list[str]:
        """Withdraw approval of the affected tasks' approved specs; return the tasks affected."""
        voided = []
        with self._write() as db:
            for task_id in task_ids:
                session_id = self._session_of(db, task_id)
                if changed(
                    db,
                    "UPDATE spec_versions SET approval = 'voided' "
                    "WHERE task_id = ? AND approval = 'approved'",
                    task_id,
                ):
                    voided.append(task_id)
                    self._event(
                        db, session_id, task_id, "spec_approval_voided", task_id, reason=reason
                    )
        return voided

    def spec_versions(self, task_id: str) -> list[SpecVersion]:
        rows = self._read("SELECT * FROM spec_versions WHERE task_id = ? ORDER BY version", task_id)
        return [to_spec(row) for row in rows]

    def latest_spec(self, task_id: str) -> SpecVersion | None:
        versions = self.spec_versions(task_id)
        return versions[-1] if versions else None

    # -- Execution pins ------------------------------------------------------------------------

    def pin(self, task_id: str, kind: PinKind, value: str, user_pinned: bool = True) -> Pin:
        """Constrain a task to a model, provider, or harness ID, replacing any earlier pin.

        The replaced pin is kept and linked to this one.
        """
        with self._write() as db:
            return self._insert_pin(db, task_id, kind, value, user_pinned)

    def _insert_pin(
        self, db: sqlite3.Connection, task_id: str, kind: PinKind, value: str, user_pinned: bool
    ) -> Pin:
        if kind not in PIN_KINDS:
            raise TaskStateError(f"unknown pin kind: {kind}")
        if not value:
            raise TaskStateError(f"empty {kind} pin")
        pin = Pin(new_id("pin"), task_id, kind, value, user_pinned, self._clock())
        session_id = self._session_of(db, task_id)
        db.execute(
            "INSERT INTO execution_pins (id, task_id, kind, value, user_pinned, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pin.id, task_id, kind, value, int(user_pinned), pin.created_at),
        )
        db.execute(
            "UPDATE execution_pins SET ended_at = ?, superseded_by = ? "
            "WHERE task_id = ? AND kind = ? AND ended_at IS NULL AND id <> ?",
            (pin.created_at, pin.id, task_id, kind, pin.id),
        )
        self._event(
            db,
            session_id,
            task_id,
            "pin_set",
            pin.id,
            kind=kind,
            value=value,
            user_pinned=str(user_pinned).lower(),
        )
        return pin

    def clear_pin(self, task_id: str, kind: PinKind) -> bool:
        """End the task's current ``kind`` pin, keeping its history. Returns whether one existed."""
        with self._write() as db:
            session_id = self._session_of(db, task_id)
            cleared = changed(
                db,
                "UPDATE execution_pins SET ended_at = ? "
                "WHERE task_id = ? AND kind = ? AND ended_at IS NULL",
                self._clock(),
                task_id,
                kind,
            )
            if cleared:
                self._event(db, session_id, task_id, "pin_cleared", task_id, kind=kind)
        return cleared

    def pins(self, task_id: str) -> dict[PinKind, Pin]:
        """The task's current pins, by kind. Pins are not inherited from parent tasks."""
        rows = self._read(
            "SELECT * FROM execution_pins WHERE task_id = ? AND ended_at IS NULL ORDER BY rowid",
            task_id,
        )
        return {pin.kind: pin for pin in map(to_pin, rows)}

    def pin_history(self, task_id: str) -> list[Pin]:
        rows = self._read("SELECT * FROM execution_pins WHERE task_id = ? ORDER BY rowid", task_id)
        return [to_pin(row) for row in rows]

    # -- Workers -------------------------------------------------------------------------------

    def record_worker(
        self,
        task_id: str,
        role: str,
        status: WorkerStatus,
        model: str | None = None,
        provider: str | None = None,
        harness: str | None = None,
        worktree: str | None = None,
    ) -> Worker:
        """Reference a worker. Its conversation is never stored, only its normalized state."""
        status = normalize_worker_status(status)
        now = self._clock()
        worker = Worker(
            new_id("worker"), task_id, role, model, provider, harness, worktree, status, now, now
        )
        with self._write() as db:
            session_id = self._session_of(db, task_id)
            db.execute(
                "INSERT INTO workers (id, task_id, role, model, provider, harness, worktree, "
                "status, created_at, heartbeat_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (worker.id, task_id, role, model, provider, harness, worktree, status, now, now),
            )
            self._event(db, session_id, task_id, "worker_recorded", worker.id, status=status)
        return worker

    def update_worker(self, worker_id: str, status: WorkerStatus | None = None) -> None:
        """Record a heartbeat and, optionally, a new status."""
        if status is not None:
            status = normalize_worker_status(status)
        with self._write() as db:
            row = db.execute(
                "SELECT task_id, status FROM workers WHERE id = ?", (worker_id,)
            ).fetchone()
            if row is None:
                raise TaskStateError(f"unknown worker: {worker_id}")
            db.execute(
                "UPDATE workers SET status = ?, heartbeat_at = ? WHERE id = ?",
                (status or row["status"], self._clock(), worker_id),
            )
            if status is not None and status != row["status"]:
                self._event(
                    db,
                    self._session_of(db, row["task_id"]),
                    row["task_id"],
                    "worker_status_changed",
                    worker_id,
                    old=row["status"],
                    new=status,
                )

    def workers(self, task_id: str) -> list[Worker]:
        rows = self._read("SELECT * FROM workers WHERE task_id = ? ORDER BY rowid", task_id)
        return [to_worker(row) for row in rows]

    # -- Findings, blockers, reviews, artifacts ------------------------------------------------

    def add_finding(
        self,
        task_id: str,
        source: str,
        severity: str,
        summary: str,
        requirement_id: str | None = None,
    ) -> Finding:
        finding = Finding(
            new_id("finding"),
            task_id,
            source,
            severity,
            summary,
            requirement_id,
            None,
            self._clock(),
        )
        with self._write() as db:
            session_id = self._session_of(db, task_id)
            if requirement_id is not None:
                linked = db.execute(
                    "SELECT session_id FROM requirements WHERE id = ?", (requirement_id,)
                ).fetchone()
                if linked is None or linked[0] != session_id:
                    raise TaskStateError(f"requirement {requirement_id} is not in this session")
            db.execute(
                "INSERT INTO findings (id, task_id, source, severity, summary, requirement_id, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    finding.id,
                    task_id,
                    source,
                    severity,
                    summary,
                    requirement_id,
                    finding.created_at,
                ),
            )
            self._event(db, session_id, task_id, "finding_added", finding.id, severity=severity)
        return finding

    def resolve_finding(self, finding_id: str, resolution: str) -> None:
        self._resolve("findings", finding_id, resolution, "finding_resolved")

    def findings(self, task_id: str, open_only: bool = False) -> list[Finding]:
        rows = self._read(
            "SELECT * FROM findings WHERE task_id = ?"
            + (" AND resolved_at IS NULL" if open_only else "")
            + " ORDER BY rowid",
            task_id,
        )
        return [to_finding(row) for row in rows]

    def add_blocker(self, task_id: str, reason: str, owner: str = "user") -> Blocker:
        with self._write() as db:
            return self._insert_blocker(db, task_id, reason, owner)

    def _insert_blocker(
        self, db: sqlite3.Connection, task_id: str, reason: str, owner: str
    ) -> Blocker:
        blocker = Blocker(new_id("blocker"), task_id, reason, owner, None, self._clock())
        session_id = self._session_of(db, task_id)
        db.execute(
            "INSERT INTO blockers (id, task_id, reason, owner, created_at) VALUES (?, ?, ?, ?, ?)",
            (blocker.id, task_id, reason, owner, blocker.created_at),
        )
        self._event(db, session_id, task_id, "blocker_added", blocker.id, owner=owner)
        return blocker

    def block_task(self, task_id: str, reason: str, owner: str = "user") -> Blocker:
        """Add a blocker and mark the task ``blocked``, together."""
        with self._write() as db:
            blocker = self._insert_blocker(db, task_id, reason, owner)
            self._set_task(
                db, task_id, "status", TaskStatus.BLOCKED, "status_changed", reason=reason
            )
        return blocker

    def resolve_blocker(self, blocker_id: str, resolution: str) -> None:
        self._resolve("blockers", blocker_id, resolution, "blocker_resolved")

    def blockers(self, session_id: str, open_only: bool = True) -> list[Blocker]:
        rows = self._read(
            "SELECT blockers.* FROM blockers JOIN tasks ON tasks.id = blockers.task_id "
            "WHERE tasks.session_id = ?"
            + (" AND blockers.resolved_at IS NULL" if open_only else "")
            + " ORDER BY blockers.rowid",
            session_id,
        )
        return [to_blocker(row) for row in rows]

    def _resolve(self, table: str, record_id: str, resolution: str, kind: str) -> None:
        with self._write() as db:
            self._resolve_in(db, table, record_id, resolution, kind)

    def _resolve_in(
        self, db: sqlite3.Connection, table: str, record_id: str, resolution: str, kind: str
    ) -> None:
        row = db.execute(f"SELECT task_id FROM {table} WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            raise TaskStateError(f"unknown {table[:-1]}: {record_id}")
        db.execute(
            f"UPDATE {table} SET resolution = ?, resolved_at = ? WHERE id = ?",
            (resolution, self._clock(), record_id),
        )
        self._event(db, self._session_of(db, row[0]), row[0], kind, record_id)

    def record_review(
        self,
        task_id: str,
        kind: ReviewKind,
        round: int,
        result: str,
        reviewer: str,
        spec_version: int | None = None,
    ) -> Review:
        review = Review(
            new_id("review"), task_id, kind, round, result, reviewer, spec_version, self._clock()
        )
        with self._write() as db:
            session_id = self._session_of(db, task_id)
            db.execute(
                "INSERT INTO reviews (id, task_id, kind, round, result, reviewer, spec_version, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review.id,
                    task_id,
                    kind,
                    round,
                    result,
                    reviewer,
                    spec_version,
                    review.created_at,
                ),
            )
            self._event(
                db, session_id, task_id, "review_recorded", review.id, kind=kind, result=result
            )
        return review

    def reviews(self, task_id: str) -> list[Review]:
        rows = self._read("SELECT * FROM reviews WHERE task_id = ? ORDER BY rowid", task_id)
        return [to_review(row) for row in rows]

    def add_artifact(
        self,
        task_id: str,
        kind: str,
        reference: str,
        metadata: Mapping[str, str] | None = None,
    ) -> Artifact:
        """Record an artifact by reference (for example a file path) plus small metadata."""
        artifact = Artifact(
            new_id("artifact"), task_id, kind, reference, dict(metadata or {}), self._clock()
        )
        with self._write() as db:
            session_id = self._session_of(db, task_id)
            db.execute(
                "INSERT INTO artifacts (id, task_id, kind, reference, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    artifact.id,
                    task_id,
                    kind,
                    reference,
                    json.dumps(artifact.metadata),
                    artifact.created_at,
                ),
            )
            self._event(db, session_id, task_id, "artifact_added", artifact.id, kind=kind)
        return artifact

    def artifacts(self, task_id: str) -> list[Artifact]:
        rows = self._read("SELECT * FROM artifacts WHERE task_id = ? ORDER BY rowid", task_id)
        return [to_artifact(row) for row in rows]

    # -- Skill routing -------------------------------------------------------------------------

    def record_skill_routing(self, routing: SkillRouting) -> SkillRouting:
        """Append one routing decision; see ``record_skill_routings``."""
        return self.record_skill_routings([routing])[0]

    def record_skill_routings(self, routings: Sequence[SkillRouting]) -> list[SkillRouting]:
        """Append routing decisions in one transaction: all are recorded, or none are.

        The store assigns ``id`` and ``created_at``. Routing never changes a task's role: a
        decision for a role other than the task's assigned role, or the role it was first routed
        as, is refused. An escalation must link to an earlier decision for the same task; an
        initial decision must not link to one.
        """
        stamped = [
            replace(routing, id=new_id("routing"), created_at=self._clock()) for routing in routings
        ]
        with self._write() as db:
            for routing in stamped:
                session_id = self._session_of(db, routing.task_id)
                self._check_routing(db, routing)
                db.execute(
                    "INSERT INTO skill_routings (id, task_id, kind, role, policy_version, "
                    "classifier, classifier_mode, classifier_input_version, estimator, "
                    "budget_tokens, total_tokens, signals, selected, dropped, rejected_signals, "
                    "reason, previous_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        routing.id,
                        routing.task_id,
                        routing.kind,
                        routing.role,
                        routing.policy_version,
                        routing.classifier,
                        routing.classifier_mode,
                        routing.classifier_input_version,
                        routing.estimator,
                        routing.budget_tokens,
                        routing.total_tokens,
                        json.dumps([asdict(item) for item in routing.signals]),
                        json.dumps([asdict(item) for item in routing.selected]),
                        json.dumps([asdict(item) for item in routing.dropped]),
                        json.dumps(list(routing.rejected_signals)),
                        routing.reason,
                        routing.previous_id,
                        routing.created_at,
                    ),
                )
                self._event(
                    db,
                    session_id,
                    routing.task_id,
                    "skills_routed",
                    routing.id,
                    kind=routing.kind,
                    skills=",".join(item.name for item in routing.selected),
                )
        return stamped

    @staticmethod
    def _check_routing(db: sqlite3.Connection, routing: SkillRouting) -> None:
        task = routing.task_id
        assigned = db.execute("SELECT role FROM tasks WHERE id = ?", (task,)).fetchone()[0]
        first = db.execute(
            "SELECT role FROM skill_routings WHERE task_id = ? ORDER BY rowid LIMIT 1", (task,)
        ).fetchone()
        for role in (assigned, None if first is None else first[0]):
            if role is not None and role != routing.role:
                raise TaskStateError(
                    f"task {task} has role {role}; cannot route it as {routing.role}"
                )
        if (routing.kind == "escalation") != (routing.previous_id is not None):
            raise TaskStateError(
                "an escalation must link to the decision it extends, and only an escalation may"
            )
        if routing.previous_id is not None:
            previous = db.execute(
                "SELECT task_id FROM skill_routings WHERE id = ?", (routing.previous_id,)
            ).fetchone()
            if previous is None or previous[0] != task:
                raise TaskStateError(f"routing {routing.previous_id} is not a decision for {task}")

    def skill_routings(self, task_id: str) -> list[SkillRouting]:
        """Every routing decision for the task, oldest first."""
        rows = self._read("SELECT * FROM skill_routings WHERE task_id = ? ORDER BY rowid", task_id)
        return [to_skill_routing(row) for row in rows]

    def latest_skill_routing(self, task_id: str) -> SkillRouting | None:
        rows = self._read(
            "SELECT * FROM skill_routings WHERE task_id = ? ORDER BY rowid DESC LIMIT 1", task_id
        )
        return to_skill_routing(rows[0]) if rows else None

    def latest_skill_routings(self, session_id: str) -> dict[str, SkillRouting]:
        """Each routed task's latest decision in the session, in task creation order."""
        rows = self._read(
            "SELECT skill_routings.* FROM skill_routings "
            "JOIN tasks ON tasks.id = skill_routings.task_id "
            "WHERE tasks.session_id = ? AND skill_routings.rowid IN "
            "(SELECT MAX(rowid) FROM skill_routings GROUP BY task_id) "
            "ORDER BY tasks.rowid",
            session_id,
        )
        return {row["task_id"]: to_skill_routing(row) for row in rows}

    # -- Planning ------------------------------------------------------------------------------

    def start_root_task(
        self, session_id: str, goal: str, role: str = "orchestrator"
    ) -> tuple[Task, Requirement]:
        """Create a root task for a user request, ``in_progress``, with the literal request
        recorded as its user requirement."""
        with self._write() as db:
            task = self._insert_task(db, session_id, goal, None, role, TaskStatus.IN_PROGRESS)
            requirement = self._insert_requirement(db, session_id, goal, "user", task.id)
        return task, requirement

    def task_requirements(self, task_id: str) -> list[Requirement]:
        """The task's current requirements, oldest first."""
        rows = self._read(
            "SELECT * FROM requirements WHERE task_id = ? AND superseded_by IS NULL ORDER BY rowid",
            task_id,
        )
        return [to_requirement(row) for row in rows]

    def task_decisions(self, task_id: str) -> list[Decision]:
        """The task's current decisions, oldest first."""
        rows = self._read(
            "SELECT * FROM decisions WHERE task_id = ? AND superseded_by IS NULL ORDER BY rowid",
            task_id,
        )
        return [to_decision(row) for row in rows]

    def pin_by_user(self, task_id: str, kind: PinKind, value: str, instruction: str) -> Pin:
        """Pin a task as the user instructed, recording the instruction as a user requirement."""
        with self._write() as db:
            self._instruction(db, task_id, instruction)
            return self._insert_pin(db, task_id, kind, value, True)

    def _instruction(self, db: sqlite3.Connection, task_id: str, text: str) -> str:
        """The ID of the task's current user requirement with exactly ``text``, recorded now if
        there is none, so one instruction behind several pins is recorded once."""
        row = db.execute(
            "SELECT id FROM requirements WHERE task_id = ? AND text = ? AND source = 'user' "
            "AND superseded_by IS NULL",
            (task_id, text),
        ).fetchone()
        if row is not None:
            existing: str = row[0]
            return existing
        return self._insert_requirement(db, self._session_of(db, task_id), text, "user", task_id).id

    def persist_plan(self, plan: PlanWrite) -> dict[str, str]:
        """Write a validated child graph in one transaction; return each task key's new ID.

        Children, roles, dependencies, assumptions, and user pins are committed together or not
        at all. Pins scoped to ``all`` or a role are copied onto every matching child and kept as
        scoped pins for children created later. Pins apply from least to most specific (``all``,
        then a role, then one task), so the most specific pin of a kind wins on each child. A
        root task's plan is written only once.
        """
        root_id = plan.root_task_id
        with self._write() as db:
            row = db.execute(
                "SELECT session_id, parent_id FROM tasks WHERE id = ?", (root_id,)
            ).fetchone()
            if row is None:
                raise TaskStateError(f"unknown task: {root_id}")
            if row["parent_id"] is not None:
                raise TaskStateError(f"task {root_id} is not a root task")
            if db.execute("SELECT 1 FROM tasks WHERE parent_id = ?", (root_id,)).fetchone():
                raise TaskStateError(f"task {root_id} already has a persisted plan")
            session_id: str = row["session_id"]

            ids: dict[str, str] = {}
            roles: dict[str, str] = {}
            for planned in plan.tasks:
                if planned.key in ids:
                    raise TaskStateError(f"duplicate planned task key: {planned.key}")
                task = self._insert_task(db, session_id, planned.goal, root_id, planned.role)
                ids[planned.key] = task.id
                roles[task.id] = planned.role
            for planned in plan.tasks:
                for key in planned.depends_on:
                    if key not in ids:
                        raise TaskStateError(f"{planned.key} depends on unknown task key: {key}")
                    self._insert_dependency(db, ids[planned.key], ids[key])
            for text in plan.assumptions:
                self._insert_assumption(db, root_id, text, plan.made_by)

            def specificity(pin: PlannedPin) -> int:
                return 2 if pin.scope in ids else 0 if pin.scope == ALL_CHILDREN else 1

            for pin in sorted(plan.pins, key=specificity):
                requirement_id = self._instruction(db, root_id, pin.evidence)
                if pin.scope in ids:
                    targets = [ids[pin.scope]]
                else:
                    if pin.scope != ALL_CHILDREN and pin.scope not in CANONICAL_ROLES:
                        raise TaskStateError(f"unknown pin scope: {pin.scope}")
                    self._insert_scoped_pin(db, root_id, pin, requirement_id)
                    targets = [
                        task_id
                        for task_id, role in roles.items()
                        if pin.scope in (ALL_CHILDREN, role)
                    ]
                for task_id in targets:
                    self._insert_pin(db, task_id, pin.kind, pin.value, True)
            self._event(
                db, session_id, root_id, "plan_persisted", root_id, tasks=str(len(plan.tasks))
            )
        return ids

    def _insert_scoped_pin(
        self, db: sqlite3.Connection, root_id: str, pin: PlannedPin, requirement_id: str
    ) -> None:
        if pin.kind not in PIN_KINDS:
            raise TaskStateError(f"unknown pin kind: {pin.kind}")
        if not pin.value:
            raise TaskStateError(f"empty {pin.kind} pin")
        scoped_id = new_id("scoped-pin")
        db.execute(
            "INSERT INTO scoped_pins (id, root_task_id, scope, kind, value, requirement_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (scoped_id, root_id, pin.scope, pin.kind, pin.value, requirement_id, self._clock()),
        )
        self._event(
            db,
            self._session_of(db, root_id),
            root_id,
            "scoped_pin_set",
            scoped_id,
            scope=pin.scope,
            kind=pin.kind,
            value=pin.value,
        )

    def scoped_pins(self, root_task_id: str) -> list[ScopedPin]:
        rows = self._read(
            "SELECT * FROM scoped_pins WHERE root_task_id = ? ORDER BY rowid", root_task_id
        )
        return [to_scoped_pin(row) for row in rows]

    def ask_planning_question(self, task_id: str, question: str, why: str) -> PlanningQuestion:
        """Block the task on a question for the user. At most one may be pending per task."""
        with self._write() as db:
            pending = db.execute(
                "SELECT id FROM planning_questions WHERE task_id = ? AND status = 'pending'",
                (task_id,),
            ).fetchone()
            if pending is not None:
                raise TaskStateError(f"task {task_id} already has pending question {pending[0]}")
            blocker = self._insert_blocker(
                db, task_id, f"Waiting for your answer: {question}", "user"
            )
            record = PlanningQuestion(
                new_id("question"),
                task_id,
                question,
                why,
                "pending",
                None,
                None,
                blocker.id,
                self._clock(),
                None,
            )
            db.execute(
                "INSERT INTO planning_questions (id, task_id, question, why, status, blocker_id, "
                "created_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (record.id, task_id, question, why, blocker.id, record.created_at),
            )
            self._set_task(
                db, task_id, "status", TaskStatus.BLOCKED, "status_changed", reason="question"
            )
            self._event(db, self._session_of(db, task_id), task_id, "question_asked", record.id)
        return record

    def answer_planning_question(self, question_id: str, answer: str) -> Decision:
        """Record the user's answer as a user decision, resolve the question's blocker, and put
        the task back ``in_progress``, all together."""
        if not answer.strip():
            raise TaskStateError("an answer cannot be empty")
        with self._write() as db:
            row = db.execute(
                "SELECT * FROM planning_questions WHERE id = ?", (question_id,)
            ).fetchone()
            if row is None:
                raise TaskStateError(f"unknown planning question: {question_id}")
            if row["status"] != "pending":
                raise TaskStateError(f"planning question {question_id} is {row['status']}")
            task_id: str = row["task_id"]
            session_id = self._session_of(db, task_id)
            decision = self._insert_decision(
                db, session_id, row["question"], answer, "user", row["why"], task_id
            )
            db.execute(
                "UPDATE planning_questions SET status = 'answered', answer = ?, decision_id = ?, "
                "answered_at = ? WHERE id = ?",
                (answer, decision.id, self._clock(), question_id),
            )
            if row["blocker_id"] is not None:
                self._resolve_in(db, "blockers", row["blocker_id"], "answered", "blocker_resolved")
            self._set_task(
                db, task_id, "status", TaskStatus.IN_PROGRESS, "status_changed", reason="answered"
            )
            self._event(db, session_id, task_id, "question_answered", question_id)
        return decision

    def cancel_planning_question(self, question_id: str) -> None:
        """Withdraw a pending question: its blocker is resolved and its task is canceled."""
        with self._write() as db:
            row = db.execute(
                "SELECT * FROM planning_questions WHERE id = ?", (question_id,)
            ).fetchone()
            if row is None:
                raise TaskStateError(f"unknown planning question: {question_id}")
            if row["status"] != "pending":
                raise TaskStateError(f"planning question {question_id} is {row['status']}")
            db.execute(
                "UPDATE planning_questions SET status = 'canceled', answered_at = ? WHERE id = ?",
                (self._clock(), question_id),
            )
            if row["blocker_id"] is not None:
                self._resolve_in(db, "blockers", row["blocker_id"], "canceled", "blocker_resolved")
            self._set_task(
                db,
                row["task_id"],
                "status",
                TaskStatus.CANCELED,
                "status_changed",
                reason="canceled",
            )
            self._event(
                db,
                self._session_of(db, row["task_id"]),
                row["task_id"],
                "question_canceled",
                question_id,
            )

    def planning_questions(self, task_id: str) -> list[PlanningQuestion]:
        rows = self._read(
            "SELECT * FROM planning_questions WHERE task_id = ? ORDER BY rowid", task_id
        )
        return [to_planning_question(row) for row in rows]

    def pending_planning_question(self, session_id: str) -> PlanningQuestion | None:
        """The session's most recent pending planning question, if any."""
        rows = self._read(
            "SELECT planning_questions.* FROM planning_questions "
            "JOIN tasks ON tasks.id = planning_questions.task_id "
            "WHERE tasks.session_id = ? AND planning_questions.status = 'pending' "
            "ORDER BY planning_questions.rowid DESC LIMIT 1",
            session_id,
        )
        return to_planning_question(rows[0]) if rows else None

    # -- Events --------------------------------------------------------------------------------

    def events(self, session_id: str, task_id: str | None = None) -> list[Event]:
        """The session's history, oldest first; only the task's events when ``task_id`` is set."""
        if task_id is None:
            rows = self._read("SELECT * FROM events WHERE session_id = ? ORDER BY seq", session_id)
        else:
            rows = self._read(
                "SELECT * FROM events WHERE session_id = ? AND task_id = ? ORDER BY seq",
                session_id,
                task_id,
            )
        return [to_event(row) for row in rows]


def rollback(db: sqlite3.Connection) -> None:
    if db.in_transaction:
        db.execute("ROLLBACK")


def normalize_worker_status(status: WorkerStatus) -> WorkerStatus:
    try:
        return WorkerStatus(status)
    except ValueError as error:
        raise TaskStateError(f"unknown worker status: {status}") from error


def changed(db: sqlite3.Connection, sql: str, *params: Any) -> bool:
    return db.execute(sql, params).rowcount > 0


def supersede(
    db: sqlite3.Connection, table: str, old_id: str, new_id: str, session_id: str
) -> None:
    row = db.execute(
        f"SELECT superseded_by, session_id FROM {table} WHERE id = ?", (old_id,)
    ).fetchone()
    if row is None:
        raise TaskStateError(f"unknown {table[:-1]}: {old_id}")
    if row["session_id"] != session_id:
        raise TaskStateError(f"{table[:-1]} {old_id} belongs to another session")
    if row["superseded_by"] is not None:
        raise TaskStateError(
            f"{table[:-1]} {old_id} was already superseded by {row['superseded_by']}"
        )
    db.execute(f"UPDATE {table} SET superseded_by = ? WHERE id = ?", (new_id, old_id))


def spec_diff(old: str, new: str, version: int) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"spec v{version - 1}",
        tofile=f"spec v{version}",
    )
    return "".join(lines)


def record[R](cls: type[R], row: sqlite3.Row, **overrides: Any) -> R:
    """Build ``cls`` from its own columns only, so later migrations may add columns safely."""
    names = [f.name for f in fields(cast(Any, cls))]
    return cls(**{name: overrides[name] if name in overrides else row[name] for name in names})


def to_session(row: sqlite3.Row) -> Session:
    return Session(row["id"], row["title"], row["status"] == "open", row["created_at"])


def to_task(row: sqlite3.Row) -> Task:
    return record(Task, row, status=TaskStatus(row["status"]))


def to_requirement(row: sqlite3.Row) -> Requirement:
    return record(Requirement, row)


def to_assumption(row: sqlite3.Row) -> Assumption:
    confirmed = None if row["confirmed"] is None else bool(row["confirmed"])
    return record(Assumption, row, confirmed=confirmed)


def to_decision(row: sqlite3.Row) -> Decision:
    return record(Decision, row)


def to_spec(row: sqlite3.Row) -> SpecVersion:
    return record(SpecVersion, row, approval=Approval(row["approval"]))


def to_pin(row: sqlite3.Row) -> Pin:
    return record(Pin, row, kind=cast(PinKind, row["kind"]), user_pinned=bool(row["user_pinned"]))


def to_scoped_pin(row: sqlite3.Row) -> ScopedPin:
    return record(ScopedPin, row, kind=cast(PinKind, row["kind"]))


def to_planning_question(row: sqlite3.Row) -> PlanningQuestion:
    return record(PlanningQuestion, row)


def to_worker(row: sqlite3.Row) -> Worker:
    return record(Worker, row, status=WorkerStatus(row["status"]))


def to_finding(row: sqlite3.Row) -> Finding:
    return record(Finding, row)


def to_blocker(row: sqlite3.Row) -> Blocker:
    return record(Blocker, row)


def to_review(row: sqlite3.Row) -> Review:
    return record(Review, row)


def to_artifact(row: sqlite3.Row) -> Artifact:
    return record(Artifact, row, metadata=json.loads(row["metadata"]))


def to_event(row: sqlite3.Row) -> Event:
    return record(Event, row, detail=json.loads(row["detail"]))


def to_skill_routing(row: sqlite3.Row) -> SkillRouting:
    def skills(column: str) -> tuple[RoutedSkill, ...]:
        return tuple(RoutedSkill(**item) for item in json.loads(row[column]))

    signals = tuple(
        RoutedSignal(**{**item, "evidence": tuple(item["evidence"])})
        for item in json.loads(row["signals"])
    )
    return record(
        SkillRouting,
        row,
        signals=signals,
        selected=skills("selected"),
        dropped=skills("dropped"),
        rejected_signals=tuple(json.loads(row["rejected_signals"])),
    )

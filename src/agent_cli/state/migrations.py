"""Schema migrations for the canonical task-state database.

The schema version lives in SQLite's ``user_version`` header field. Each migration is a small,
deterministic list of statements that moves the schema up exactly one version; all pending
migrations run in one transaction, so a failure leaves the database exactly as it was.

Never edit a released migration. Add a new one instead: canonical task state must survive every
upgrade, and a newer schema is never created by dropping an older one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

type Migration = Sequence[str]

V1 = (
    """
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions (id),
        parent_id TEXT REFERENCES tasks (id),
        goal TEXT NOT NULL,
        status TEXT NOT NULL,
        role TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX tasks_by_session ON tasks (session_id)",
    "CREATE INDEX tasks_by_parent ON tasks (parent_id)",
    """
    CREATE TABLE task_dependencies (
        task_id TEXT NOT NULL REFERENCES tasks (id),
        depends_on TEXT NOT NULL REFERENCES tasks (id),
        created_at TEXT NOT NULL,
        PRIMARY KEY (task_id, depends_on),
        CHECK (task_id <> depends_on)
    )
    """,
    """
    CREATE TABLE requirements (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions (id),
        task_id TEXT REFERENCES tasks (id),
        text TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('user', 'derived')),
        created_at TEXT NOT NULL,
        superseded_by TEXT REFERENCES requirements (id)
    )
    """,
    """
    CREATE TABLE assumptions (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks (id),
        text TEXT NOT NULL,
        made_by TEXT NOT NULL,
        confirmed INTEGER CHECK (confirmed IN (0, 1)),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE decisions (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions (id),
        task_id TEXT REFERENCES tasks (id),
        question TEXT NOT NULL,
        decision TEXT NOT NULL,
        decided_by TEXT NOT NULL,
        rationale TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        superseded_by TEXT REFERENCES decisions (id)
    )
    """,
    """
    CREATE TABLE spec_versions (
        task_id TEXT NOT NULL REFERENCES tasks (id),
        version INTEGER NOT NULL,
        body TEXT NOT NULL,
        diff TEXT NOT NULL,
        approval TEXT NOT NULL CHECK (approval IN ('pending', 'approved', 'rejected', 'voided')),
        created_at TEXT NOT NULL,
        PRIMARY KEY (task_id, version)
    )
    """,
    """
    CREATE TABLE execution_pins (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks (id),
        kind TEXT NOT NULL CHECK (kind IN ('model', 'provider', 'harness')),
        value TEXT NOT NULL,
        user_pinned INTEGER NOT NULL CHECK (user_pinned IN (0, 1)),
        created_at TEXT NOT NULL,
        ended_at TEXT,
        superseded_by TEXT REFERENCES execution_pins (id)
    )
    """,
    "CREATE INDEX pins_by_task ON execution_pins (task_id, kind)",
    """
    CREATE TABLE workers (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks (id),
        role TEXT NOT NULL,
        model TEXT,
        provider TEXT,
        harness TEXT,
        worktree TEXT,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE findings (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks (id),
        source TEXT NOT NULL,
        severity TEXT NOT NULL,
        summary TEXT NOT NULL,
        requirement_id TEXT REFERENCES requirements (id),
        resolution TEXT,
        created_at TEXT NOT NULL,
        resolved_at TEXT
    )
    """,
    """
    CREATE TABLE blockers (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks (id),
        reason TEXT NOT NULL,
        owner TEXT NOT NULL,
        resolution TEXT,
        created_at TEXT NOT NULL,
        resolved_at TEXT
    )
    """,
    """
    CREATE TABLE reviews (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks (id),
        kind TEXT NOT NULL CHECK (kind IN ('spec', 'code')),
        round INTEGER NOT NULL,
        result TEXT NOT NULL,
        reviewer TEXT NOT NULL,
        spec_version INTEGER,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE artifacts (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks (id),
        kind TEXT NOT NULL,
        reference TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions (id),
        task_id TEXT REFERENCES tasks (id),
        kind TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX events_by_task ON events (task_id)",
)

MIGRATIONS: tuple[Migration, ...] = (V1,)
"""``MIGRATIONS[n]`` upgrades the schema from version ``n`` to ``n + 1``."""


class MigrationError(Exception):
    """The database could not be brought to the current schema. It was left unchanged."""


def schema_version(connection: sqlite3.Connection) -> int:
    version: int = connection.execute("PRAGMA user_version").fetchone()[0]
    return version


def migrate(connection: sqlite3.Connection, migrations: Sequence[Migration] = MIGRATIONS) -> int:
    """Bring the schema to ``len(migrations)``; return the version the database started at.

    ``connection`` must be in autocommit mode (``isolation_level=None``) so this function
    controls the transaction.
    """
    current = schema_version(connection)
    latest = len(migrations)
    if current > latest:
        raise MigrationError(
            f"schema version {current} is newer than this Agent CLI supports ({latest}); "
            "upgrade Agent CLI to use this task state"
        )
    if current == 0 and has_tables(connection):
        raise MigrationError("not an Agent CLI task-state database")
    if current == latest:
        return current
    connection.execute("BEGIN IMMEDIATE")
    try:
        for version in range(current, latest):
            for statement in migrations[version]:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {version + 1}")
    except sqlite3.Error as error:
        connection.execute("ROLLBACK")
        raise MigrationError(
            f"migration to schema version {version + 1} failed ({error}); "
            "the database was left unchanged"
        ) from error
    connection.execute("COMMIT")
    return current


def has_tables(connection: sqlite3.Connection) -> bool:
    row = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone()
    return row is not None

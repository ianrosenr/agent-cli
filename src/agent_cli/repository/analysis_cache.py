"""The global content-analysis cache: ``~/.agent-cli/cache/repository-analysis.sqlite``.

Results are keyed by ``blob ID + analyzer name + analyzer version`` and hold no paths, so
identical content is analyzed once and reused across branches, worktrees, repositories, and
sessions. This is a cache: if it is corrupt or has an unknown schema it is discarded and rebuilt,
and if it cannot be used right now (for example another process holds a lock), lookups miss and
writes are skipped rather than failing the caller.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agent_cli.repository.analyzers import Analysis
from agent_cli.state.paths import ensure_private_dir, global_cache_dir

CACHE_FILE = "repository-analysis.sqlite"
CACHE_SCHEMA = 1
BUSY_TIMEOUT = 5.0
"""Seconds to wait for another process's lock before treating the cache as unavailable."""

SCHEMA = """
CREATE TABLE analyses (
    blob TEXT NOT NULL,
    analyzer TEXT NOT NULL,
    analyzer_version TEXT NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (blob, analyzer, analyzer_version)
) WITHOUT ROWID
"""


class AnalysisCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None

    @classmethod
    def default(cls) -> AnalysisCache:
        return cls(global_cache_dir() / CACHE_FILE)

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            ensure_private_dir(self.path.parent)
            try:
                self._connection = open_cache(self.path)
            except sqlite3.OperationalError:
                raise  # locked or unreadable: another process may be using it
            except sqlite3.DatabaseError:
                discard(self.path)
                self._connection = open_cache(self.path)
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def get(self, blob: str, analyzer: str, version: str) -> Analysis | None:
        """The cached analysis, or ``None`` on a miss or when the cache is unusable."""
        try:
            row = self._db.execute(
                "SELECT result FROM analyses "
                "WHERE blob = ? AND analyzer = ? AND analyzer_version = ?",
                (blob, analyzer, version),
            ).fetchone()
            return None if row is None else Analysis.from_json(row[0])
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            return None

    def put(self, blob: str, analyzer: str, version: str, analysis: Analysis) -> None:
        """Store an analysis; skipped silently when the cache is unusable."""
        try:
            with self._db as db:
                db.execute(
                    "INSERT OR REPLACE INTO analyses (blob, analyzer, analyzer_version, result) "
                    "VALUES (?, ?, ?, ?)",
                    (blob, analyzer, version, analysis.to_json()),
                )
        except (OSError, sqlite3.Error):
            pass


def open_cache(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'analyses'"
        ).fetchone()
        if version != CACHE_SCHEMA or table is None:
            connection.execute("DROP TABLE IF EXISTS analyses")
            connection.execute(SCHEMA)
            connection.execute(f"PRAGMA user_version = {CACHE_SCHEMA}")
            connection.commit()
    except sqlite3.DatabaseError:
        connection.close()
        raise
    return connection


def discard(path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)

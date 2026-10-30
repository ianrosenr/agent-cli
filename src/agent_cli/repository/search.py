"""Lexical search over indexed file text: SQLite FTS5, or a plain-SQL fallback without it.

The fallback is deterministic substring matching. Search never falls back to model-based or
semantic retrieval.
"""

from __future__ import annotations

import sqlite3

FTS5 = "fts5"
PLAIN = "plain"

FTS5_TABLE = "CREATE VIRTUAL TABLE search USING fts5(path UNINDEXED, symbols, body)"
PLAIN_TABLE = "CREATE TABLE search (path TEXT PRIMARY KEY, symbols TEXT NOT NULL, body TEXT)"


def fts5_available(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("CREATE VIRTUAL TABLE temp.probe USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    connection.execute("DROP TABLE temp.probe")
    return True


def phrase(term: str) -> str:
    """An FTS5 phrase matching ``term`` literally, so operators in it have no effect."""
    return '"' + term.replace('"', '""') + '"'


def search(connection: sqlite3.Connection, mode: str, term: str, limit: int) -> list[str]:
    """Paths whose text or symbol names contain ``term``, best matches first."""
    if not term.strip():
        return []
    if mode == FTS5:
        try:
            rows = connection.execute(
                "SELECT path FROM search WHERE search MATCH ? ORDER BY rank LIMIT ?",
                (phrase(term), limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    else:
        rows = connection.execute(
            "SELECT path FROM search WHERE instr(lower(symbols), lower(?)) "
            "OR instr(lower(body), lower(?)) ORDER BY instr(lower(symbols), lower(?)) = 0, path "
            "LIMIT ?",
            (term, term, term, limit),
        ).fetchall()
    return [row[0] for row in rows]

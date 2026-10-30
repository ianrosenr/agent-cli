"""The persistent repository fact index: ``<repo>/.agent-cli/state/repository.sqlite``.

This is derived state. It maps each current path to the blob ID of its working-tree content
and stores normalized, evidence-backed facts: classifications, symbols, imports, simple
relationships, and lexical search text. It is rebuilt when its schema or classifier changes
and is never canonical task state (see ``agent_cli.state``).

A refresh is incremental. Paths whose blob ID and analyzer version are unchanged are skipped;
changed paths reuse the global analysis cache when the same content was analyzed before; only
uncached content is analyzed. Relationships are re-derived from stored rows, never by
re-analyzing files.
"""

from __future__ import annotations

import functools
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath

from agent_cli.repository.analysis_cache import AnalysisCache, discard
from agent_cli.repository.analyzers import Analysis, AnalyzerSet
from agent_cli.repository.classify import (
    CLASSIFIER_VERSION,
    GENERATED,
    SOURCE,
    TEST,
    classify_path,
    import_signals,
    language_of,
)
from agent_cli.repository.git import FileEntry, GitWorkspace, RepositoryError, blob_id
from agent_cli.repository.python_analyzer import PythonAnalyzer
from agent_cli.repository.search import FTS5, FTS5_TABLE, PLAIN, PLAIN_TABLE, fts5_available
from agent_cli.repository.search import search as search_text
from agent_cli.state.paths import ensure_private_dir, project_state_dir

INDEX_FILE = "repository.sqlite"
INDEX_SCHEMA = 1
BUSY_TIMEOUT = 5.0
"""Seconds to wait for another process's lock before failing."""
MAX_FILE_BYTES = 1024 * 1024
"""Larger files keep their path facts but are neither analyzed nor searched."""
BINARY_PROBE = 8192
TEST_NAME_MATCHES = 5

SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """
    CREATE TABLE files (
        path TEXT PRIMARY KEY,
        blob TEXT NOT NULL,
        language TEXT,
        module TEXT,
        analyzer TEXT,
        analyzer_version TEXT,
        analysis TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX files_by_module ON files (module)",
    """
    CREATE TABLE facts (
        path TEXT NOT NULL REFERENCES files (path) ON DELETE CASCADE,
        fact TEXT NOT NULL,
        source TEXT NOT NULL,
        evidence TEXT NOT NULL,
        PRIMARY KEY (path, fact)
    )
    """,
    "CREATE INDEX facts_by_fact ON facts (fact)",
    """
    CREATE TABLE symbols (
        path TEXT NOT NULL REFERENCES files (path) ON DELETE CASCADE,
        name TEXT NOT NULL,
        qualname TEXT NOT NULL,
        kind TEXT NOT NULL,
        line INTEGER NOT NULL,
        end_line INTEGER NOT NULL,
        exported INTEGER NOT NULL
    )
    """,
    "CREATE INDEX symbols_by_path ON symbols (path)",
    "CREATE INDEX symbols_by_name ON symbols (name)",
    "CREATE INDEX symbols_by_qualname ON symbols (qualname)",
    """
    CREATE TABLE imports (
        path TEXT NOT NULL REFERENCES files (path) ON DELETE CASCADE,
        module TEXT NOT NULL,
        names TEXT NOT NULL,
        level INTEGER NOT NULL,
        line INTEGER NOT NULL
    )
    """,
    "CREATE INDEX imports_by_path ON imports (path)",
    """
    CREATE TABLE relations (
        src TEXT NOT NULL,
        kind TEXT NOT NULL,
        dst TEXT NOT NULL,
        evidence TEXT NOT NULL,
        PRIMARY KEY (src, kind, dst)
    )
    """,
    "CREATE INDEX relations_by_dst ON relations (dst, kind)",
)


class FactSource(StrEnum):
    """How a repository fact was learned. Structural evidence outranks semantic inference."""

    STRUCTURAL = "structural"
    """Derived directly from Git, paths, manifests, parser output, or exact search."""
    SEMANTIC = "semantic"
    """Reserved: inferred by a classifier or model."""
    WORKER_DISCOVERED = "worker_discovered"
    """Reserved: discovered by a worker during task execution."""


class AnalysisState(StrEnum):
    ANALYZED = "analyzed"
    FAILED = "failed"
    """An analyzer ran but could not extract structure (for example a syntax error)."""
    UNSUPPORTED = "unsupported"
    """No analyzer handles this file type; path and lexical facts still apply."""
    SKIPPED = "skipped"
    """Generated, vendored, binary, or too large: path facts only."""


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    blob: str
    language: str | None
    module: str | None
    analyzer: str | None
    """``name@version`` of the analyzer responsible for this file, if any."""
    analysis: AnalysisState
    detail: str


@dataclass(frozen=True, slots=True)
class Fact:
    path: str
    fact: str
    source: FactSource
    evidence: str


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    path: str
    name: str
    qualname: str
    kind: str
    line: int
    end_line: int
    exported: bool


@dataclass(frozen=True, slots=True)
class Relation:
    src: str
    kind: str
    """``imports`` (src imports dst) or ``tests`` (test file src covers dst)."""
    dst: str
    evidence: str


@dataclass(frozen=True, slots=True)
class RefreshReport:
    files: int = 0
    unchanged: int = 0
    indexed: int = 0
    """Paths whose facts were (re)written."""
    analyzed: int = 0
    """Blobs an analyzer actually ran on: the expensive part, ideally only changed content."""
    reused: int = 0
    """Indexed paths whose analysis came from the global cache."""
    removed: int = 0
    rebuilt: bool = False
    """Whether the index was discarded first (new database, schema, or classifier)."""


@dataclass(frozen=True, slots=True)
class IndexInfo:
    """Version metadata, so a later miss can be attributed to the index that produced it."""

    schema: int
    classifier: str
    analyzers: dict[str, str]
    search: str
    files: int
    refreshed_at: str | None


@dataclass
class _Counts:
    indexed: int = 0
    analyzed: int = 0
    reused: int = 0


def translated[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Report SQLite failures (for example a lock held too long) as ``RepositoryError``."""

    @functools.wraps(method)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return method(*args, **kwargs)
        except sqlite3.Error as error:
            raise RepositoryError(f"repository index unavailable: {error}") from error

    return wrapper


def default_analyzers() -> AnalyzerSet:
    return AnalyzerSet((PythonAnalyzer(),))


class RepositoryIndex:
    def __init__(
        self,
        workspace: GitWorkspace,
        path: Path | None = None,
        cache: AnalysisCache | None = None,
        analyzers: AnalyzerSet | None = None,
    ) -> None:
        self.workspace = workspace
        self.path = path or project_state_dir(workspace.root) / INDEX_FILE
        self.cache = cache or AnalysisCache.default()
        self.analyzers = analyzers or default_analyzers()
        self._connection: sqlite3.Connection | None = None
        self._rebuilt = False

    @classmethod
    def open(
        cls,
        root: Path,
        cache: AnalysisCache | None = None,
        analyzers: AnalyzerSet | None = None,
    ) -> RepositoryIndex:
        """The index for the Git repository containing ``root``; raises ``RepositoryError``."""
        return cls(GitWorkspace(root), cache=cache, analyzers=analyzers)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    # -- Storage -------------------------------------------------------------------------------

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            try:
                ensure_private_dir(self.path.parent)
                self._connection = self._open()
            except (OSError, sqlite3.Error) as error:
                raise RepositoryError(f"cannot open repository index: {error}") from error
        return self._connection

    def _open(self) -> sqlite3.Connection:
        connection = connect(self.path)
        try:
            if compatible(connection):
                return connection
        except sqlite3.OperationalError:
            connection.close()
            raise  # locked or unreadable: not proof of corruption, so never discard
        except sqlite3.DatabaseError:
            pass  # corrupt: derived state, safe to rebuild
        connection.close()
        discard(self.path)
        connection = connect(self.path)
        mode = FTS5 if fts5_available(connection) else PLAIN
        with connection:
            for statement in (*SCHEMA, FTS5_TABLE if mode == FTS5 else PLAIN_TABLE):
                connection.execute(statement)
            connection.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [("classifier", CLASSIFIER_VERSION), ("search", mode)],
            )
            connection.execute(f"PRAGMA user_version = {INDEX_SCHEMA}")
        self._rebuilt = True
        return connection

    def _meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    # -- Refresh -------------------------------------------------------------------------------

    @translated
    def refresh(self) -> RefreshReport:
        """Bring the index up to date with the working tree, doing as little work as possible."""
        current = {entry.path: entry for entry in self.workspace.files()}
        db = self._db
        rebuilt, self._rebuilt = self._rebuilt, False
        stored = {
            row["path"]: (row["blob"], row["analyzer"], row["analyzer_version"])
            for row in db.execute("SELECT path, blob, analyzer, analyzer_version FROM files")
        }
        removed = sorted(stored.keys() - current.keys())
        stale = [entry for path, entry in current.items() if stored.get(path) != self._key(entry)]
        counts = _Counts()
        if removed or stale:
            with db:
                for path in removed:
                    self._forget(db, path)
                for entry in stale:
                    self._forget(db, entry.path)
                    self._index(db, entry, counts)
                self._relate(db)
        with db:
            db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('refreshed_at', ?)",
                (datetime.now(UTC).isoformat(timespec="seconds"),),
            )
        return RefreshReport(
            files=len(current),
            unchanged=len(current) - len(stale),
            indexed=counts.indexed,
            analyzed=counts.analyzed,
            reused=counts.reused,
            removed=len(removed),
            rebuilt=rebuilt,
        )

    def _key(self, entry: FileEntry) -> tuple[str, str | None, str | None]:
        analyzer = self.analyzers.for_path(entry.path)
        if analyzer is None:
            return (entry.blob, None, None)
        return (entry.blob, analyzer.name, analyzer.version)

    @staticmethod
    def _forget(db: sqlite3.Connection, path: str) -> None:
        db.execute("DELETE FROM files WHERE path = ?", (path,))
        db.execute("DELETE FROM search WHERE path = ?", (path,))

    def _index(self, db: sqlite3.Connection, entry: FileEntry, counts: _Counts) -> None:
        content = self.workspace.read(entry.path)
        if content is None:
            return
        path = entry.path
        actual_blob = blob_id(content, self.workspace.object_format)
        classifications = classify_path(path)
        analyzer = self.analyzers.for_path(path)
        state, detail = AnalysisState.UNSUPPORTED, ""
        analysis: Analysis | None = None
        searchable = False
        if any(c.fact == GENERATED for c in classifications):
            state, detail = AnalysisState.SKIPPED, "generated or vendored"
        elif b"\0" in content[:BINARY_PROBE]:
            state, detail = AnalysisState.SKIPPED, "binary"
        elif len(content) > MAX_FILE_BYTES:
            state, detail = AnalysisState.SKIPPED, f"larger than {MAX_FILE_BYTES} bytes"
        else:
            searchable = True
            if analyzer is not None:
                analysis = self.cache.get(actual_blob, analyzer.name, analyzer.version)
                if analysis is None:
                    analysis = analyzer.analyze(content)
                    self.cache.put(actual_blob, analyzer.name, analyzer.version, analysis)
                    counts.analyzed += 1
                else:
                    counts.reused += 1
                state = AnalysisState.FAILED if analysis.error else AnalysisState.ANALYZED
                detail = analysis.error or ""

        db.execute(
            "INSERT INTO files (path, blob, language, analyzer, analyzer_version, analysis, "
            "detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                path,
                actual_blob,
                analysis.language if analysis else language_of(path),
                analyzer.name if analyzer else None,
                analyzer.version if analyzer else None,
                state,
                detail,
            ),
        )
        facts = list(classifications)
        if analysis is not None:
            for item in analysis.imports:
                facts += import_signals(item.module, item.line) if item.level == 0 else []
            self._store_structure(db, path, analysis)
        db.executemany(
            "INSERT OR IGNORE INTO facts (path, fact, source, evidence) VALUES (?, ?, ?, ?)",
            [(path, c.fact, FactSource.STRUCTURAL, c.evidence) for c in facts],
        )
        if searchable:
            symbols = " ".join(s.qualname for s in analysis.symbols) if analysis else ""
            db.execute(
                "INSERT INTO search (path, symbols, body) VALUES (?, ?, ?)",
                (path, symbols, content.decode("utf-8", errors="replace")),
            )
        counts.indexed += 1

    @staticmethod
    def _store_structure(db: sqlite3.Connection, path: str, analysis: Analysis) -> None:
        db.executemany(
            "INSERT INTO symbols (path, name, qualname, kind, line, end_line, exported) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (path, s.name, s.qualname, s.kind, s.line, s.end_line, int(s.exported))
                for s in analysis.symbols
            ],
        )
        db.executemany(
            "INSERT INTO imports (path, module, names, level, line) VALUES (?, ?, ?, ?, ?)",
            [(path, i.module, ",".join(i.names), i.level, i.line) for i in analysis.imports],
        )

    def _relate(self, db: sqlite3.Connection) -> None:
        """Re-derive module names and relationships from stored rows (no file is re-read)."""
        paths = [row[0] for row in db.execute("SELECT path FROM files ORDER BY path")]
        packages = {str(PurePosixPath(p).parent) for p in paths if p.endswith("/__init__.py")}
        modules: defaultdict[str, list[str]] = defaultdict(list)
        names: dict[str, str] = {}
        for path in paths:
            if path.endswith((".py", ".pyi")):
                names[path] = module_name(path, packages)
                modules[names[path]].append(path)
        db.execute("UPDATE files SET module = NULL")
        db.executemany(
            "UPDATE files SET module = ? WHERE path = ?", [(m, p) for p, m in names.items()]
        )

        relations: dict[tuple[str, str, str], str] = {}
        imports = db.execute("SELECT path, module, names, level, line FROM imports ORDER BY rowid")
        for path, module, imported, level, line in imports:
            if path not in names:
                continue
            for target, evidence in resolve_import(
                names[path], path, module, imported, level, line, modules
            ):
                relations.setdefault((path, "imports", target), evidence)

        kinds: defaultdict[str, set[str]] = defaultdict(set)
        rows = db.execute("SELECT path, fact FROM facts WHERE fact IN (?, ?)", (TEST, SOURCE))
        for path, fact in rows:
            kinds[fact].add(path)
        tests, sources = kinds[TEST], kinds[SOURCE] - kinds[TEST]
        by_stem: defaultdict[str, list[str]] = defaultdict(list)
        for path in sorted(sources):
            by_stem[PurePosixPath(path).stem].append(path)
        for (src, kind, dst), evidence in list(relations.items()):
            if kind == "imports" and src in tests and dst in sources:
                relations.setdefault((src, "tests", dst), evidence)
        for test in sorted(tests):
            stem = tested_stem(PurePosixPath(test).name)
            for target in by_stem.get(stem or "", [])[:TEST_NAME_MATCHES]:
                evidence = (
                    f"name: '{PurePosixPath(test).name}' matches '{PurePosixPath(target).name}'"
                )
                relations.setdefault((test, "tests", target), evidence)

        db.execute("DELETE FROM relations")
        db.executemany(
            "INSERT INTO relations (src, kind, dst, evidence) VALUES (?, ?, ?, ?)",
            [(src, kind, dst, evidence) for (src, kind, dst), evidence in relations.items()],
        )

    # -- Queries -------------------------------------------------------------------------------

    @translated
    def info(self) -> IndexInfo:
        count = self._db.execute("SELECT count(*) FROM files").fetchone()[0]
        return IndexInfo(
            schema=INDEX_SCHEMA,
            classifier=self._meta("classifier") or CLASSIFIER_VERSION,
            analyzers=self.analyzers.versions(),
            search=self._meta("search") or PLAIN,
            files=count,
            refreshed_at=self._meta("refreshed_at"),
        )

    @translated
    def file(self, path: str) -> FileRecord | None:
        row = self._db.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
        return None if row is None else to_file(row)

    @translated
    def files(self, prefix: str = "") -> list[FileRecord]:
        """Indexed files, optionally only those under the directory ``prefix``."""
        if not prefix:
            rows = self._db.execute("SELECT * FROM files ORDER BY path").fetchall()
        else:
            directory = prefix.rstrip("/") + "/"
            rows = self._db.execute(
                "SELECT * FROM files WHERE substr(path, 1, ?) = ? ORDER BY path",
                (len(directory), directory),
            ).fetchall()
        return [to_file(row) for row in rows]

    @translated
    def facts(self, path: str) -> list[Fact]:
        rows = self._db.execute(
            "SELECT * FROM facts WHERE path = ? ORDER BY rowid", (path,)
        ).fetchall()
        return [
            Fact(row["path"], row["fact"], FactSource(row["source"]), row["evidence"])
            for row in rows
        ]

    @translated
    def paths_with_fact(self, fact: str) -> list[str]:
        rows = self._db.execute(
            "SELECT path FROM facts WHERE fact = ? ORDER BY path", (fact,)
        ).fetchall()
        return [row[0] for row in rows]

    @translated
    def symbols(self, path: str) -> list[SymbolRecord]:
        rows = self._db.execute(
            "SELECT * FROM symbols WHERE path = ? ORDER BY line", (path,)
        ).fetchall()
        return [to_symbol(row) for row in rows]

    @translated
    def find_symbol(self, name: str) -> list[SymbolRecord]:
        """Definitions whose name or dotted qualified name is exactly ``name``."""
        rows = self._db.execute(
            "SELECT * FROM symbols WHERE name = ? OR qualname = ? ORDER BY path, line",
            (name, name),
        ).fetchall()
        return [to_symbol(row) for row in rows]

    @translated
    def module_paths(self, module: str) -> list[str]:
        rows = self._db.execute(
            "SELECT path FROM files WHERE module = ? ORDER BY path", (module,)
        ).fetchall()
        return [row[0] for row in rows]

    @translated
    def relations(self, path: str) -> list[Relation]:
        """Relationships in which ``path`` is either side."""
        rows = self._db.execute(
            "SELECT * FROM relations WHERE src = ? UNION SELECT * FROM relations WHERE dst = ? "
            "ORDER BY kind, src, dst",
            (path, path),
        ).fetchall()
        return [Relation(*row) for row in rows]

    @translated
    def search(self, term: str, limit: int = 20) -> list[str]:
        """Paths whose text or symbol names contain ``term`` literally."""
        return search_text(self._db, self._meta("search") or PLAIN, term, limit)


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def compatible(connection: sqlite3.Connection) -> bool:
    if connection.execute("PRAGMA user_version").fetchone()[0] != INDEX_SCHEMA:
        return False
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    required = {"meta", "files", "facts", "symbols", "imports", "relations", "search"}
    if not required.issubset(tables):
        return False
    row = connection.execute("SELECT value FROM meta WHERE key = 'classifier'").fetchone()
    return row is not None and row[0] == CLASSIFIER_VERSION


def module_name(path: str, packages: Iterable[str]) -> str:
    """The importable dotted name of a Python file, rooted above its outermost package."""
    packages = set(packages)
    parts = PurePosixPath(path).with_suffix("").parts
    start = len(parts) - 1
    while start > 0 and "/".join(parts[:start]) in packages:
        start -= 1
    module = list(parts[start:])
    if module[-1] == "__init__" and len(module) > 1:
        module.pop()
    return ".".join(module)


def resolve_import(
    importer: str,
    path: str,
    module: str,
    imported: str,
    level: int,
    line: int,
    modules: dict[str, list[str]] | defaultdict[str, list[str]],
) -> list[tuple[str, str]]:
    """Repository files an import refers to, each with evidence. External imports yield none."""
    if level:
        package = importer.split(".")
        if not path.endswith("/__init__.py") and path != "__init__.py":
            package = package[:-1]
        if level - 1 > len(package):
            return []
        package = package[: len(package) - (level - 1)]
        base = ".".join([*package, *([module] if module else [])])
    else:
        base = module
    written = "." * level + module
    found: list[tuple[str, str]] = []
    unresolved_names = False
    for name in filter(None, imported.split(",")):
        targets = modules.get(f"{base}.{name}" if base else name, [])
        found += [(t, f"line {line}: from {written} import {name}") for t in targets]
        unresolved_names |= not targets
    if not imported or unresolved_names:
        statement = f"from {written} import {imported}" if imported else f"import {written}"
        found += [(t, f"line {line}: {statement}") for t in modules.get(base, [])]
    return [(target, evidence) for target, evidence in found if target != path]


def tested_stem(name: str) -> str | None:
    """``foo`` for test files such as ``test_foo.py``, ``foo_test.go``, or ``foo.spec.ts``."""
    stem = PurePosixPath(name).stem
    for prefix in ("test_",):
        if stem.startswith(prefix):
            return stem[len(prefix) :] or None
    for suffix in ("_test", ".test", ".spec", "Test", "Tests"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)] or None
    return None


def to_file(row: sqlite3.Row) -> FileRecord:
    analyzer = f"{row['analyzer']}@{row['analyzer_version']}" if row["analyzer"] else None
    return FileRecord(
        row["path"],
        row["blob"],
        row["language"],
        row["module"],
        analyzer,
        AnalysisState(row["analysis"]),
        row["detail"],
    )


def to_symbol(row: sqlite3.Row) -> SymbolRecord:
    return SymbolRecord(
        row["path"],
        row["name"],
        row["qualname"],
        row["kind"],
        row["line"],
        row["end_line"],
        bool(row["exported"]),
    )

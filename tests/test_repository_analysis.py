from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from agent_cli.repository import AnalysisCache, PythonAnalyzer, RepositoryIndex
from agent_cli.repository import index as index_module
from agent_cli.repository.analyzers import Analysis, Import, Symbol
from agent_cli.repository.classify import classify_path, import_signals
from agent_cli.repository.index import module_name, resolve_import
from gitrepo import commit, write


def facts(path: str) -> set[str]:
    return {c.fact for c in classify_path(path)}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/app/cache.py", {"source", "language:python"}),
        ("tests/test_cache.py", {"test", "language:python"}),
        ("pkg/cache_test.go", {"test", "language:go"}),
        ("web/button.spec.tsx", {"test", "language:typescript"}),
        ("migrations/0001_init.py", {"migration", "language:python"}),
        ("db/0042_add_ttl.sql", {"migration", "language:sql"}),
        (".github/workflows/ci.yml", {"ci", "configuration"}),
        ("Dockerfile", {"deployment"}),
        ("infra/main.tf", {"deployment", "language:terraform", "source"}),
        ("pyproject.toml", {"dependency_manifest", "configuration"}),
        ("requirements-dev.txt", {"dependency_manifest"}),
        ("package.json", {"dependency_manifest", "configuration"}),
        ("config/settings.yaml", {"configuration"}),
        ("docs/ROUTING.md", {"documentation"}),
        ("README.md", {"documentation"}),
        ("README.txt", {"documentation"}),
        ("docs/notes.txt", {"documentation"}),
        ("tests/fixtures/data.txt", {"test"}),
        ("LICENSE", {"documentation"}),
        ("node_modules/lib/index.js", {"generated", "language:javascript"}),
        ("api/service_pb2.py", {"generated", "language:python"}),
    ],
)
def test_paths_are_classified(path: str, expected: set[str]) -> None:
    assert facts(path) == expected


def test_every_classification_carries_evidence() -> None:
    for classification in classify_path("tests/migrations/test_0001.py"):
        assert classification.evidence.startswith("path:")


def test_imports_become_signals_with_evidence() -> None:
    [signal] = import_signals("sqlalchemy.orm", 7)
    assert (signal.fact, signal.evidence) == (
        "signal:database",
        "imports 'sqlalchemy.orm' (line 7)",
    )
    assert import_signals("app.models", 1) == []


PYTHON = b'''\
"""Sessions."""
from __future__ import annotations
import os, json as j
from . import store
from ..core.tokens import sign, verify
from typing import TYPE_CHECKING

__all__ = ["SessionService", "create"]


def create():
    import hashlib


async def _helper():
    pass


def internal():
    pass


class SessionService:
    def refresh(self):
        pass

    async def _rotate(self):
        pass

    def __eq__(self, other):
        return True


class _Private:
    def run(self):
        pass
'''


def test_python_analyzer_extracts_symbols_and_exports() -> None:
    analysis = PythonAnalyzer().analyze(PYTHON)
    assert analysis.error is None and analysis.language == "python"
    found = {s.qualname: (s.kind, s.exported) for s in analysis.symbols}
    assert found == {
        "create": ("function", True),
        "_helper": ("function", False),
        "internal": ("function", False),
        "SessionService": ("class", True),
        "SessionService.refresh": ("method", True),
        "SessionService._rotate": ("method", False),
        "SessionService.__eq__": ("method", True),
        "_Private": ("class", False),
        "_Private.run": ("method", False),
    }
    refresh = next(s for s in analysis.symbols if s.name == "refresh")
    assert (refresh.line, refresh.end_line) == (24, 25)


def test_python_analyzer_extracts_imports() -> None:
    imports = PythonAnalyzer().analyze(PYTHON).imports
    assert [(i.module, i.names, i.level) for i in imports] == [
        ("__future__", ("annotations",), 0),
        ("os", (), 0),
        ("json", (), 0),
        ("", ("store",), 1),
        ("core.tokens", ("sign", "verify"), 2),
        ("typing", ("TYPE_CHECKING",), 0),
        ("hashlib", (), 0),
    ]


def test_without_all_underscore_names_are_private() -> None:
    analysis = PythonAnalyzer().analyze(b"def a(): pass\ndef _b(): pass\n")
    assert [(s.name, s.exported) for s in analysis.symbols] == [("a", True), ("_b", False)]


def test_syntax_errors_are_reported_not_raised() -> None:
    analysis = PythonAnalyzer().analyze(b"def broken(:\n")
    assert analysis.symbols == () and analysis.error is not None


def test_analysis_round_trips_through_json() -> None:
    analysis = Analysis(
        "python",
        (Symbol("f", "f", "function", 1, 2, True),),
        (Import("os.path", ("join",), 0, 1),),
    )
    assert Analysis.from_json(analysis.to_json()) == analysis


def test_cached_analysis_is_keyed_by_blob_and_analyzer_version(tmp_path: Path) -> None:
    cache = AnalysisCache(tmp_path / "cache.sqlite")
    analysis = Analysis("python")
    cache.put("abc", "python-ast", "1", analysis)
    assert cache.get("abc", "python-ast", "1") == analysis
    assert cache.get("abc", "python-ast", "2") is None
    assert cache.get("def", "python-ast", "1") is None


@pytest.mark.parametrize(
    ("path", "packages", "expected"),
    [
        ("src/app/cache.py", {"src/app"}, "app.cache"),
        ("src/app/__init__.py", {"src/app"}, "app"),
        ("tests/test_cache.py", set(), "test_cache"),
        ("pkg/sub/mod.py", {"pkg", "pkg/sub"}, "pkg.sub.mod"),
        ("__init__.py", set(), "__init__"),
    ],
)
def test_module_names_are_rooted_above_the_outermost_package(
    path: str, packages: set[str], expected: str
) -> None:
    assert module_name(path, packages) == expected


def test_imports_resolve_to_repository_files_only() -> None:
    modules: defaultdict[str, list[str]] = defaultdict(list)
    modules.update(
        {
            "app": ["src/app/__init__.py"],
            "app.cache": ["src/app/cache.py"],
            "app.store": ["src/app/store.py"],
        }
    )
    resolve = resolve_import

    assert resolve("app.service", "src/app/service.py", "cache", "Session", 1, 3, modules) == [
        ("src/app/cache.py", "line 3: from .cache import Session")
    ]
    assert resolve("app.service", "src/app/service.py", "", "store", 1, 4, modules) == [
        ("src/app/store.py", "line 4: from . import store")
    ]
    assert resolve("app", "src/app/__init__.py", "cache", "", 1, 1, modules) == [
        ("src/app/cache.py", "line 1: import .cache")
    ]
    assert resolve("app.service", "src/app/service.py", "os", "", 0, 1, modules) == []
    assert resolve("app.service", "src/app/service.py", "x", "", 5, 1, modules) == []


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("test_cache.py", "cache"),
        ("cache_test.go", "cache"),
        ("button.spec.ts", "button"),
        ("CacheTest.java", "Cache"),
        ("conftest.py", None),
        ("test_.py", None),
    ],
)
def test_tested_stems(name: str, stem: str | None) -> None:
    assert index_module.tested_stem(name) == stem


@pytest.fixture
def searchable(repo: Path) -> Path:
    write(repo, "app/errors.py", 'MESSAGE = "token expired: refresh required"\n')
    write(repo, "app/cache.py", "class SessionCache:\n    pass\n")
    write(repo, "app/config.yaml", "session_ttl_seconds: 300\n")
    commit(repo)
    return repo


@pytest.mark.parametrize("fts5", [True, False])
def test_lexical_search_finds_identifiers_and_strings(
    searchable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fts5: bool
) -> None:
    if not fts5:
        monkeypatch.setattr("agent_cli.repository.index.fts5_available", lambda db: False)
    index = RepositoryIndex.open(searchable, cache=AnalysisCache(tmp_path / "c.sqlite"))
    index.refresh()
    assert index.info().search == ("fts5" if fts5 else "plain")
    assert index.search("SessionCache") == ["app/cache.py"]
    assert index.search("token expired") == ["app/errors.py"]
    assert index.search("session_ttl_seconds") == ["app/config.yaml"]
    assert index.search('"; DROP TABLE files; --') == []
    assert index.search("   ") == []

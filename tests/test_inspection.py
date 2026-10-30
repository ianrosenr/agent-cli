from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.repository import (
    AnalysisCache,
    Anchors,
    RepositoryIndex,
    TaskInspector,
    Weight,
)
from gitrepo import commit, write

SOURCE = "class SessionCache:\n    def get(self, key):\n        return None\n"


@pytest.fixture
def index(repo: Path, tmp_path: Path) -> RepositoryIndex:
    write(repo, "src/app/__init__.py", "")
    write(repo, "src/app/cache.py", SOURCE)
    write(repo, "src/app/service.py", "from app.cache import SessionCache\nimport sqlalchemy\n")
    write(repo, "src/app/billing.py", "def charge():\n    pass  # unrelated\n")
    write(repo, "tests/test_cache.py", "def test_get():\n    pass\n")
    write(repo, "migrations/0001_sessions.sql", "CREATE TABLE sessions (id TEXT);\n")
    write(repo, "README.md", "Uses SessionCache for sessions.\n")
    commit(repo)
    return RepositoryIndex.open(repo, cache=AnalysisCache(tmp_path / "cache.sqlite"))


def inspect(index: RepositoryIndex, **anchors: tuple[str, ...]) -> list[str]:
    return [f.path for f in TaskInspector(index).inspect(Anchors(**anchors)).files]


def test_explicit_file_anchor_ranks_first(index: RepositoryIndex) -> None:
    paths = inspect(index, paths=("src/app/billing.py",), terms=("SessionCache",))
    assert paths[0] == "src/app/billing.py"


def test_explicit_symbol_resolves_to_its_definition(index: RepositoryIndex) -> None:
    result = TaskInspector(index).inspect(Anchors(symbols=("SessionCache.get",)))
    top = result.files[0]
    assert top.path == "src/app/cache.py"
    assert top.reasons[0].weight is Weight.EXPLICIT_SYMBOL
    assert top.reasons[0].detail == "defines method 'SessionCache.get' (line 2)"
    assert top.symbols[0].qualname == "SessionCache.get"
    assert top.analyzer == "python-ast@1" and top.blob


def test_symbol_mentions_rank_below_the_definition(index: RepositoryIndex) -> None:
    result = TaskInspector(index).inspect(Anchors(symbols=("SessionCache",)))
    weights = {f.path: f.reasons[0].weight for f in result.files}
    assert weights["src/app/cache.py"] is Weight.EXPLICIT_SYMBOL
    assert weights["README.md"] is Weight.EXACT_IDENTIFIER
    assert result.files[0].path == "src/app/cache.py"


def test_lexical_anchor_works(index: RepositoryIndex) -> None:
    result = TaskInspector(index).inspect(Anchors(terms=("CREATE TABLE sessions",)))
    assert result.files[0].path == "migrations/0001_sessions.sql"
    assert result.files[0].reasons[0].weight is Weight.LEXICAL
    assert "migration" in result.facts().categories


def test_neighbors_and_related_tests_are_included(index: RepositoryIndex) -> None:
    result = TaskInspector(index).inspect(Anchors(paths=("src/app/cache.py",)))
    reasons = {f.path: {r.weight for r in f.reasons} for f in result.files}
    assert Weight.NEIGHBOR in reasons["src/app/service.py"]
    assert Weight.RELATED_TEST in reasons["tests/test_cache.py"]
    assert "src/app/billing.py" not in reasons
    assert "migrations/0001_sessions.sql" not in reasons


def test_changed_files_and_modules_are_anchors(index: RepositoryIndex) -> None:
    result = TaskInspector(index).inspect(
        Anchors(changed=("src/app/billing.py",), modules=("app.service",))
    )
    first, second = result.files[:2]
    assert (first.path, first.reasons[0].weight) == ("src/app/service.py", Weight.MODULE)
    assert (second.path, second.reasons[0].weight) == ("src/app/billing.py", Weight.CHANGED_FILE)


def test_directory_anchor_covers_its_files(index: RepositoryIndex) -> None:
    paths = inspect(index, paths=("src/app",))
    assert {"src/app/cache.py", "src/app/billing.py"} <= set(paths)


def test_unresolved_anchors_are_reported(index: RepositoryIndex) -> None:
    result = TaskInspector(index).inspect(
        Anchors(paths=("nope.py",), symbols=("Missing",), modules=("x.y",), terms=("zzqx",))
    )
    assert result.files == ()
    assert result.unresolved == ("path:nope.py", "symbol:Missing", "module:x.y", "term:zzqx")


def test_results_are_bounded_and_never_the_whole_repository(repo: Path, tmp_path: Path) -> None:
    write(repo, "core.py", "def shared():\n    pass\n")
    for n in range(40):
        write(repo, f"user_{n}.py", "from core import shared\n")
    commit(repo)
    index = RepositoryIndex.open(repo, cache=AnalysisCache(tmp_path / "cache.sqlite"))

    result = TaskInspector(index).inspect(Anchors(paths=("core.py",)), limit=5)
    assert len(result.files) == 5 and result.truncated
    unlimited = TaskInspector(index).inspect(Anchors(paths=("core.py",)), limit=100)
    assert len(unlimited.files) < 41, "neighbor expansion is capped per anchor"


def test_facts_are_compact_and_free_of_source_text(index: RepositoryIndex) -> None:
    result = TaskInspector(index).inspect(Anchors(paths=("src/app/service.py",)))
    facts = result.facts()
    assert facts.paths[0] == "src/app/service.py"
    assert "database" in facts.signals and "python" in facts.languages
    assert "source" in facts.categories and "SessionCache" in facts.symbols
    assert all(e.source == "structural" and e.evidence for e in facts.evidence)
    assert "return None" not in repr(result)


def test_inspection_refreshes_a_stale_index_first(index: RepositoryIndex, repo: Path) -> None:
    TaskInspector(index).inspect(Anchors(terms=("SessionCache",)))
    write(repo, "src/app/ttl.py", "class ExpiringCache:\n    pass\n")

    result = TaskInspector(index).inspect(Anchors(symbols=("ExpiringCache",)))
    assert result.files[0].path == "src/app/ttl.py"
    assert result.refresh is not None and result.refresh.indexed == 1
    assert result.index.analyzers == {"python-ast": "1"}

    write(repo, "src/app/later.py", "class Later:\n    pass\n")
    stale = TaskInspector(index).inspect(Anchors(symbols=("Later",)), refresh=False)
    assert stale.unresolved == ("symbol:Later",) and stale.refresh is None


@pytest.fixture
def hub(repo: Path, tmp_path: Path) -> RepositoryIndex:
    """``core.py`` is imported by ten modules and covered by one test."""
    write(repo, "core.py", "def shared():\n    pass\n")
    for n in range(10):
        write(repo, f"user_{n}.py", "from core import shared\n")
    write(repo, "tests/test_core.py", "def test_shared():\n    pass\n")
    write(repo, "notes.md", "unrelated_marker\n")
    commit(repo)
    return RepositoryIndex.open(repo, cache=AnalysisCache(tmp_path / "cache.sqlite"))


def test_a_widely_imported_module_keeps_its_related_tests(hub: RepositoryIndex) -> None:
    result = TaskInspector(hub).inspect(Anchors(paths=("core.py",)), limit=50)
    weights = {f.path: {r.weight for r in f.reasons} for f in result.files}
    assert Weight.RELATED_TEST in weights["tests/test_core.py"]
    assert sum(path.startswith("user_") for path in weights) == 8
    assert result.truncated, "dropped importers are reported"


def test_many_weak_reasons_never_outrank_direct_evidence(hub: RepositoryIndex) -> None:
    users = tuple(f"user_{n}.py" for n in range(10))
    result = TaskInspector(hub).inspect(Anchors(paths=users, changed=("notes.md",)), limit=50)
    scores = {f.path: f.score for f in result.files}
    assert scores["notes.md"] > scores["core.py"]
    assert scores["core.py"] < Weight.NEIGHBOR + 1


def test_mention_only_files_get_only_a_weak_expansion(hub: RepositoryIndex) -> None:
    result = TaskInspector(hub).inspect(Anchors(terms=("from core import shared",)), limit=50)
    core = next(f for f in result.files if f.path == "core.py")
    assert {r.weight for r in core.reasons} == {Weight.WEAK_NEIGHBOR}


def test_common_symbol_names_are_capped_with_exact_matches_first(
    repo: Path, tmp_path: Path
) -> None:
    write(repo, "api.py", "def get():\n    pass\n")
    for n in range(8):
        write(repo, f"store_{n}.py", f"class Store{n}:\n    def get(self):\n        pass\n")
    commit(repo)
    index = RepositoryIndex.open(repo, cache=AnalysisCache(tmp_path / "cache.sqlite"))

    result = TaskInspector(index).inspect(Anchors(symbols=("get",)), limit=50)
    defined = [f for f in result.files if f.reasons[0].weight is Weight.EXPLICIT_SYMBOL]
    assert len(defined) == 5
    assert "api.py" in {f.path for f in defined}
    assert result.capped == ("symbol:get (9 definitions, kept 5)",)


def test_large_directory_anchors_are_capped_and_reported(repo: Path, tmp_path: Path) -> None:
    for n in range(12):
        write(repo, f"pkg/mod_{n:02}.py", "x = 1\n")
    commit(repo)
    index = RepositoryIndex.open(repo, cache=AnalysisCache(tmp_path / "cache.sqlite"))

    result = TaskInspector(index).inspect(Anchors(paths=("pkg/",)), limit=50)
    assert len(result.files) == 8
    assert result.capped == ("path:pkg/ (12 files, kept 8)",)


@pytest.mark.parametrize("path", ["", ".", "./", "/"])
def test_the_repository_root_is_not_an_anchor(index: RepositoryIndex, path: str) -> None:
    result = TaskInspector(index).inspect(Anchors(paths=(path,)))
    assert result.files == () and result.unresolved == (f"path:{path}",)

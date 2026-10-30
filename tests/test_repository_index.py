from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_cli.repository import (
    Analysis,
    AnalysisCache,
    AnalysisState,
    AnalyzerSet,
    FactSource,
    PythonAnalyzer,
    RepositoryError,
    RepositoryIndex,
)
from agent_cli.repository.git import GitWorkspace, blob_id
from agent_cli.state import TaskStore
from gitrepo import commit, git, init_repo, locked, write

CACHE_MODULE = """\
class SessionCache:
    def get(self, key):
        return None
"""


class CountingAnalyzer(PythonAnalyzer):
    def __init__(self, version: str = "1") -> None:
        self.version = version
        self.calls = 0

    def analyze(self, content: bytes) -> Analysis:
        self.calls += 1
        return super().analyze(content)


@pytest.fixture
def cache(tmp_path: Path) -> AnalysisCache:
    return AnalysisCache(tmp_path / "global" / "repository-analysis.sqlite")


@pytest.fixture
def analyzer() -> CountingAnalyzer:
    return CountingAnalyzer()


def open_index(root: Path, cache: AnalysisCache, analyzer: CountingAnalyzer) -> RepositoryIndex:
    return RepositoryIndex.open(root, cache=cache, analyzers=AnalyzerSet((analyzer,)))


@pytest.fixture
def project(repo: Path) -> Path:
    write(repo, "src/app/__init__.py", "")
    write(repo, "src/app/cache.py", CACHE_MODULE)
    write(repo, "tests/test_cache.py", "from app.cache import SessionCache\n")
    write(repo, "README.md", "# App\n")
    commit(repo)
    return repo


def test_default_locations(project: Path, isolated_home: Path) -> None:
    index = RepositoryIndex.open(project)
    assert index.path == project / ".agent-cli" / "state" / "repository.sqlite"
    assert index.cache.path == isolated_home / ".agent-cli" / "cache" / "repository-analysis.sqlite"


def test_cold_index_persists_files_facts_and_symbols(
    project: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    report = open_index(project, cache, analyzer).refresh()
    assert report.rebuilt and report.files == report.indexed == 4
    assert report.analyzed == analyzer.calls == 3

    index = open_index(project, cache, analyzer)
    record = index.file("src/app/cache.py")
    assert record is not None and record.analysis is AnalysisState.ANALYZED
    assert record.module == "app.cache" and record.analyzer == "python-ast@1"
    assert [s.qualname for s in index.symbols("src/app/cache.py")] == [
        "SessionCache",
        "SessionCache.get",
    ]
    assert index.info().files == 4


def test_warm_refresh_reanalyzes_nothing(
    project: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    open_index(project, cache, analyzer).refresh()
    analyzer.calls = 0
    report = open_index(project, cache, analyzer).refresh()
    assert (report.unchanged, report.indexed, report.analyzed, report.rebuilt) == (4, 0, 0, False)
    assert analyzer.calls == 0


def test_changed_content_is_analyzed_once(
    project: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    index = open_index(project, cache, analyzer)
    index.refresh()
    analyzer.calls = 0
    write(project, "src/app/cache.py", CACHE_MODULE + "\ndef expire():\n    pass\n")

    report = index.refresh()
    assert (report.indexed, report.analyzed, report.unchanged) == (1, 1, 3)
    assert "expire" in [s.name for s in index.symbols("src/app/cache.py")]
    assert index.refresh().analyzed == 0
    assert analyzer.calls == 1


def test_dirty_tracked_content_gets_a_current_content_blob_id(project: Path) -> None:
    text = CACHE_MODULE + "# local edit\n"
    write(project, "src/app/cache.py", text)
    entries = {entry.path: entry.blob for entry in GitWorkspace(project).files()}
    expected = git(project, "hash-object", "src/app/cache.py").strip()
    assert entries["src/app/cache.py"] == expected == blob_id(text.encode())
    assert entries["README.md"] == git(project, "rev-parse", "HEAD:README.md").strip()


def test_deleted_paths_lose_all_their_facts(
    project: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    index = open_index(project, cache, analyzer)
    index.refresh()
    (project / "src/app/cache.py").unlink()

    report = index.refresh()
    assert report.removed == 1
    assert index.file("src/app/cache.py") is None
    assert index.symbols("src/app/cache.py") == []
    assert index.facts("src/app/cache.py") == []
    assert index.find_symbol("SessionCache") == []
    assert index.search("SessionCache") == ["tests/test_cache.py"]
    assert all(
        "src/app/cache.py" not in (r.src, r.dst) for r in index.relations("tests/test_cache.py")
    )


def test_untracked_files_are_indexed_but_ignored_and_runtime_files_are_not(
    project: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    write(project, ".gitignore", "secret.py\n")
    write(project, "secret.py", "TOKEN = 1\n")
    write(project, "new.py", "def fresh(): pass\n")
    write(project, ".agent-cli/config.yaml", "git: {}\n")
    write(project, "sub/.agent-cli/notes.py", "x = 1\n")
    (project / ".agent-cli/state").mkdir(parents=True, exist_ok=True)

    index = open_index(project, cache, analyzer)
    index.refresh()
    paths = {record.path for record in index.files()}
    assert "new.py" in paths and ".gitignore" in paths
    assert not {p for p in paths if p == "secret.py" or ".agent-cli" in p or p.startswith(".git/")}


def test_identical_content_reuses_analysis_across_repositories(
    project: Path, tmp_path: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    open_index(project, cache, analyzer).refresh()
    other = init_repo(tmp_path / "other")
    write(other, "lib/cache_copy.py", CACHE_MODULE)
    commit(other)
    analyzer.calls = 0

    report = open_index(other, cache, analyzer).refresh()
    assert (report.indexed, report.reused, report.analyzed) == (1, 1, 0)
    assert analyzer.calls == 0
    symbols = open_index(other, cache, analyzer).symbols("lib/cache_copy.py")
    assert [s.qualname for s in symbols] == ["SessionCache", "SessionCache.get"]


def test_switching_branches_reuses_cached_analysis(
    project: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    index = open_index(project, cache, analyzer)
    index.refresh()
    git(project, "checkout", "-q", "-b", "feature")
    write(project, "src/app/cache.py", "def changed(): pass\n")
    commit(project, "feature")
    index.refresh()
    analyzer.calls = 0

    git(project, "checkout", "-q", "main")
    report = index.refresh()
    assert (report.indexed, report.reused, report.analyzed) == (1, 1, 0)
    assert index.find_symbol("SessionCache")


def test_identical_content_reuses_analysis_across_worktrees(
    project: Path, tmp_path: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    open_index(project, cache, analyzer).refresh()
    worktree = tmp_path / "worktree"
    git(project, "worktree", "add", "-q", str(worktree), "-b", "worker")
    analyzer.calls = 0

    report = open_index(worktree, cache, analyzer).refresh()
    assert report.analyzed == 0 and report.reused == 3


def test_analyzer_version_change_reanalyzes_only_its_files(
    project: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    open_index(project, cache, analyzer).refresh()
    upgraded = CountingAnalyzer(version="2")

    report = open_index(project, cache, upgraded).refresh()
    assert (report.indexed, report.analyzed, report.unchanged) == (3, 3, 1)
    record = open_index(project, cache, upgraded).file("src/app/cache.py")
    assert record is not None and record.analyzer == "python-ast@2"
    assert not report.rebuilt


def test_incompatible_index_is_rebuilt(
    project: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    index = open_index(project, cache, analyzer)
    index.refresh()
    index.close()
    with sqlite3.connect(index.path) as connection:
        connection.execute("UPDATE meta SET value = 'old' WHERE key = 'classifier'")
    analyzer.calls = 0

    report = open_index(project, cache, analyzer).refresh()
    assert report.rebuilt and report.indexed == 4
    assert analyzer.calls == 0, "the global cache still serves unchanged content"


def test_corrupt_index_and_cache_are_rebuilt(
    project: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    index = open_index(project, cache, analyzer)
    index.refresh()
    index.close()
    cache.close()
    index.path.write_bytes(b"junk" * 1000)
    cache.path.write_bytes(b"junk" * 1000)

    report = open_index(project, cache, analyzer).refresh()
    assert report.rebuilt and report.indexed == 4 and report.analyzed == 3


def test_rebuilding_the_index_never_touches_task_state(
    project: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    tasks = TaskStore.for_repository(project)
    task = tasks.create_task(tasks.start_session().id, "Keep me")
    tasks.close()
    index = open_index(project, cache, analyzer)
    index.refresh()
    index.close()
    index.path.write_bytes(b"junk")

    open_index(project, cache, analyzer).refresh()
    assert TaskStore.for_repository(project).task(task.id) is not None


def test_runtime_state_is_ignored_by_git(
    project: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    open_index(project, cache, analyzer).refresh()
    assert git(project, "status", "--porcelain") == ""


def test_outside_a_git_repository_indexing_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(RepositoryError, match="not a readable Git repository"):
        RepositoryIndex.open(tmp_path)


def test_unsupported_and_broken_files_keep_path_and_lexical_facts(
    repo: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    write(repo, "server/main.go", "package main\nfunc HandleRefresh() {}\n")
    write(repo, "broken.py", "def oops(:\n    refresh_token = 1\n")
    write(repo, "logo.png", "\0PNG binary")
    commit(repo)
    index = open_index(repo, cache, analyzer)
    index.refresh()

    go = index.file("server/main.go")
    assert go is not None and go.analysis is AnalysisState.UNSUPPORTED and go.language == "go"
    assert {f.fact for f in index.facts("server/main.go")} >= {"language:go", "source"}
    assert index.search("HandleRefresh") == ["server/main.go"]

    broken = index.file("broken.py")
    assert broken is not None and broken.analysis is AnalysisState.FAILED
    assert broken.detail.startswith("cannot parse")
    assert index.search("refresh_token") == ["broken.py"]

    logo = index.file("logo.png")
    assert logo is not None and logo.analysis is AnalysisState.SKIPPED and logo.detail == "binary"


def test_facts_carry_structural_provenance(
    project: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    write(project, "src/app/http.py", "import fastapi\n")
    index = open_index(project, cache, analyzer)
    index.refresh()
    facts = {f.fact: f for f in index.facts("src/app/http.py")}
    assert facts["signal:http_api"].evidence == "imports 'fastapi' (line 1)"
    assert facts["source"].source is FactSource.STRUCTURAL
    assert index.paths_with_fact("test") == ["tests/test_cache.py"]


def test_relationships_resolve_imports_and_related_tests(
    project: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    write(project, "src/app/service.py", "from .cache import SessionCache\n")
    index = open_index(project, cache, analyzer)
    index.refresh()
    relations = {(r.src, r.kind, r.dst) for r in index.relations("src/app/cache.py")}
    assert relations == {
        ("src/app/service.py", "imports", "src/app/cache.py"),
        ("tests/test_cache.py", "imports", "src/app/cache.py"),
        ("tests/test_cache.py", "tests", "src/app/cache.py"),
    }


@pytest.fixture
def short_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_cli.repository.index.BUSY_TIMEOUT", 0.05)
    monkeypatch.setattr("agent_cli.repository.analysis_cache.BUSY_TIMEOUT", 0.05)


@pytest.mark.usefixtures("short_timeouts")
def test_a_locked_index_fails_clearly_and_is_not_discarded(
    project: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    index = open_index(project, cache, analyzer)
    index.refresh()
    index.close()

    with locked(index.path), pytest.raises(RepositoryError, match="locked"):
        open_index(project, cache, analyzer).refresh()
    report = open_index(project, cache, analyzer).refresh()
    assert not report.rebuilt and report.unchanged == 4


@pytest.mark.usefixtures("short_timeouts")
def test_queries_on_a_locked_index_raise_repository_error(
    project: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    index = open_index(project, cache, analyzer)
    index.refresh()
    with locked(index.path), pytest.raises(RepositoryError):
        index.find_symbol("SessionCache")


@pytest.mark.usefixtures("short_timeouts")
def test_a_locked_analysis_cache_never_fails_indexing_or_gets_discarded(
    project: Path, tmp_path: Path, cache: AnalysisCache, analyzer: CountingAnalyzer
) -> None:
    open_index(project, cache, analyzer).refresh()
    cache.close()
    other = init_repo(tmp_path / "other")
    write(other, "copy.py", CACHE_MODULE)
    commit(other)

    with locked(cache.path):
        report = open_index(other, AnalysisCache(cache.path), analyzer).refresh()
    assert report.indexed == 1 and report.analyzed == 1
    assert AnalysisCache(cache.path).get(blob_id(CACHE_MODULE.encode()), "python-ast", "1")

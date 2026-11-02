from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from agent_cli.adapters import (
    DIAGNOSTIC_AUTHORITY,
    AdapterRegistry,
    Changes,
    HarnessIdentity,
    ModelDescriptor,
    ProviderIdentity,
    ProviderReadiness,
    ReadinessState,
    WorkerHandle,
    WorkerResult,
    WorkerStatus,
)
from agent_cli.adapters.fake import FakeHarnessAdapter, FakeProviderAdapter
from agent_cli.config import Config
from agent_cli.config.model import WorkersConfig
from agent_cli.models import ModelCapability, ModelRegistry, parse_registry
from agent_cli.state import Task, TaskStatus, TaskStore
from agent_cli.workers import (
    WorkerDispatcher,
    WorkerExecutor,
    WorkerLaunchError,
    reconcile_orphaned_workers,
)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "pyproject.toml").write_text("[project]\nname='worker-test'\nversion='0.0.0'\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return root


def model(id: str, *, cost: str, score: int) -> dict[str, Any]:
    scores = dict.fromkeys((cap.value for cap in ModelCapability), score)
    return {
        "id": id,
        "display_name": id,
        "status": "active",
        "scores": scores,
        "latency": "fast" if cost == "low" else "medium",
        "cost": cost,
        "confidence": "medium",
        "sources": ["test"],
    }


def registry() -> ModelRegistry:
    return parse_registry(
        {
            "version": 1,
            "models": [
                model("small", cost="low", score=2),
                model("frontier", cost="high", score=5),
            ],
        }
    )


def runtime(auto_finish: bool = True) -> tuple[AdapterRegistry, FakeHarnessAdapter]:
    provider = FakeProviderAdapter(
        provider_identity=ProviderIdentity("sub", "Subscription", "fake", "subscription"),
        provider_readiness=ProviderReadiness(ReadinessState.READY),
        models=(ModelDescriptor("sub", "small"), ModelDescriptor("sub", "frontier")),
    )
    harness = FakeHarnessAdapter(
        harness_identity=HarnessIdentity("crew", "Crew"),
        auto_finish=auto_finish,
        outcome=WorkerResult(WorkerStatus.COMPLETED, "pytest: 42 passed"),
    )
    return AdapterRegistry([harness], [provider]), harness


def task(store: TaskStore) -> Task:
    session = store.start_session()
    root, _ = store.start_root_task(
        session.id,
        "Run pytest",
        kind="scout",
        complexity="trivial",
    )
    child = store.create_task(
        session.id,
        "Run pytest and report failures",
        parent_id=root.id,
        role="research_investigator",
    )
    return child


def test_trivial_dispatch_prefers_cheaper_adequate_model(tmp_path: Path) -> None:
    root = repository(tmp_path)
    store = TaskStore.for_repository(root)
    child = task(store)
    adapters, _ = runtime()

    choice = WorkerDispatcher(adapters, registry(), Config()).choose(child, "trivial", {})

    assert (choice.model_id, choice.provider_id, choice.harness_id) == ("small", "sub", "crew")


def test_scout_launch_is_non_blocking_and_uses_isolated_read_only_worker(tmp_path: Path) -> None:
    root = repository(tmp_path)
    store = TaskStore.for_repository(root)
    child = task(store)
    adapters, harness = runtime(auto_finish=False)

    started = WorkerExecutor(
        store,
        adapters,
        registry(),
        Config(),
        root,
        poll_seconds=0.01,
    ).launch(child, "scout", "trivial", child.goal)

    assert "started in background" in started
    [recorded] = store.workers(child.id)
    assert recorded.status is WorkerStatus.RUNNING
    [worker] = harness.workers.values()
    assert worker.request.authority == DIAGNOSTIC_AUTHORITY
    assert worker.request.mode is None
    assert worker.request.working_directory != root
    assert worker.request.working_directory.is_dir()
    assert git(root, "diff", "--name-only") == ""


def test_completed_scout_persists_summary_for_status(tmp_path: Path) -> None:
    root = repository(tmp_path)
    store = TaskStore.for_repository(root)
    child = task(store)
    adapters, _ = runtime(auto_finish=True)

    WorkerExecutor(store, adapters, registry(), Config(), root).launch(
        child, "scout", "trivial", child.goal
    )

    [recorded] = store.workers(child.id)
    assert recorded.status is WorkerStatus.COMPLETED
    [summary] = [a for a in store.artifacts(child.id) if a.kind == "worker-summary"]
    assert summary.metadata["summary"] == "pytest: 42 passed"


def test_background_completion_notifies_and_finishes_parent(tmp_path: Path) -> None:
    root = repository(tmp_path)
    store = TaskStore.for_repository(root)
    child = task(store)
    adapters, harness = runtime(auto_finish=False)
    notifications: list[str] = []
    executor = WorkerExecutor(
        store,
        adapters,
        registry(),
        Config(),
        root,
        poll_seconds=0.01,
        notify=notifications.append,
    )

    executor.launch(child, "scout", "trivial", child.goal)
    [token] = harness.workers
    harness.finish(WorkerHandle("crew", token))

    deadline = time.monotonic() + 1
    while not notifications and time.monotonic() < deadline:
        time.sleep(0.01)

    assert notifications and "completed" in notifications[-1]
    updated = store.task(child.id)
    assert updated is not None and updated.status is TaskStatus.COMPLETED
    parent = store.task(child.parent_id or "")
    assert parent is not None and parent.status is TaskStatus.COMPLETED
    [recorded] = store.workers(child.id)
    assert recorded.worktree is not None and not Path(recorded.worktree).exists()


def test_worker_concurrency_limit_is_enforced_before_second_launch(tmp_path: Path) -> None:
    root = repository(tmp_path)
    store = TaskStore.for_repository(root)
    first = task(store)
    assert first.parent_id is not None
    second = store.create_task(
        first.session_id,
        "Run another check",
        parent_id=first.parent_id,
        role="research_investigator",
    )
    adapters, _ = runtime(auto_finish=False)
    config = Config(workers=WorkersConfig(max_concurrency=1))
    executor = WorkerExecutor(store, adapters, registry(), config, root)

    executor.launch(first, "scout", "trivial", first.goal)

    with pytest.raises(WorkerLaunchError, match="concurrency limit"):
        executor.launch(second, "scout", "trivial", second.goal)
    assert store.workers(second.id) == []


def test_restart_reconciliation_never_replays_active_worker(tmp_path: Path) -> None:
    root = repository(tmp_path)
    store = TaskStore.for_repository(root)
    child = task(store)
    store.set_status(child.id, TaskStatus.IN_PROGRESS)
    worker = store.record_worker(
        child.id,
        "research_investigator",
        WorkerStatus.RUNNING,
        model="small",
        provider="sub",
        harness="crew",
        worktree="some-worktree",
    )

    assert reconcile_orphaned_workers(store) == 1
    assert store.workers(child.id)[0].id == worker.id
    assert store.workers(child.id)[0].status is WorkerStatus.FAILED
    updated = store.task(child.id)
    assert updated is not None and updated.status is TaskStatus.FAILED
    assert reconcile_orphaned_workers(store) == 0


def test_scout_source_mutation_is_rejected_even_if_harness_reports_success(tmp_path: Path) -> None:
    root = repository(tmp_path)
    store = TaskStore.for_repository(root)
    child = task(store)
    provider = FakeProviderAdapter(
        provider_identity=ProviderIdentity("sub", "Subscription", "fake", "subscription"),
        provider_readiness=ProviderReadiness(ReadinessState.READY),
        models=(ModelDescriptor("sub", "small"),),
    )
    harness = FakeHarnessAdapter(
        harness_identity=HarnessIdentity("crew", "Crew"),
        auto_finish=True,
        outcome=WorkerResult(
            WorkerStatus.COMPLETED,
            "done",
            changes=Changes(
                files=("src/example.py",),
                diff="diff --git a/src/example.py b/src/example.py\n",
            ),
        ),
    )
    adapters = AdapterRegistry([harness], [provider])

    WorkerExecutor(store, adapters, registry(), Config(), root).launch(
        child, "scout", "trivial", child.goal
    )

    [recorded] = store.workers(child.id)
    assert recorded.status is WorkerStatus.FAILED
    updated = store.task(child.id)
    assert updated is not None and updated.status is TaskStatus.FAILED
    [summary] = [a for a in store.artifacts(child.id) if a.kind == "worker-summary"]
    assert "modified tracked source" in summary.metadata["summary"]

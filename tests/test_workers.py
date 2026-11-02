from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from agent_cli.adapters import (
    AdapterRegistry,
    HarnessIdentity,
    ModelDescriptor,
    ProviderIdentity,
    ProviderReadiness,
    ReadinessState,
    WorkerResult,
    WorkerStatus,
)
from agent_cli.adapters.fake import FakeHarnessAdapter, FakeProviderAdapter
from agent_cli.config import Config
from agent_cli.models import ModelCapability, parse_registry
from agent_cli.state import TaskStore
from agent_cli.workers import WorkerDispatcher, WorkerExecutor


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


def registry():
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


def task(store: TaskStore):
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
    assert worker.request.mode == "plan-only"
    assert worker.request.working_directory != root
    assert worker.request.working_directory.is_dir()
    assert git(root, "status", "--porcelain") == ""


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

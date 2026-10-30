from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_cli.adapters import Selection
from agent_cli.state import Approval, TaskStateError, TaskStatus, TaskStore, status_lines
from agent_cli.state.migrations import MIGRATIONS
from agent_cli.state.task_store import TASKS_FILE
from gitrepo import locked


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "repo" / ".agent-cli" / "state" / TASKS_FILE


@pytest.fixture
def store(path: Path) -> TaskStore:
    return TaskStore(path)


def reopen(store: TaskStore) -> TaskStore:
    """Simulate a process restart: drop the connection and open the same file again."""
    store.close()
    return TaskStore(store.path)


def test_for_repository_uses_the_project_state_directory(tmp_path: Path) -> None:
    store = TaskStore.for_repository(tmp_path)
    assert store.path == tmp_path / ".agent-cli" / "state" / "tasks.sqlite"


def test_checking_for_state_does_not_create_the_database(store: TaskStore) -> None:
    assert not store.exists()
    assert status_lines(store) == ["No active task."]
    assert not store.path.exists()


def test_first_write_creates_a_self_ignoring_state_directory(store: TaskStore) -> None:
    store.start_session()
    assert store.exists()
    assert (store.path.parent / ".gitignore").read_text().splitlines()[-1] == "*"


def test_task_tree_and_dependencies_survive_restart(store: TaskStore) -> None:
    session = store.start_session("cache expiration")
    parent = store.create_task(session.id, "Add expiration", role="orchestrator")
    spec = store.create_task(session.id, "Write spec", parent_id=parent.id)
    build = store.create_task(session.id, "Implement", parent_id=parent.id)
    store.add_dependency(build.id, spec.id)

    store = reopen(store)
    assert store.latest_session() == session
    assert [t.id for t in store.tasks(session.id)] == [parent.id, spec.id, build.id]
    assert [t.id for t in store.subtasks(parent.id)] == [spec.id, build.id]
    assert store.dependencies(build.id) == [spec.id]
    assert store.task(parent.id) == parent


def test_a_task_cannot_depend_on_itself(store: TaskStore) -> None:
    session = store.start_session()
    task = store.create_task(session.id, "Loop")
    with pytest.raises(TaskStateError):
        store.add_dependency(task.id, task.id)


def test_tasks_cannot_link_across_sessions(store: TaskStore) -> None:
    first = store.create_task(store.start_session().id, "A")
    second = store.start_session()
    with pytest.raises(TaskStateError, match="another session"):
        store.create_task(second.id, "B", parent_id=first.id)


def test_unknown_task_fails_without_partial_writes(store: TaskStore) -> None:
    session = store.start_session()
    before = store.events(session.id)
    with pytest.raises(TaskStateError, match="unknown task"):
        store.add_blocker("task-missing", "nothing")
    assert store.events(session.id) == before


def test_status_transitions_are_recorded_as_events(store: TaskStore) -> None:
    session = store.start_session()
    task = store.create_task(session.id, "Work")
    store.set_status(task.id, TaskStatus.IN_PROGRESS)
    store.set_status(task.id, TaskStatus.BLOCKED, reason="needs a decision")
    store.assign_role(task.id, "implementer")

    task_after = store.task(task.id)
    assert task_after is not None and task_after.status is TaskStatus.BLOCKED
    assert task_after.role == "implementer"
    changes = [e for e in store.events(session.id, task.id) if e.kind == "status_changed"]
    assert [(e.detail["old"], e.detail["new"]) for e in changes] == [
        ("pending", "in_progress"),
        ("in_progress", "blocked"),
    ]
    assert changes[1].detail["reason"] == "needs a decision"


def test_requirement_changes_keep_the_original_intent(store: TaskStore) -> None:
    session = store.start_session()
    original = store.add_requirement(session.id, "Expire entries after 5 minutes")
    changed = store.add_requirement(session.id, "Expire after 10 minutes", supersedes=original.id)

    assert store.requirements(session.id) == [changed]
    history = store.requirements(session.id, current_only=False)
    assert [r.text for r in history] == [original.text, changed.text]
    assert history[0].superseded_by == changed.id
    with pytest.raises(TaskStateError, match="already superseded"):
        store.add_requirement(session.id, "Again", supersedes=original.id)


def test_decisions_are_superseded_not_overwritten(store: TaskStore) -> None:
    session = store.start_session()
    first = store.record_decision(session.id, "TTL default?", "5m", "user", "safe default")
    second = store.record_decision(session.id, "TTL default?", "10m", "user", supersedes=first.id)
    assert store.decisions(session.id) == [second]
    assert store.decisions(session.id, current_only=False)[0].superseded_by == second.id


def test_assumptions_are_unconfirmed_until_resolved(store: TaskStore) -> None:
    task = store.create_task(store.start_session().id, "Work")
    assumption = store.add_assumption(task.id, "Callers never pass ttl=0", "orchestrator")
    assert store.assumptions(task.id)[0].confirmed is None
    store.resolve_assumption(assumption.id, confirmed=False)
    assert store.assumptions(task.id)[0].confirmed is False


def test_spec_versions_are_kept_with_diffs_and_approval(store: TaskStore) -> None:
    task = store.create_task(store.start_session().id, "Work")
    first = store.add_spec_version(task.id, "Add ttl.\n")
    store.set_spec_approval(task.id, 1, Approval.APPROVED)
    second = store.add_spec_version(task.id, "Add ttl.\nKeep callers unchanged.\n")

    versions = store.spec_versions(task.id)
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].body == first.body and versions[0].approval is Approval.APPROVED
    assert first.diff == ""
    assert "+Keep callers unchanged." in second.diff
    assert store.latest_spec(task.id) == second


def test_voiding_withdraws_only_approved_specs(store: TaskStore) -> None:
    session = store.start_session()
    approved = store.create_task(session.id, "A")
    pending = store.create_task(session.id, "B")
    store.add_spec_version(approved.id, "spec")
    store.add_spec_version(pending.id, "spec")
    store.set_spec_approval(approved.id, 1, Approval.APPROVED)

    voided = store.void_spec_approvals([approved.id, pending.id], "requirement changed")
    assert voided == [approved.id]
    assert store.spec_versions(approved.id)[0].approval is Approval.VOIDED
    assert store.spec_versions(pending.id)[0].approval is Approval.PENDING


def test_user_pins_survive_restart_on_the_right_subtask(store: TaskStore) -> None:
    session = store.start_session()
    parent = store.create_task(session.id, "Feature")
    child = store.create_task(session.id, "Implement", parent_id=parent.id)
    store.pin(child.id, "model", "claude-opus-5-5")
    store.pin(child.id, "harness", "pi")
    store.pin(child.id, "provider", "local-vllm", user_pinned=False)

    store = reopen(store)
    pins = store.pins(child.id)
    assert pins["model"].selection() == Selection.pin("claude-opus-5-5")
    assert pins["harness"].selection() == Selection.pin("pi")
    assert pins["provider"].selection() == Selection("local-vllm", pinned=False)
    assert store.pins(parent.id) == {}


def test_a_new_pin_supersedes_the_old_one_and_keeps_history(store: TaskStore) -> None:
    task = store.create_task(store.start_session().id, "Work")
    old = store.pin(task.id, "model", "model-a")
    new = store.pin(task.id, "model", "model-b")
    assert store.pins(task.id) == {"model": new}
    assert [p.id for p in store.pin_history(task.id)] == [old.id, new.id]

    assert store.clear_pin(task.id, "model")
    assert store.pins(task.id) == {}
    assert not store.clear_pin(task.id, "model")
    assert len(store.pin_history(task.id)) == 2


@pytest.mark.parametrize(("kind", "value"), [("region", "eu"), ("model", "")])
def test_invalid_pins_are_rejected(store: TaskStore, kind: str, value: str) -> None:
    task = store.create_task(store.start_session().id, "Work")
    with pytest.raises(TaskStateError):
        store.pin(task.id, kind, value)  # type: ignore[arg-type]


def test_workers_findings_blockers_reviews_and_artifacts(store: TaskStore) -> None:
    session = store.start_session()
    task = store.create_task(session.id, "Work")
    worker = store.record_worker(task.id, "implementer", "running", harness="pi", worktree="wt/1")
    store.update_worker(worker.id, status="completed")
    finding = store.add_finding(task.id, "code_reviewer", "high", "Missing test")
    store.resolve_finding(finding.id, "added test")
    blocker = store.add_blocker(task.id, "Choose TTL default")
    store.record_review(task.id, "code", 1, "changes_requested", "reviewer")
    store.add_artifact(task.id, "diff", "artifacts/1.diff", {"files": "2"})

    store = reopen(store)
    assert store.workers(task.id)[0].status == "completed"
    assert store.findings(task.id, open_only=True) == []
    assert store.findings(task.id)[0].resolution == "added test"
    assert [b.id for b in store.blockers(session.id)] == [blocker.id]
    store.resolve_blocker(blocker.id, "5 minutes")
    assert store.blockers(session.id) == []
    assert store.reviews(task.id)[0].result == "changes_requested"
    assert store.artifacts(task.id)[0].metadata == {"files": "2"}
    kinds = [e.kind for e in store.events(session.id)]
    assert "worker_status_changed" in kinds and "blocker_resolved" in kinds


def test_latest_session_prefers_open_sessions(store: TaskStore) -> None:
    open_session = store.start_session("open")
    closed = store.start_session("closed")
    store.close_session(closed.id)
    latest = store.latest_session()
    assert latest is not None and latest.id == open_session.id


# -- Durability ---------------------------------------------------------------------------------


def seeded(store: TaskStore) -> str:
    session = store.start_session()
    task = store.create_task(session.id, "Keep me")
    store.pin(task.id, "model", "model-a")
    store.close()
    return task.id


def test_migration_preserves_canonical_records(store: TaskStore) -> None:
    task_id = seeded(store)
    v2 = ("ALTER TABLE tasks ADD COLUMN priority INTEGER",)
    upgraded = TaskStore(store.path, migrations=(*MIGRATIONS, v2))
    task = upgraded.tasks(upgraded.latest_session().id)[0]  # type: ignore[union-attr]
    assert task.id == task_id
    assert upgraded.pins(task_id)["model"].value == "model-a"
    assert schema_version(store.path) == len(MIGRATIONS) + 1


def test_failed_migration_leaves_the_database_unchanged(store: TaskStore) -> None:
    task_id = seeded(store)
    broken = ("ALTER TABLE tasks ADD COLUMN priority INTEGER", "ALTER TABLE missing ADD x")
    with pytest.raises(TaskStateError, match="left unchanged"):
        TaskStore(store.path, migrations=(*MIGRATIONS, broken)).tasks("any")

    assert schema_version(store.path) == len(MIGRATIONS)
    intact = TaskStore(store.path)
    assert intact.task(task_id) is not None
    assert intact.pins(task_id)["model"].value == "model-a"


def test_newer_schema_is_refused_and_kept(store: TaskStore) -> None:
    seeded(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(TaskStateError, match="newer"):
        TaskStore(store.path).latest_session()
    assert schema_version(store.path) == 99


def test_corrupt_database_is_reported_and_never_deleted(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a database at all" * 100)
    with pytest.raises(TaskStateError, match="cannot use task state"):
        TaskStore(path).latest_session()
    assert path.read_bytes() == b"not a database at all" * 100


def test_foreign_database_is_not_adopted(path: Path) -> None:
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE other (x)")
    with pytest.raises(TaskStateError, match="not an Agent CLI"):
        TaskStore(path).latest_session()


def schema_version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        version: int = connection.execute("PRAGMA user_version").fetchone()[0]
    return version


def test_a_locked_database_reports_task_state_error(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = store.start_session()
    monkeypatch.setattr("agent_cli.state.task_store.BUSY_TIMEOUT", 0.05)
    busy = TaskStore(store.path)
    busy.latest_session()
    with locked(store.path), pytest.raises(TaskStateError, match="cannot update"):
        busy.create_task(session.id, "Blocked")
    assert busy.create_task(session.id, "Later").goal == "Later"


def test_records_cannot_link_across_sessions(store: TaskStore) -> None:
    first = store.start_session()
    task = store.create_task(first.id, "A")
    requirement = store.add_requirement(first.id, "Original")
    decision = store.record_decision(first.id, "Q?", "yes", "user")
    second = store.start_session()

    with pytest.raises(TaskStateError, match="another session"):
        store.add_requirement(second.id, "Moved", task_id=task.id)
    with pytest.raises(TaskStateError, match="another session"):
        store.add_requirement(second.id, "Replace", supersedes=requirement.id)
    with pytest.raises(TaskStateError, match="another session"):
        store.record_decision(second.id, "Q?", "no", "user", supersedes=decision.id)
    other_task = store.create_task(second.id, "B")
    with pytest.raises(TaskStateError, match="not in this session"):
        store.add_finding(other_task.id, "reviewer", "low", "x", requirement_id=requirement.id)
    assert store.requirements(second.id) == []
    assert store.requirements(first.id) == [requirement]

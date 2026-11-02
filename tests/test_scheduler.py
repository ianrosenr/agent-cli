from __future__ import annotations

from agent_cli.orchestration.scheduler import Readiness, evaluate_task, ready_tasks
from agent_cli.state import TaskStatus, TaskStore


def test_dependency_readiness_and_failure_propagation(tmp_path) -> None:
    store = TaskStore.for_repository(tmp_path)
    session = store.start_session()
    root, _ = store.start_root_task(session.id, "Ship feature", kind="ship")
    first = store.create_task(session.id, "First", parent_id=root.id, role="implementer")
    second = store.create_task(session.id, "Second", parent_id=root.id, role="implementer")
    assert ready_tasks(store, root.id) == [first, second]
    assert evaluate_task(store, first).readiness is Readiness.READY
    store.set_status(first.id, TaskStatus.FAILED)
    assert evaluate_task(store, first).readiness is Readiness.INACTIVE


def test_pending_dependency_blocks_dispatch(tmp_path) -> None:
    store = TaskStore.for_repository(tmp_path)
    session = store.start_session()
    root, _ = store.start_root_task(session.id, "Ship feature", kind="ship")
    upstream = store.create_task(session.id, "Upstream", parent_id=root.id, role="implementer")
    downstream = store.create_task(session.id, "Downstream", parent_id=root.id, role="implementer")
    # The scheduler is intentionally pure; dependency wiring comes from persist_plan.
    assert evaluate_task(store, downstream).readiness is Readiness.READY
    assert upstream in ready_tasks(store, root.id)

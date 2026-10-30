"""A concise, human-readable view of canonical task state for ``/status``."""

from __future__ import annotations

from collections import defaultdict

from agent_cli.state.models import PIN_KINDS, Task
from agent_cli.state.task_store import TaskStore

NO_ACTIVE_TASK = "No active task."
INDENT = "  "


def status_lines(store: TaskStore) -> list[str]:
    """The latest session's task tree, roles, pins, and open blockers; never raw events.

    Reading status does not create a task-state database.
    """
    if not store.exists():
        return [NO_ACTIVE_TASK]
    session = store.latest_session()
    tasks = [] if session is None else store.tasks(session.id)
    if session is None or not tasks:
        return [NO_ACTIVE_TASK]

    state = "open" if session.open else "closed"
    title = f": {session.title}" if session.title else ""
    lines = [f"Session {session.id}{title} ({state}, started {session.created_at})", "", "Tasks:"]

    children: defaultdict[str | None, list[Task]] = defaultdict(list)
    for task in tasks:
        children[task.parent_id].append(task)

    def walk(parent: str | None, depth: int) -> None:
        for task in children[parent]:
            lines.append(INDENT * (depth + 1) + task_line(store, task))
            walk(task.id, depth + 1)

    walk(None, 0)

    blockers = store.blockers(session.id)
    if blockers:
        lines += ["", "Blockers:"]
        lines += [
            f"{INDENT}{blocker.task_id}: {blocker.reason} (waiting on {blocker.owner})"
            for blocker in blockers
        ]
    return lines


def task_line(store: TaskStore, task: Task) -> str:
    parts = [f"[{task.status}] {task.goal} ({task.id})"]
    if task.role:
        parts.append(f"role: {task.role}")
    pins = store.pins(task.id)
    if pins:
        described = [
            f"{kind}={pins[kind].value}" + ("" if pins[kind].user_pinned else " (default)")
            for kind in PIN_KINDS
            if kind in pins
        ]
        parts.append("pinned: " + ", ".join(described))
    spec = store.latest_spec(task.id)
    if spec is not None:
        parts.append(f"spec v{spec.version} {spec.approval}")
    return " · ".join(parts)

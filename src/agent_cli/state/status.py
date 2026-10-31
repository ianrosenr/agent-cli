"""A concise, human-readable view of canonical task state for ``/status``."""

from __future__ import annotations

from collections import defaultdict

from agent_cli.state.models import ALL_CHILDREN, PIN_KINDS, ScopedPin, Task
from agent_cli.state.task_store import TaskStore

NO_ACTIVE_TASK = "No active task."
INDENT = "  "


def status_lines(store: TaskStore) -> list[str]:
    """The latest session's task tree, roles, pins, role-scoped pins, and open blockers; never
    raw events.

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

    later = [
        f"{INDENT}{pin.root_task_id}: {pin.scope} {pin.kind}={pin.value}"
        for task in children[None]
        for pin in pins_for_later_roles(store, task.id)
    ]
    if later:
        lines += ["", "Pinned for later roles:", *later]

    blockers = store.blockers(session.id)
    if blockers:
        lines += ["", "Blockers:"]
        lines += [
            f"{INDENT}{blocker.task_id}: {blocker.reason} (waiting on {blocker.owner})"
            for blocker in blockers
        ]
    return lines


def pins_for_later_roles(store: TaskStore, root_id: str) -> list[ScopedPin]:
    """Role-scoped user pins no child holds yet, such as a reviewer pin for V1-12."""
    roles = {task.role for task in store.subtasks(root_id)}
    return [
        pin
        for pin in store.scoped_pins(root_id)
        if pin.scope != ALL_CHILDREN and pin.scope not in roles
    ]


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

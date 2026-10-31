"""Compact, readable planning output for the root prompt.

Rendered from canonical task state, never from raw model output: no JSON, no skill bodies, no
index internals, no hidden reasoning. Hierarchy comes from numbering, alignment, and semantic
styles rather than borders.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from agent_cli.state import PIN_KINDS, PlanningQuestion, Task, TaskStore
from agent_cli.state.status import pins_for_later_roles
from agent_cli.ui import blocks, bullets, error, fields, heading, muted, section, styled, warning

READY = "Ready for spec review. Nothing has been implemented."
INCOMPLETE = "Planning is incomplete."
SEE_STATUS = "See /status."
ANSWER_HINT = "Your next line is taken as the answer; /cancel withdraws the request instead."
NONE = "—"


def render_plan(
    store: TaskStore,
    root_id: str,
    summary: str,
    unpinned: Sequence[str] = (),
    problems: Sequence[str] = (),
    planned_by: Sequence[str] = (),
) -> RenderableType:
    children = store.subtasks(root_id)
    number = {task.id: str(index) for index, task in enumerate(children, 1)}
    steps = Table.grid(padding=(0, 2))
    steps.add_column(no_wrap=True, style="step", justify="right")
    steps.add_column(overflow="fold")
    for index, task in enumerate(children):
        if index:
            steps.add_row("", "")
        steps.add_row(number[task.id], work_item(store, task, number))

    later = [f"{p.scope}: {p.kind}={p.value}" for p in pins_for_later_roles(store, root_id)]
    assumptions = [a.text for a in store.assumptions(root_id)]
    outcome = styled(READY, "success") if not problems else warning(INCOMPLETE, SEE_STATUS)
    return blocks(
        Group(heading("Plan"), Text(summary)),
        steps if children else None,
        section("Pinned for later roles", bullets(later)) if later else None,
        section("Assumptions (unconfirmed)", bullets(assumptions)) if assumptions else None,
        section("Not pinned", bullets(unpinned)) if unpinned else None,
        Group(styled("Blocked", "label.error"), bullets(problems)) if problems else None,
        Group(
            outcome,
            *(
                [muted("Planned with " + "; ".join(dict.fromkeys(planned_by)) + ".")]
                if planned_by
                else []
            ),
        ),
    )


def work_item(store: TaskStore, task: Task, number: dict[str, str]) -> RenderableType:
    """One child: its role, its goal, then aligned details."""
    after = [number.get(dependency, dependency) for dependency in store.dependencies(task.id)]
    routing = store.latest_skill_routing(task.id)
    skills = "not routed" if routing is None else ", ".join(s.name for s in routing.selected)
    details: list[tuple[str, RenderableType]] = [
        ("Depends", ", ".join(after) or NONE),
        ("Skills", skills or "none"),
    ]
    spec = store.latest_spec(task.id)
    if spec is not None:
        state = "pending review" if spec.approval == "pending" else str(spec.approval)
        details.append(("Spec", f"v{spec.version} · {state}"))
    pins = store.pins(task.id)
    if pins:
        pinned = ", ".join(f"{kind}={pins[kind].value}" for kind in PIN_KINDS if kind in pins)
        details.append(("Pinned", pinned))
    return Group(styled(task.role or "unassigned", "role"), Text(task.goal), fields(details))


def render_question(question: PlanningQuestion, summary: str = "") -> RenderableType:
    asked = Group(
        styled("Question", "step"),
        styled(question.question, "value"),
        Text.assemble(("Why it matters: ", "muted"), question.why),
    )
    return blocks(Text(summary) if summary else None, asked, muted(ANSWER_HINT))


def render_stopped(reason: str) -> RenderableType:
    return error(f"Planning stopped: {reason}", "Your request is saved; see /status.")

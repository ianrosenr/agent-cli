"""Compact, readable planning output for the root prompt.

Rendered from canonical task state, never from raw model output: no JSON, no skill bodies, no
index internals, no hidden reasoning.
"""

from __future__ import annotations

from collections.abc import Sequence

from agent_cli.state import PIN_KINDS, PlanningQuestion, TaskStore

INDENT = "   "
READY = "Ready for spec review. Nothing has been implemented."
ANSWER_HINT = "Reply at the prompt: your next line is taken as the answer."


def render_plan(
    store: TaskStore,
    root_id: str,
    summary: str,
    preferences: Sequence[str] = (),
    problems: Sequence[str] = (),
    planned_by: Sequence[str] = (),
) -> str:
    children = store.subtasks(root_id)
    number = {task.id: str(index) for index, task in enumerate(children, 1)}
    lines = ["Plan", summary, ""]
    for task in children:
        lines.append(f"{number[task.id]}. [{task.role}] {task.goal}")
        after = [number.get(dependency, dependency) for dependency in store.dependencies(task.id)]
        lines.append(f"{INDENT}depends on: {', '.join(after) or '—'}")
        routing = store.latest_skill_routing(task.id)
        skills = "not routed" if routing is None else ", ".join(s.name for s in routing.selected)
        lines.append(f"{INDENT}skills: {skills or 'none'}")
        spec = store.latest_spec(task.id)
        if spec is not None:
            state = "pending review" if spec.approval == "pending" else str(spec.approval)
            lines.append(f"{INDENT}spec: v{spec.version} {state}")
        pins = store.pins(task.id)
        if pins:
            pinned = ", ".join(f"{kind}={pins[kind].value}" for kind in PIN_KINDS if kind in pins)
            lines.append(f"{INDENT}pinned: {pinned}")
    later = [
        f"- {pin.scope}: {pin.kind}={pin.value}"
        for pin in store.scoped_pins(root_id)
        if pin.scope != "all" and not any(task.role == pin.scope for task in children)
    ]
    sections = [
        ("Pinned for later work", later),
        ("Assumptions (unconfirmed)", [f"- {a.text}" for a in store.assumptions(root_id)]),
        ("Preferences, not pins", [f'- "{text}"' for text in preferences]),
        ("Blocked", [f"- {problem}" for problem in problems]),
    ]
    for title, body in sections:
        if body:
            lines += ["", title, *body]
    lines += ["", READY if not problems else "Planning is incomplete. See /status."]
    if planned_by:
        lines.append("Planned with " + "; ".join(dict.fromkeys(planned_by)) + ".")
    return "\n".join(lines)


def render_question(question: PlanningQuestion, summary: str = "") -> str:
    lines = ["Question", question.question, f"Why it matters: {question.why}", "", ANSWER_HINT]
    return "\n".join(([summary, ""] if summary else []) + lines)


def render_stopped(reason: str) -> str:
    return f"Planning stopped: {reason}\nYour request is saved; see /status."

"""A concise view of the effective skill catalog and recent routing for ``/skills``.

Shows metadata and routing reasons only, never skill bodies.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from rich.console import Group, RenderableType
from rich.text import Text

from agent_cli.config import Config, ConfigError, ConfigService
from agent_cli.config.model import SkillActivation
from agent_cli.skills.discovery import SkillCatalog, load_catalog, skill_sources
from agent_cli.skills.models import SkillError
from agent_cli.skills.policy import load_policy
from agent_cli.skills.selection import activation_of
from agent_cli.state import SkillRouting, TaskStateError, TaskStore
from agent_cli.ui import blocks, error, muted, section, warning
from agent_cli.ui import table as render_table

INDENT = "  "
HEADERS = ("Skill", "Source", "Version", "Activation", "Tokens")


def skills_view(
    config: ConfigService | None, tasks: TaskStore | None, home: Path | None = None
) -> RenderableType:
    try:
        settings = Config().skills if config is None else config.resolve().skills
    except ConfigError as problem:
        return error("Cannot read configuration.", str(problem))
    root = None if config is None else config.paths.project_root
    try:
        catalog = load_catalog(skill_sources(settings.paths, root, home))
    except SkillError as problem:
        return error("Cannot load skills.", str(problem))

    header = Text.assemble(
        ("Skills", "heading"),
        f"  {len(catalog)} available",
        (f" · budget {settings.context_budget_tokens} tokens", "muted"),
        (f" · estimator {catalog.estimator_id}", "muted"),
    )
    unknown = sorted(name for name in settings.activation if name not in catalog)
    ignored = (
        warning("Ignored skills.activation entries (no such skill).", ", ".join(unknown))
        if unknown
        else None
    )
    try:
        load_policy().check_catalog(catalog)
        policy = None
    except SkillError as problem:
        policy = error("Routing policy problem.", str(problem))
    return blocks(header, table(catalog, settings.activation), ignored, policy, routing_view(tasks))


def table(catalog: SkillCatalog, activation: Mapping[str, SkillActivation]) -> RenderableType:
    rows = [
        (
            skill.name,
            f"{skill.source.kind.value}:{skill.source.root.resolve()}",
            skill.version,
            activation_of(skill.name, activation),
            str(skill.tokens),
        )
        for skill in catalog
    ]
    return render_table(
        HEADERS, rows, styles=("value", "path", None, None, None), right={len(HEADERS) - 1}
    )


def routing_view(tasks: TaskStore | None) -> RenderableType | None:
    """The latest routing decision of each task in the latest session, if any."""
    if tasks is None or not tasks.exists():
        return None
    try:
        session = tasks.latest_session()
        routed = [] if session is None else _latest_routings(tasks, session.id)
    except TaskStateError as problem:
        return error("Cannot read task state.", str(problem))
    if not routed:
        return None
    lines: list[Text] = []
    for goal, routing in routed:
        lines += describe(goal, routing)
    return section("Routing", Group(*lines))


def _latest_routings(tasks: TaskStore, session_id: str) -> list[tuple[str, SkillRouting]]:
    latest = tasks.latest_skill_routings(session_id)
    return [(task.goal, latest[task.id]) for task in tasks.tasks(session_id) if task.id in latest]


def describe(goal: str, routing: SkillRouting) -> list[Text]:
    classifier = (
        "no classifier"
        if routing.classifier is None
        else f"classifier {routing.classifier} ({routing.classifier_mode})"
    )
    lines = [
        Text.assemble(
            f"{INDENT}{goal} ",
            (f"({routing.task_id})", "muted"),
            " · role: ",
            (routing.role, "role"),
            (
                f" · {routing.kind} · {routing.total_tokens}/{routing.budget_tokens} tokens"
                f" · policy v{routing.policy_version} · {classifier}",
                "muted",
            ),
        )
    ]
    if routing.reason:
        lines.append(Text(f"{INDENT * 2}escalated: {routing.reason}", style="warning"))
    lines += [
        Text.assemble(
            f"{INDENT * 2}", ("+ ", "success"), (skill.name, "value"), muted(f" — {skill.reason}")
        )
        for skill in routing.selected
    ]
    lines += [
        Text.assemble(f"{INDENT * 2}", ("- ", "muted"), skill.name, muted(f" — {skill.reason}"))
        for skill in routing.dropped
    ]
    by_source: dict[str, list[str]] = {}
    for signal in routing.signals:
        by_source.setdefault(signal.source, []).append(signal.signal)
    if by_source:
        summary = "; ".join(
            f"{source}: {', '.join(sorted(names))}" for source, names in by_source.items()
        )
        lines.append(muted(f"{INDENT * 2}signals — {summary}"))
    return lines

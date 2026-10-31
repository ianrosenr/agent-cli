"""A concise view of the effective skill catalog and recent routing for ``/skills``.

Shows metadata and routing reasons only, never skill bodies.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from agent_cli.config import Config, ConfigError, ConfigService
from agent_cli.config.model import SkillActivation
from agent_cli.skills.discovery import SkillCatalog, load_catalog, skill_sources
from agent_cli.skills.models import SkillError
from agent_cli.skills.policy import load_policy
from agent_cli.skills.selection import activation_of
from agent_cli.state import SkillRouting, TaskStateError, TaskStore

INDENT = "  "
HEADERS = ("Skill", "Source", "Version", "Activation", "Tokens")


def skills_lines(
    config: ConfigService | None, tasks: TaskStore | None, home: Path | None = None
) -> list[str]:
    try:
        settings = Config().skills if config is None else config.resolve().skills
    except ConfigError as error:
        return [f"Cannot read configuration: {error}"]
    root = None if config is None else config.paths.project_root
    try:
        catalog = load_catalog(skill_sources(settings.paths, root, home))
    except SkillError as error:
        return [f"Cannot load skills: {error}"]

    lines = [
        f"Skills: {len(catalog)} available · budget {settings.context_budget_tokens} tokens "
        f"· estimator {catalog.estimator_id}",
        "",
        *table(catalog, settings.activation),
    ]
    unknown = sorted(name for name in settings.activation if name not in catalog)
    if unknown:
        lines += ["", f"Ignored skills.activation entries (no such skill): {', '.join(unknown)}"]
    try:
        load_policy().check_catalog(catalog)
    except SkillError as error:
        lines += ["", f"Routing policy problem: {error}"]
    lines += routing_lines(tasks)
    return lines


def table(catalog: SkillCatalog, activation: Mapping[str, SkillActivation]) -> list[str]:
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
    widths = [max(len(row[i]) for row in [HEADERS, *rows]) for i in range(len(HEADERS))]

    def line(row: tuple[str, ...]) -> str:
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=True)]
        cells[-1] = row[-1].rjust(widths[-1])
        return (INDENT + "  ".join(cells)).rstrip()

    return [line(HEADERS), *(line(row) for row in rows)]


def routing_lines(tasks: TaskStore | None) -> list[str]:
    """The latest routing decision of each task in the latest session, if any."""
    if tasks is None or not tasks.exists():
        return []
    try:
        session = tasks.latest_session()
        routed = [] if session is None else _latest_routings(tasks, session.id)
    except TaskStateError as error:
        return ["", f"Cannot read task state: {error}"]
    if not routed:
        return []
    lines = ["", "Routing:"]
    for goal, routing in routed:
        lines += describe(goal, routing)
    return lines


def _latest_routings(tasks: TaskStore, session_id: str) -> list[tuple[str, SkillRouting]]:
    latest = tasks.latest_skill_routings(session_id)
    return [(task.goal, latest[task.id]) for task in tasks.tasks(session_id) if task.id in latest]


def describe(goal: str, routing: SkillRouting) -> list[str]:
    classifier = (
        "no classifier"
        if routing.classifier is None
        else f"classifier {routing.classifier} ({routing.classifier_mode})"
    )
    lines = [
        f"{INDENT}{goal} ({routing.task_id}) · role: {routing.role} · {routing.kind} · "
        f"{routing.total_tokens}/{routing.budget_tokens} tokens · policy v{routing.policy_version}"
        f" · {classifier}"
    ]
    if routing.reason:
        lines.append(f"{INDENT * 2}escalated: {routing.reason}")
    lines += [f"{INDENT * 2}+ {skill.name} — {skill.reason}" for skill in routing.selected]
    lines += [f"{INDENT * 2}- {skill.name} — {skill.reason}" for skill in routing.dropped]
    by_source: dict[str, list[str]] = {}
    for signal in routing.signals:
        by_source.setdefault(signal.source, []).append(signal.signal)
    if by_source:
        summary = "; ".join(
            f"{source}: {', '.join(sorted(names))}" for source, names in by_source.items()
        )
        lines.append(f"{INDENT * 2}signals — {summary}")
    return lines

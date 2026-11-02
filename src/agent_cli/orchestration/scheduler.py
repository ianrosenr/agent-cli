"""Deterministic dependency readiness for persisted worker tasks.

This module does not launch workers or authorize writes. A source-changing dependent
task additionally needs an integrated upstream worktree before dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agent_cli.state import Task, TaskStatus, TaskStore


class Readiness(StrEnum):
    READY = "ready"
    WAITING = "waiting"
    FAILED = "failed"
    INACTIVE = "inactive"


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    task: Task
    readiness: Readiness
    reason: str


def evaluate_task(store: TaskStore, task: Task) -> ScheduledTask:
    """Assess dependencies without side effects or model calls."""
    if task.status is not TaskStatus.PENDING:
        return ScheduledTask(task, Readiness.INACTIVE, f"task is {task.status}")
    dependencies = [store.task(task_id) for task_id in store.dependencies(task.id)]
    if any(dependency is None for dependency in dependencies):
        return ScheduledTask(task, Readiness.FAILED, "dependency was deleted")
    if any(
        dependency is not None and dependency.status in (TaskStatus.CANCELED, TaskStatus.FAILED)
        for dependency in dependencies
    ):
        return ScheduledTask(task, Readiness.FAILED, "dependency failed or was canceled")
    if any(
        dependency is not None and dependency.status is not TaskStatus.COMPLETED
        for dependency in dependencies
    ):
        return ScheduledTask(task, Readiness.WAITING, "waiting for dependencies")
    return ScheduledTask(task, Readiness.READY, "all dependencies completed")


def ready_tasks(store: TaskStore, parent_id: str) -> list[Task]:
    """Return eligible children in persisted order, including independent tasks."""
    return [
        outcome.task
        for task in store.subtasks(parent_id)
        if (outcome := evaluate_task(store, task)).readiness is Readiness.READY
    ]

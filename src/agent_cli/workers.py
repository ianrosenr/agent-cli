"""Generic worker dispatch, isolated worktrees, and non-blocking lifecycle."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from agent_cli.adapters import (
    AdapterError,
    AdapterRegistry,
    Capability,
    Selection,
    WorkerHandle,
    WorkerRequest,
    WorkerResult,
    WorkerStatus,
)
from agent_cli.config import Config
from agent_cli.config.model import RoleFloors, Tier
from agent_cli.models import (
    ModelPath,
    ModelPin,
    ModelRegistry,
    pinned_paths,
    ranked_model_candidates,
    runtime_paths,
)
from agent_cli.skills import SkillError, load_catalog, skill_sources
from agent_cli.state import PinKind, Task, TaskStateError, TaskStatus, TaskStore
from agent_cli.terms import Role, TaskComplexity, TaskKind


class WorkerLaunchError(Exception):
    """A prepared task cannot be launched safely."""


_LAUNCH_LOCK = threading.Lock()
ACTIVE_WORKER_STATUSES = {
    WorkerStatus.STARTING,
    WorkerStatus.RUNNING,
    WorkerStatus.WAITING,
}


@dataclass(frozen=True, slots=True)
class Dispatch:
    model_id: str
    provider_id: str
    harness_id: str
    reason: str


def floors_for(config: Config, role: Role, complexity: TaskComplexity) -> RoleFloors:
    """Lower only the task role's floor for trivial work; complex work requires high."""
    floor: Tier | None = (
        "low" if complexity == "trivial" else "high" if complexity == "complex" else None
    )
    if floor is None:
        return config.routing.role_floors
    changes = cast(Any, {role: floor})
    return replace(config.routing.role_floors, **changes)


@dataclass(slots=True)
class WorkerDispatcher:
    adapters: AdapterRegistry
    registry: ModelRegistry
    config: Config

    def choose(
        self,
        task: Task,
        complexity: TaskComplexity,
        pins: dict[PinKind, str],
    ) -> Dispatch:
        runtime = runtime_paths(self.adapters, self.config)
        role = cast(Role, task.role or "research_investigator")
        model_pin = pins.get("model")
        provider_pin = pins.get("provider")
        harness_pin = pins.get("harness")

        paths: list[tuple[str, ModelPath]] = []
        if model_pin:
            resolved = pinned_paths(
                self.registry,
                runtime,
                self.config.access.preference,
                pin=self._model_pin(model_pin, task.id),
            )
            paths = [(resolved.model_id, path) for path in resolved.paths]
        else:
            candidates = ranked_model_candidates(
                self.registry,
                role,
                floors_for(self.config, role, complexity),
                runtime,
                self.config.access.preference,
            )
            ranked = list(candidates)
            if complexity == "trivial":
                ranked.sort(
                    key=lambda candidate: (
                        candidate.entry.cost.rank,
                        candidate.entry.latency.rank,
                        -candidate.entry.confidence.rank,
                        candidate.model_id,
                    )
                )
            paths = [
                (candidate.model_id, path)
                for candidate in ranked
                for path in candidate.paths
            ]

        for _, path in paths:
            if provider_pin and path.provider.id != provider_pin:
                continue
            # Worker execution over paid API is held until harness-level hard spend bounds exist.
            if path.access == "api":
                continue
            for harness_id, harness in sorted(self.adapters.harnesses().items()):
                if harness_pin and harness_id != harness_pin:
                    continue
                if not harness.readiness().ready:
                    continue
                if not harness.capabilities().supports(Capability.START_WORKER):
                    continue
                probe = WorkerRequest(
                    task.id,
                    task.role or "research_investigator",
                    task.goal,
                    Path("."),
                    model=Selection(path.model_id, pinned=bool(model_pin)),
                    provider=Selection(path.provider.id, pinned=bool(provider_pin)),
                    harness=Selection(harness_id, pinned=bool(harness_pin)),
                    mode="plan-only",
                )
                try:
                    harness.check(probe)
                except AdapterError:
                    continue
                why = (
                    f"{complexity} task: minimum sufficient route"
                    if not model_pin
                    else "explicit user model pin"
                )
                return Dispatch(path.model_id, path.provider.id, harness_id, why)

        detail = "; ".join(runtime.problems)
        suffix = f" ({detail})" if detail else ""
        raise WorkerLaunchError(f"no compatible worker route is available{suffix}")

    @staticmethod
    def _model_pin(value: str, task_id: str) -> ModelPin:
        return ModelPin(value, f"task:{task_id}")


@dataclass(slots=True)
class WorktreeManager:
    root: Path
    config: Config

    def create(self, task_id: str, kind: TaskKind) -> Path:
        if not self.config.git.use_worktrees:
            if kind == "scout":
                return self.root
            raise WorkerLaunchError("ship execution requires git.use_worktrees=true")
        base = Path(self.config.git.worktree_dir)
        if not base.is_absolute():
            base = self.root / base
        base.mkdir(parents=True, exist_ok=True)
        target = base / task_id
        if target.exists():
            return target
        command = ["git", "worktree", "add", "--detach", str(target), "HEAD"]
        result = subprocess.run(
            command,
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            diagnostic = (result.stderr or result.stdout).strip().splitlines()
            detail = diagnostic[-1] if diagnostic else "git worktree add failed"
            raise WorkerLaunchError(detail)
        return target


@dataclass(slots=True)
class WorkerExecutor:
    store: TaskStore
    adapters: AdapterRegistry
    registry: ModelRegistry
    config: Config
    root: Path
    poll_seconds: float = 0.25

    def launch(
        self,
        task: Task,
        kind: TaskKind,
        complexity: TaskComplexity,
        spec: str,
    ) -> str:
        pins = self._pins(task)
        dispatch = WorkerDispatcher(self.adapters, self.registry, self.config).choose(
            task, complexity, pins
        )
        harness = self.adapters.harness(dispatch.harness_id)
        if harness is None:
            raise WorkerLaunchError(f"harness {dispatch.harness_id!r} disappeared")

        workspace = WorktreeManager(self.root, self.config).create(task.id, kind)
        mode = "plan-only" if kind == "scout" else self.config.implementation.mode
        skills = self._skill_context(task.id)
        request = WorkerRequest(
            task.id,
            task.role or "research_investigator",
            spec,
            workspace,
            repository=str(self.root),
            skills=skills,
            model=Selection(dispatch.model_id, pinned="model" in pins),
            provider=Selection(dispatch.provider_id, pinned="provider" in pins),
            harness=Selection(dispatch.harness_id, pinned="harness" in pins),
            mode=mode,
        )
        with _LAUNCH_LOCK:
            active = self._active_worker_count(task.session_id)
            if active >= self.config.workers.max_concurrency:
                raise WorkerLaunchError(
                    f"worker concurrency limit reached ({active}/"
                    f"{self.config.workers.max_concurrency})"
                )
            recorded = self.store.record_worker(
                task.id,
                request.role,
                WorkerStatus.STARTING,
                model=dispatch.model_id,
                provider=dispatch.provider_id,
                harness=dispatch.harness_id,
                worktree=str(workspace),
            )
            try:
                handle = harness.start(request)
            except AdapterError as error:
                self.store.update_worker(recorded.id, WorkerStatus.FAILED)
                raise WorkerLaunchError(str(error)) from error

        status = harness.status(handle)
        self.store.update_worker(recorded.id, status)
        if status.finished:
            self._finish(recorded.id, task.id, harness.result(handle))
        else:
            threading.Thread(
                target=self._monitor,
                args=(recorded.id, task.id, dispatch.harness_id, handle),
                daemon=True,
                name=f"agent-cli-{recorded.id}",
            ).start()
        return (
            f"{kind.title()} started in background as {recorded.id} via "
            f"{dispatch.model_id} / {dispatch.harness_id}. You can keep prompting."
        )

    def _monitor(
        self, worker_id: str, task_id: str, harness_id: str, handle: WorkerHandle
    ) -> None:
        harness = self.adapters.harness(harness_id)
        if harness is None:
            self.store.update_worker(worker_id, WorkerStatus.FAILED)
            return
        while True:
            try:
                status = harness.status(handle)
                self.store.update_worker(worker_id, status)
                if status.finished:
                    self._finish(worker_id, task_id, harness.result(handle))
                    return
            except (AdapterError, TaskStateError):
                try:
                    self.store.update_worker(worker_id, WorkerStatus.FAILED)
                except TaskStateError:
                    pass
                return
            time.sleep(self.poll_seconds)

    def _finish(self, worker_id: str, task_id: str, result: WorkerResult) -> None:
        self.store.update_worker(worker_id, result.status)
        if result.summary:
            self.store.add_artifact(
                task_id,
                "worker-summary",
                f"worker:{worker_id}",
                {"summary": result.summary},
            )
        if result.status is WorkerStatus.COMPLETED:
            self.store.set_status(task_id, TaskStatus.COMPLETED)
        elif result.status is WorkerStatus.CANCELED:
            self.store.set_status(task_id, TaskStatus.CANCELED)
        elif result.status is WorkerStatus.FAILED:
            self.store.set_status(task_id, TaskStatus.FAILED)

    def _active_worker_count(self, session_id: str) -> int:
        return sum(
            1
            for task in self.store.tasks(session_id)
            for worker in self.store.workers(task.id)
            if worker.status in ACTIVE_WORKER_STATUSES
        )

    def _skill_context(self, task_id: str) -> tuple[str, ...]:
        routing = self.store.latest_skill_routing(task_id)
        if routing is None or not routing.selected:
            return ()
        try:
            catalog = load_catalog(
                skill_sources(self.config.skills.paths, self.root)
            )
        except SkillError as error:
            raise WorkerLaunchError(f"cannot load routed skills: {error}") from error
        rendered: list[str] = []
        for routed in routing.selected:
            skill = catalog.get(routed.name)
            if skill is None:
                raise WorkerLaunchError(f"routed skill {routed.name!r} is no longer available")
            if skill.version != routed.version:
                raise WorkerLaunchError(
                    f"routed skill {routed.name!r} changed from v{routed.version} "
                    f"to v{skill.version}; reroute the task before launch"
                )
            rendered.append(f"{skill.name} v{skill.version}\n{skill.body}")
        return tuple(rendered)

    def _pins(self, task: Task) -> dict[PinKind, str]:
        pins = {kind: pin.value for kind, pin in self.store.pins(task.id).items()}
        parent = self.store.task(task.parent_id) if task.parent_id else None
        if parent is not None:
            inherited = {kind: pin.value for kind, pin in self.store.pins(parent.id).items()}
            inherited.update(pins)
            return inherited
        return pins

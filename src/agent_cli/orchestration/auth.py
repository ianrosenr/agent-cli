"""Auth-aware harness readiness repair for orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from agent_cli.adapters import AdapterRegistry, Capability, ReadinessState
from agent_cli.config import Config
from agent_cli.config.model import HarnessConfig

type LoginHarnesses = Callable[[tuple[str, ...]], bool]


@dataclass(slots=True)
class HarnessAuthCoordinator:
    adapters: AdapterRegistry
    config: Config
    login: LoginHarnesses | None = None

    def enabled(self, harness_id: str) -> bool:
        return self.config.harnesses.get(harness_id, HarnessConfig()).enabled

    def ready(self, harness_id: str) -> bool:
        adapter = self.adapters.harness(harness_id)
        if adapter is None or not self.enabled(harness_id):
            return False
        try:
            return adapter.readiness().ready
        except Exception:
            return False

    def auth_required(self, harness_id: str) -> bool:
        adapter = self.adapters.harness(harness_id)
        if adapter is None or not self.enabled(harness_id):
            return False
        try:
            return adapter.readiness().state is ReadinessState.AUTH_REQUIRED
        except Exception:
            return False

    def repair(self, harness_ids: Iterable[str]) -> bool:
        wanted = tuple(dict.fromkeys(harness_ids))
        pending = tuple(harness_id for harness_id in wanted if self.auth_required(harness_id))
        if not pending:
            return all(self.ready(harness_id) for harness_id in wanted)
        if self.login is None or not self.login(pending):
            return False
        return all(self.ready(harness_id) for harness_id in wanted)

    def repair_pinned(self, harness_ids: Iterable[str]) -> tuple[bool, str]:
        wanted = tuple(dict.fromkeys(harness_ids))
        if not wanted:
            return True, ""
        missing = [
            harness_id
            for harness_id in wanted
            if self.adapters.harness(harness_id) is None or not self.enabled(harness_id)
        ]
        if missing:
            return False, f"required harness unavailable: {', '.join(missing)}"
        if self.repair(wanted):
            return True, ""
        blocked = [harness_id for harness_id in wanted if not self.ready(harness_id)]
        return False, f"required harness not ready: {', '.join(blocked)}"

    def ensure_any_ready(self) -> bool:
        candidates = []
        for harness_id, adapter in self.adapters.harnesses().items():
            if not self.enabled(harness_id):
                continue
            try:
                if not adapter.capabilities().supports(Capability.START_WORKER):
                    continue
                readiness = adapter.readiness()
            except Exception:
                continue
            if readiness.ready:
                return True
            if readiness.state is ReadinessState.AUTH_REQUIRED:
                candidates.append(harness_id)
        if not candidates:
            return False
        if self.login is None or not self.login(tuple(candidates)):
            return False
        return any(self.ready(harness_id) for harness_id in candidates)

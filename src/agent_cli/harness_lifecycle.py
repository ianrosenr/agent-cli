"""Generic install/update workflow for configured coding harnesses."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.console import RenderableType

from agent_cli.adapters import AdapterError, AdapterRegistry, Capability, ReadinessState
from agent_cli.config import Config
from agent_cli.config.model import HarnessConfig
from agent_cli.ui import blocks, fields, info, section, success, warning

UPDATE_INTERVAL_S = 24 * 60 * 60

type ReadLine = Callable[[str], str]
type WriteLine = Callable[[RenderableType], None]
type Clock = Callable[[], float]


@dataclass(slots=True)
class HarnessLifecycleService:
    adapters: AdapterRegistry
    config: Config
    read_line: ReadLine | None = None
    write_line: WriteLine | None = None
    cache_file: Path | None = None
    clock: Clock = time.time

    def configured(self, harness_id: str) -> HarnessConfig | None:
        return self.config.harnesses.get(harness_id)

    def offer_install(self) -> RenderableType | None:
        """Offer installation for enabled, explicitly configured harnesses that are missing."""
        if self.read_line is None:
            return None
        missing = []
        for harness_id, adapter in self.adapters.harnesses().items():
            config = self.configured(harness_id)
            if config is None or not config.enabled:
                continue
            if not adapter.capabilities().supports(Capability.INSTALL):
                continue
            if adapter.readiness().state is ReadinessState.NOT_DETECTED:
                missing.append(adapter)
        if not missing:
            return None

        names = ", ".join(adapter.identity().display_name for adapter in missing)
        answer = self._yes_no(f"Install missing harnesses ({names}) now? (Y/n): ")
        if not answer:
            return info("Harness installation skipped.", "Run /startup again to install later.")

        rows: list[tuple[str, str]] = []
        failed = False
        for adapter in missing:
            name = adapter.identity().display_name
            self._write(info(f"Installing {name}..."))
            try:
                readiness = adapter.install()
            except AdapterError as problem:
                rows.append((name, problem.message))
                failed = True
                continue
            if readiness.state is ReadinessState.NOT_DETECTED:
                rows.append((name, "install completed but executable is still not detected"))
                failed = True
            else:
                rows.append((name, "installed ✓"))

        result = section("Harness installation", fields(rows, indent=1))
        ending = (
            warning("Some harnesses could not be installed.")
            if failed
            else success("Configured harnesses are installed.")
        )
        return blocks(result, ending)

    def refresh_latest(self) -> RenderableType | None:
        """Check configured latest harnesses at most once per TTL; never block startup."""
        now = self.clock()
        cache = self._read_cache()
        messages: list[RenderableType] = []
        changed = False

        for harness_id, config in self.config.harnesses.items():
            if not config.enabled or config.version != "latest":
                continue
            adapter = self.adapters.harness(harness_id)
            if adapter is None or not adapter.capabilities().supports(Capability.UPDATE):
                continue
            readiness = adapter.readiness()
            if readiness.state is ReadinessState.NOT_DETECTED:
                continue
            last = cache.get(harness_id)
            if isinstance(last, (int, float)) and now - float(last) < UPDATE_INTERVAL_S:
                continue

            before = adapter.identity().version
            try:
                after = adapter.update()
            except AdapterError as problem:
                messages.append(
                    warning(
                        f"Could not update {adapter.identity().display_name}; continuing.",
                        problem.message,
                    )
                )
            else:
                current = adapter.identity().version
                if current and current != before:
                    messages.append(
                        info(f"{adapter.identity().display_name} updated to {current}.")
                    )
                elif not after.ready and after.state is not ReadinessState.AUTH_REQUIRED:
                    messages.append(
                        warning(
                            f"{adapter.identity().display_name} updated but is not ready.",
                            after.describe(),
                        )
                    )
            cache[harness_id] = now
            changed = True

        if changed:
            self._write_cache(cache)
        return blocks(*messages) if messages else None

    def _yes_no(self, prompt: str) -> bool:
        assert self.read_line is not None
        while True:
            try:
                answer = self.read_line(prompt).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            if answer in {"", "y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            self._write(warning("Type y for yes or n for no."))

    def _write(self, shown: RenderableType) -> None:
        if self.write_line is not None:
            self.write_line(shown)

    def _read_cache(self) -> dict[str, float]:
        if self.cache_file is None:
            return {}
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            key: float(value)
            for key, value in data.items()
            if isinstance(key, str) and isinstance(value, (int, float))
        }

    def _write_cache(self, data: dict[str, float]) -> None:
        if self.cache_file is None:
            return
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            return

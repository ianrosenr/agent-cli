"""Configuration-driven native login for subscription harnesses."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rich.console import RenderableType

from agent_cli.adapters import AdapterError, AdapterRegistry, Capability, ReadinessState
from agent_cli.adapters.harness import HarnessAdapter
from agent_cli.config import Config
from agent_cli.ui import blocks, error, fields, info, muted, section, success, warning

type ReadLine = Callable[[str], str]
type WriteLine = Callable[[RenderableType], None]

ALIASES = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex-cli",
    "codex-cli": "codex-cli",
}


@dataclass(slots=True)
class HarnessLoginService:
    adapters: AdapterRegistry
    config: Config
    read_line: ReadLine
    write_line: WriteLine

    def enabled(self, harness_id: str) -> bool:
        entry = self.config.harnesses.get(harness_id)
        return entry is not None and entry.enabled

    def selected(self, target: str = "") -> tuple[HarnessAdapter, ...] | RenderableType:
        if target:
            harness_id = ALIASES.get(target.lower(), target)
            adapter = self.adapters.harness(harness_id)
            if adapter is None:
                return error(
                    f"Unknown login target: {target}.",
                    "Use /login or /login <configured-harness-id>.",
                )
            if adapter is None:
                return error(f"{harness_id} is not available in this session.")
            if not self.enabled(harness_id):
                return info(f"{adapter.identity().display_name} is disabled in /startup.")
            return (adapter,)

        return tuple(
            adapter
            for harness_id, adapter in self.adapters.harnesses().items()
            if self.enabled(harness_id) and adapter.capabilities().supports(Capability.LOGIN)
        )

    def offer(self) -> RenderableType | None:
        selected = self.selected()
        if not isinstance(selected, tuple):
            return selected
        pending = [
            adapter
            for adapter in selected
            if adapter.readiness().state is ReadinessState.AUTH_REQUIRED
        ]
        if not pending:
            return None
        names = ", ".join(adapter.identity().display_name for adapter in pending)
        self.write_line("")
        self.write_line(
            section(
                "Sign in",
                muted(f"Authentication is needed for: {names}. Credentials stay with each CLI."),
            )
        )
        while True:
            answer = self.read_line("Sign into configured harnesses now? (Y/n): ").strip().lower()
            if answer in {"n", "no"}:
                return info("Sign-in skipped.", "Run /login any time.")
            if answer in {"", "y", "yes"}:
                return self.run()
            self.write_line(warning("Type y for yes or n for no."))

    def repair(self, harness_ids: tuple[str, ...]) -> bool:
        selected = tuple(
            adapter
            for harness_id in harness_ids
            if self.enabled(harness_id)
            and (adapter := self.adapters.harness(harness_id)) is not None
        )
        pending = tuple(
            adapter
            for adapter in selected
            if adapter.readiness().state is ReadinessState.AUTH_REQUIRED
        )
        if not pending:
            return all(adapter.readiness().ready for adapter in selected) and len(selected) == len(
                harness_ids
            )

        names = ", ".join(adapter.identity().display_name for adapter in pending)
        while True:
            answer = (
                self.read_line(f"{names} authentication expired. Reauthenticate now? (Y/n): ")
                .strip()
                .lower()
            )
            if answer in {"n", "no"}:
                return False
            if answer in {"", "y", "yes"}:
                break
            self.write_line(warning("Type y for yes or n for no."))

        for adapter in pending:
            self.write_line(info(f"Opening {adapter.identity().display_name} login..."))
            try:
                after = adapter.login()
            except AdapterError as problem:
                self.write_line(error(problem.message))
                return False
            if not after.ready:
                self.write_line(error(after.describe()))
                return False
        return all(adapter.readiness().ready for adapter in selected)

    def run(self, target: str = "") -> RenderableType:
        selected = self.selected(target)
        if not isinstance(selected, tuple):
            return selected
        if not selected:
            return info("No enabled subscription harnesses are configured.")

        rows: list[tuple[str, str]] = []
        failures = False
        for adapter in selected:
            identity = adapter.identity()
            readiness = adapter.readiness()
            if readiness.state is ReadinessState.READY:
                rows.append((identity.display_name, "already signed in ✓"))
                continue
            if readiness.state is ReadinessState.NOT_DETECTED:
                rows.append((identity.display_name, "not installed"))
                failures = True
                continue
            if readiness.state is not ReadinessState.AUTH_REQUIRED:
                rows.append((identity.display_name, readiness.describe()))
                failures = True
                continue

            self.write_line("")
            self.write_line(info(f"Opening {identity.display_name} login..."))
            try:
                after = adapter.login()
            except AdapterError as problem:
                rows.append((identity.display_name, problem.message))
                failures = True
                continue
            if after.ready:
                rows.append((identity.display_name, "signed in ✓"))
            else:
                rows.append((identity.display_name, after.describe()))
                failures = True

        result = section("Login", fields(rows, indent=1))
        return blocks(
            result,
            warning("Some configured harnesses still need attention.")
            if failures
            else success("All configured harnesses are ready."),
        )

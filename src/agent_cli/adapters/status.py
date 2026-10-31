"""Human-readable harness and provider status, combining configuration with runtime readiness.

Configuration says what the user set up; adapters say what is actually there right now. The
two are shown side by side and never merged: nothing detected here is written to config.
A config entry and an adapter describe the same harness or provider when the config key
equals the adapter's ``identity().id``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from rich.console import RenderableType
from rich.text import Text

from agent_cli.adapters.domain import HarnessIdentity, ReadinessState
from agent_cli.adapters.harness import HarnessAdapter
from agent_cli.adapters.provider import ProviderAdapter
from agent_cli.adapters.registry import AdapterRegistry
from agent_cli.config import Config
from agent_cli.ui import blocks, section, table

NO_ADAPTER = "configured; no adapter available yet"
DISABLED = "disabled in config"
NONE_FOUND = "none configured or detected"

type Adapter = HarnessAdapter | ProviderAdapter


@dataclass(frozen=True, slots=True)
class AdapterRow:
    """One configured or detected harness or provider, as shown to the user."""

    name: str
    label: str
    """``name`` with the adapter's display name and version, when it reports them."""
    state: str
    ready: bool


def describe(name: str, adapter: Adapter) -> AdapterRow:
    """The adapter's identity and readiness. A misbehaving adapter is reported, not allowed to
    break the screen."""
    try:
        identity = adapter.identity()
        version = identity.version if isinstance(identity, HarnessIdentity) else None
        title = " ".join(part for part in (identity.display_name, version) if part)
        label = f"{name} ({title})" if title and title != name else name
    except Exception as error:
        return AdapterRow(name, name, f"Unavailable (adapter error: {error})", False)
    try:
        readiness = adapter.readiness()
        return AdapterRow(
            name, label, readiness.describe(), readiness.state is ReadinessState.READY
        )
    except Exception as error:
        return AdapterRow(name, label, f"Unavailable (adapter error: {error})", False)


def rows(enabled: Mapping[str, bool], adapters: Mapping[str, Adapter]) -> list[AdapterRow]:
    """``enabled`` maps configured IDs to whether they are enabled."""
    result = []
    for name in sorted(enabled.keys() | adapters.keys()):
        if enabled.get(name) is False:
            result.append(AdapterRow(name, name, DISABLED, False))
        elif name in adapters:
            result.append(describe(name, adapters[name]))
        else:
            result.append(AdapterRow(name, name, NO_ADAPTER, False))
    return result


def sections(registry: AdapterRegistry, config: Config) -> list[tuple[str, list[AdapterRow]]]:
    return [
        (
            "Harnesses",
            rows(
                {name: entry.enabled for name, entry in config.harnesses.items()},
                registry.harnesses(),
            ),
        ),
        (
            "Providers",
            rows(
                {name: entry.enabled for name, entry in config.providers.items()},
                registry.providers(),
            ),
        ),
    ]


def adapter_status(registry: AdapterRegistry, config: Config) -> list[str]:
    """Plain lines describing every configured or registered harness and provider."""
    lines = []
    for title, found in sections(registry, config):
        if not found:
            lines.append(f"{title}: {NONE_FOUND}")
            continue
        lines.append(f"{title}:")
        lines += [f"  {row.label}: {row.state}" for row in found]
    return lines


def providers_view(registry: AdapterRegistry, config: Config) -> RenderableType:
    """``/providers``: a table per kind, readiness named in words and colored by state."""
    parts: list[RenderableType] = []
    for title, found in sections(registry, config):
        if not found:
            parts.append(Text.assemble((title, "heading"), ("  " + NONE_FOUND, "muted")))
            continue
        cells = [(Text(row.label), Text(row.state, style=state_style(row))) for row in found]
        parts.append(section(title, table(("Name", "Status"), cells, styles=("provider",))))
    return blocks(*parts)


def state_style(row: AdapterRow) -> str:
    if row.ready:
        return "success"
    return "muted" if row.state == DISABLED else "warning"

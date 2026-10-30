"""Human-readable harness and provider status, combining configuration with runtime readiness.

Configuration says what the user set up; adapters say what is actually there right now. The
two are shown side by side and never merged: nothing detected here is written to config.
A config entry and an adapter describe the same harness or provider when the config key
equals the adapter's ``identity().id``.
"""

from __future__ import annotations

from collections.abc import Mapping

from agent_cli.adapters.domain import HarnessIdentity
from agent_cli.adapters.harness import HarnessAdapter
from agent_cli.adapters.provider import ProviderAdapter
from agent_cli.adapters.registry import AdapterRegistry
from agent_cli.config import Config

NO_ADAPTER = "configured; no adapter available yet"
DISABLED = "disabled in config"

type Adapter = HarnessAdapter | ProviderAdapter


def describe(name: str, adapter: Adapter) -> str:
    """``name (Display name version): readiness``. A misbehaving adapter is reported, not
    allowed to break the screen."""
    try:
        identity = adapter.identity()
        version = identity.version if isinstance(identity, HarnessIdentity) else None
        title = " ".join(part for part in (identity.display_name, version) if part)
        label = f"{name} ({title})" if title and title != name else name
    except Exception as error:
        return f"{name}: Unavailable (adapter error: {error})"
    try:
        return f"{label}: {adapter.readiness().describe()}"
    except Exception as error:
        return f"{label}: Unavailable (adapter error: {error})"


def section(title: str, enabled: Mapping[str, bool], adapters: Mapping[str, Adapter]) -> list[str]:
    """``enabled`` maps configured IDs to whether they are enabled."""
    names = sorted(enabled.keys() | adapters.keys())
    if not names:
        return [f"{title}: none configured or detected"]
    lines = [f"{title}:"]
    for name in names:
        if enabled.get(name) is False:
            lines.append(f"  {name}: {DISABLED}")
        elif name in adapters:
            lines.append(f"  {describe(name, adapters[name])}")
        else:
            lines.append(f"  {name}: {NO_ADAPTER}")
    return lines


def adapter_status(registry: AdapterRegistry, config: Config) -> list[str]:
    """Lines describing every configured or registered harness and provider."""
    return [
        *section(
            "Harnesses",
            {name: entry.enabled for name, entry in config.harnesses.items()},
            registry.harnesses(),
        ),
        *section(
            "Providers",
            {name: entry.enabled for name, entry in config.providers.items()},
            registry.providers(),
        ),
    ]

"""A concise view of the model registry, role floors, and classifier for ``/models``.

Shows scores and categories only; evidence prose stays in the registry file.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import fields
from pathlib import Path

from agent_cli.adapters import AdapterRegistry
from agent_cli.config import Config, ConfigError, ConfigService
from agent_cli.config.model import RoleFloors
from agent_cli.models.access import RuntimePaths, runtime_paths
from agent_cli.models.classifier_selection import (
    LOW_COST_RECOMMENDATION,
    ClassifierChoice,
    select_classifier,
)
from agent_cli.models.domain import ModelCapability, ModelRegistry, RegistryError
from agent_cli.models.eligibility import requirement
from agent_cli.models.registry import load_registry

INDENT = "  "
SCORE_HEADERS = {
    ModelCapability.REASONING: "Reas",
    ModelCapability.CODING: "Code",
    ModelCapability.REPOSITORY_ENGINEERING: "Repo",
    ModelCapability.DEBUGGING: "Debug",
    ModelCapability.TOOL_USE: "Tools",
    ModelCapability.ARCHITECTURE: "Arch",
    ModelCapability.SECURITY_REASONING: "Sec",
}
MODEL_HEADERS = ("Model", "Name", "Status", *SCORE_HEADERS.values(), "Latency", "Cost", "Conf")
FLOOR_HEADERS = ("Role", "Capability", "Floor")
EMPTY_REGISTRY = (
    "No scored models yet. Automatic routing has nothing to choose from;",
    "name a model explicitly to use it unscored.",
)


def models_lines(
    config: ConfigService | None,
    adapters: AdapterRegistry | None,
    registry_path: Path | None = None,
) -> list[str]:
    try:
        settings = Config() if config is None else config.resolve()
    except ConfigError as error:
        return [f"Cannot read configuration: {error}"]
    try:
        registry = load_registry(registry_path)
    except RegistryError as error:
        return [f"Cannot load model registry: {error}"]
    runtime = runtime_paths(adapters or AdapterRegistry(), settings)
    choice = select_classifier(
        settings.routing.classifier, registry, runtime, settings.access.preference
    )
    return [
        *registry_lines(registry),
        *unscored_lines(registry, runtime),
        "",
        "Role floors (advanced: routing.role_floors):",
        *floor_lines(settings.routing.role_floors),
        "",
        *classifier_lines(choice, settings, runtime),
    ]


def registry_lines(registry: ModelRegistry) -> list[str]:
    active = len(registry.active())
    header = (
        f"Model registry v{registry.version}: {len(registry)} models "
        f"({active} active, {len(registry) - active} candidate)"
    )
    if not len(registry):
        return [header, *(INDENT + line for line in EMPTY_REGISTRY)]
    rows = [
        (
            entry.id,
            entry.display_name,
            entry.status.value,
            *(str(entry.scores[capability]) for capability in SCORE_HEADERS),
            entry.latency.value,
            entry.cost.value,
            entry.confidence.value,
        )
        for entry in registry
    ]
    return [header, "", *table(MODEL_HEADERS, rows)]


def unscored_lines(registry: ModelRegistry, runtime: RuntimePaths) -> list[str]:
    """Models providers report that the registry does not know: pinnable, never automatic."""
    unscored = [path for path in runtime.paths if registry.get(path.model_id) is None]
    if not unscored:
        return []
    return [
        "",
        "Reachable but unscored (used only when you name them):",
        *(f"{INDENT}{path.model_id} via {path.describe()}" for path in unscored),
    ]


def floor_lines(floors: RoleFloors) -> list[str]:
    """Every role's effective floor, marking those that differ from the built-in default."""
    defaults = RoleFloors()
    rows = []
    for f in fields(floors):
        need = requirement(f.name, floors)
        floor = f"{need.floor} (>= {need.minimum})"
        default = getattr(defaults, f.name)
        if need.floor != default:
            floor += f" · default {default}"
        rows.append((need.role, need.primary.value, floor))
    return table(FLOOR_HEADERS, rows)


def classifier_lines(
    choice: ClassifierChoice, settings: Config, runtime: RuntimePaths
) -> list[str]:
    mode = f"Mode: {choice.mode}"
    selected = settings.routing.classifier.selected_model
    if selected and choice.mode == "selected":
        mode += f" · selected model: {selected}"
    if choice.path is not None:
        return [
            f"Skill classifier: {choice.model_id} via {choice.path.describe()}",
            f"{INDENT}{mode}",
        ]
    lines = ["Skill classifier: deterministic only", f"{INDENT}{mode}"]
    if choice.recommend_low_cost:
        lines += ["", *LOW_COST_RECOMMENDATION]
    else:
        lines.append(f"{INDENT}{choice.reason}")
    if runtime.problems:
        lines += ["", "Providers without usable models:"]
        lines += [f"{INDENT}{problem}" for problem in runtime.problems]
    return lines


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in [headers, *rows]) for i in range(len(headers))]

    def line(row: Sequence[str]) -> str:
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=True)]
        return (INDENT + "  ".join(cells)).rstrip()

    return [line(headers), *(line(row) for row in rows)]

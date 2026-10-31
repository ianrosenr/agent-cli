"""A concise view of the model registry, role floors, and classifier for ``/models``.

Shows scores and categories only; evidence prose stays in the registry file.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from rich.console import Group, RenderableType
from rich.text import Text

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
from agent_cli.ui import blocks, error, indented, muted, section, table, warning

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
STATUS_STYLES = {"active": "success"}
FLOOR_HEADERS = ("Role", "Capability", "Floor")
EMPTY_REGISTRY = (
    "No scored models yet. Automatic routing has nothing to choose from;",
    "name a model explicitly to use it unscored.",
)


def models_view(
    config: ConfigService | None,
    adapters: AdapterRegistry | None,
    registry_path: Path | None = None,
) -> RenderableType:
    try:
        settings = Config() if config is None else config.resolve()
    except ConfigError as problem:
        return error("Cannot read configuration.", str(problem))
    try:
        registry = load_registry(registry_path)
    except RegistryError as problem:
        return error("Cannot load model registry.", str(problem))
    runtime = runtime_paths(adapters or AdapterRegistry(), settings)
    choice = select_classifier(
        settings.routing.classifier, registry, runtime, settings.access.preference
    )
    return blocks(
        registry_view(registry),
        unscored_view(registry, runtime),
        section(
            "Role floors",
            floor_table(settings.routing.role_floors),
            note="advanced: routing.role_floors",
        ),
        classifier_view(choice, settings, runtime),
    )


def registry_view(registry: ModelRegistry) -> RenderableType:
    active = len(registry.active())
    header = Text.assemble(
        ("Model registry", "heading"),
        f" v{registry.version}: {len(registry)} models ",
        (f"({active} active, {len(registry) - active} candidate)", "muted"),
    )
    if not len(registry):
        return Group(header, indented(muted("\n".join(EMPTY_REGISTRY))))
    rows = [
        (
            entry.id,
            entry.display_name,
            Text(
                entry.status.value, style="success" if entry.status.value == "active" else "muted"
            ),
            *(str(entry.scores[capability]) for capability in SCORE_HEADERS),
            entry.latency.value,
            entry.cost.value,
            entry.confidence.value,
        )
        for entry in registry
    ]
    scores = range(3, 3 + len(SCORE_HEADERS))
    return Group(header, Text(""), table(MODEL_HEADERS, rows, styles=("model",), right=scores))


def unscored_view(registry: ModelRegistry, runtime: RuntimePaths) -> RenderableType | None:
    """Models providers report that the registry does not know: pinnable, never automatic."""
    unscored = [path for path in runtime.paths if registry.get(path.model_id) is None]
    if not unscored:
        return None
    models = Group(
        *(Text.assemble((path.model_id, "model"), f" via {path.describe()}") for path in unscored)
    )
    return section("Reachable but unscored", indented(models), note="used only when you name them")


def floor_table(floors: RoleFloors) -> RenderableType:
    """Every role's effective floor, marking those that differ from the built-in default."""
    defaults = RoleFloors()
    rows = []
    for f in fields(floors):
        need = requirement(f.name, floors)
        floor = Text(f"{need.floor} (>= {need.minimum})")
        default = getattr(defaults, f.name)
        if need.floor != default:
            floor.append(f" · default {default}", style="warning")
        rows.append((need.role, need.primary.value, floor))
    return table(FLOOR_HEADERS, rows, styles=("role",))


def classifier_view(
    choice: ClassifierChoice, settings: Config, runtime: RuntimePaths
) -> RenderableType:
    mode = f"Mode: {choice.mode}"
    selected = settings.routing.classifier.selected_model
    if selected and choice.mode == "selected":
        mode += f" · selected model: {selected}"
    if choice.path is not None:
        return Group(
            Text.assemble(
                ("Skill classifier", "heading"),
                "  ",
                (choice.model_id or "", "model"),
                f" via {choice.path.describe()}",
            ),
            indented(muted(mode)),
        )
    summary: list[RenderableType] = [
        Text.assemble(("Skill classifier", "heading"), "  deterministic only"),
        indented(muted(mode)),
    ]
    if not choice.recommend_low_cost:
        summary.append(indented(Text(choice.reason)))
    parts: list[RenderableType | None] = [Group(*summary)]
    if choice.recommend_low_cost:
        first, *rest = LOW_COST_RECOMMENDATION
        parts.append(warning(first, *rest))
    if runtime.problems:
        parts.append(
            section(
                "Providers without usable models",
                indented(Group(*(Text(problem) for problem in runtime.problems))),
            )
        )
    return blocks(*parts)

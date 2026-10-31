"""Loading the versioned model registry (``builtins/models/registry.yaml``).

The registry is a reviewed, diffable asset: every field is checked, unknown fields are errors,
and nothing is inferred from a model's name, vendor, size, latency, or price.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from agent_cli.assets import builtin_root
from agent_cli.models.domain import (
    MAX_SCORE,
    MIN_SCORE,
    Confidence,
    Cost,
    Latency,
    ModelCapability,
    ModelEntry,
    ModelRegistry,
    ModelStatus,
    RegistryError,
    Scores,
)

MODELS_DIR = "models"
REGISTRY_FILE = "registry.yaml"
TOP_LEVEL_KEYS = ("version", "models")
REQUIRED_KEYS = (
    "id",
    "display_name",
    "status",
    "scores",
    "latency",
    "cost",
    "confidence",
    "sources",
)
OPTIONAL_KEYS = ("aliases",)
SCORE_KEYS = tuple(capability.value for capability in ModelCapability)


def default_registry_path() -> Path:
    return builtin_root() / MODELS_DIR / REGISTRY_FILE


def load_registry(path: Path | None = None) -> ModelRegistry:
    path = default_registry_path() if path is None else path
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise RegistryError(f"{path}: cannot read model registry: {error}") from error
    try:
        return parse_registry(data)
    except RegistryError as error:
        raise RegistryError(f"{path}: {error}") from error


def parse_registry(data: object) -> ModelRegistry:
    top = mapping(data, "registry")
    unknown = [str(key) for key in top if key not in TOP_LEVEL_KEYS]
    if unknown:
        raise RegistryError(f"unknown key(s) {', '.join(unknown)}")
    missing = [key for key in TOP_LEVEL_KEYS if key not in top]
    if missing:
        raise RegistryError(f"missing key(s) {', '.join(missing)}")
    version = top["version"]
    if isinstance(version, bool) or not isinstance(version, (int, str)) or str(version) == "":
        raise RegistryError("version must be an integer or string")
    models = top["models"]
    if models is None:
        models = []
    if not isinstance(models, list):
        raise RegistryError("models must be a list")
    entries = [parse_entry(item, index) for index, item in enumerate(models)]
    return ModelRegistry(str(version), tuple(entries))


def parse_entry(data: object, index: int) -> ModelEntry:
    where = f"models[{index}]"
    fields = mapping(data, where)
    if isinstance(fields.get("id"), str) and fields["id"]:
        where = f"model {fields['id']!r}"
    unknown = [str(key) for key in fields if key not in (*REQUIRED_KEYS, *OPTIONAL_KEYS)]
    if unknown:
        raise RegistryError(f"{where}: unknown key(s) {', '.join(unknown)}")
    missing = [key for key in REQUIRED_KEYS if key not in fields]
    if missing:
        raise RegistryError(f"{where}: missing key(s) {', '.join(missing)}")

    scores = mapping(fields["scores"], f"{where}.scores")
    if set(scores) != set(SCORE_KEYS):
        raise RegistryError(f"{where}.scores must have exactly {', '.join(SCORE_KEYS)}")
    for key in SCORE_KEYS:
        value = scores[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise RegistryError(f"{where}.scores.{key} must be an integer, got {value!r}")
        if not MIN_SCORE <= value <= MAX_SCORE:
            raise RegistryError(
                f"{where}.scores.{key} must be from {MIN_SCORE} through {MAX_SCORE}, got {value}"
            )

    sources = names(fields["sources"], f"{where}.sources")
    if not sources:
        raise RegistryError(f"{where}.sources must name the evidence for the scores")
    return ModelEntry(
        id=identifier(fields["id"], f"{where}.id"),
        display_name=text(fields["display_name"], f"{where}.display_name"),
        status=choice(ModelStatus, fields["status"], f"{where}.status"),
        scores=Scores(**{key: scores[key] for key in SCORE_KEYS}),
        latency=choice(Latency, fields["latency"], f"{where}.latency"),
        cost=choice(Cost, fields["cost"], f"{where}.cost"),
        confidence=choice(Confidence, fields["confidence"], f"{where}.confidence"),
        sources=sources,
        aliases=identifiers(fields.get("aliases", []), f"{where}.aliases"),
    )


def choice[E: StrEnum](kind: type[E], value: object, key: str) -> E:
    allowed = [member.value for member in kind]
    if not isinstance(value, str) or value not in allowed:
        raise RegistryError(f"{key} must be one of {', '.join(allowed)}, got {value!r}")
    return kind(value)


def mapping(value: object, key: str) -> Mapping[Any, Any]:
    if not isinstance(value, Mapping):
        raise RegistryError(f"{key} must be a mapping")
    return value


def text(value: object, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{key} must be a non-empty string")
    return value


def identifier(value: object, key: str) -> str:
    result = text(value, key)
    if result != result.strip():
        raise RegistryError(f"{key} must not have surrounding whitespace")
    return result


def identifiers(value: object, key: str) -> tuple[str, ...]:
    result = names(value, key)
    for index, item in enumerate(result):
        if item != item.strip():
            raise RegistryError(f"{key}[{index}] must not have surrounding whitespace")
    return result


def names(value: object, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise RegistryError(f"{key} must be a list of non-empty strings")
    return tuple(value)

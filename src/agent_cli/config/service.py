"""Loading, layering, validating, and persisting configuration.

Layers, least to most specific: built-in defaults → global → project → session → task. Global
and project layers are YAML files; session and task layers are in-memory override mappings.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from agent_cli.config.model import Config, CostConfig
from agent_cli.config.schema import (
    ConfigError,
    RawConfig,
    build,
    merge,
    ordered,
    to_raw,
    validate_layer,
)

CONFIG_DIR = ".agent-cli"
CONFIG_FILE = "config.yaml"
STATE_DIR = "state"
FILE_HEADER = (
    "# Agent CLI configuration. See docs/CONFIGURATION.md.\n"
    "# Never put secret values here: reference them with api_key_env or api_key_keyring.\n"
)
COST_KEYS = tuple(f.name for f in fields(CostConfig))


class Layer(Enum):
    DEFAULTS = "defaults"
    GLOBAL = "global"
    PROJECT = "project"
    SESSION = "session"
    TASK = "task"


@dataclass(frozen=True, slots=True)
class ConfigPaths:
    global_file: Path
    project_root: Path | None
    """Root of the Git repository containing the working directory, if any."""

    @property
    def project_file(self) -> Path | None:
        return None if self.project_root is None else self.project_root / CONFIG_DIR / CONFIG_FILE

    @classmethod
    def discover(cls, home: Path | None = None, cwd: Path | None = None) -> ConfigPaths:
        home = Path.home() if home is None else home
        root = find_repo_root(Path.cwd() if cwd is None else cwd)
        return cls(home / CONFIG_DIR / CONFIG_FILE, root)

    def file_for(self, layer: Layer) -> Path | None:
        if layer is Layer.GLOBAL:
            return self.global_file
        if layer is Layer.PROJECT:
            return self.project_file
        return None


def find_repo_root(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def read_layer(path: Path, source: str) -> RawConfig:
    """Load one YAML layer. A missing file is an empty layer; anything invalid is an error."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ConfigError(source, None, f"cannot read file: {error.strerror or error}") from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError(source, None, f"invalid YAML: {error}") from error
    if data is None:
        return {}
    validate_layer(Config, data, source)
    return dict(data)


def write_yaml(path: Path, data: Mapping[str, Any], source: str) -> None:
    """Atomically write a complete YAML document, creating parent directories."""
    document = FILE_HEADER + yaml.safe_dump(
        dict(data), sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(document)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError as error:
        raise ConfigError(source, None, f"cannot write file: {error.strerror or error}") from error


def check_cost_ceilings(ceiling: CostConfig, layer: Mapping[str, Any], source: str) -> None:
    """More-specific layers may lower cost limits, but never raise them above global limits."""
    cost = layer.get("cost", {})
    for key in COST_KEYS:
        if key not in cost:
            continue
        limit = getattr(ceiling, key)
        value = cost[key]
        if limit is None:
            continue
        if value is None:
            raise ConfigError(
                source,
                f"cost.{key}",
                f"cannot remove the global limit of ${limit:.2f}; "
                "only global config may raise or remove cost limits",
            )
        if value > limit:
            raise ConfigError(
                source,
                f"cost.{key}",
                f"${value:.2f} is above the global limit of ${limit:.2f}; "
                "only global config may raise cost limits",
            )


@dataclass(slots=True)
class ConfigService:
    """Resolves the effective configuration and persists global and project layers."""

    paths: ConfigPaths
    session: RawConfig = field(default_factory=dict)

    def source(self, layer: Layer) -> str:
        path = self.paths.file_for(layer)
        return f"{layer.value} config ({path})" if path else f"{layer.value} overrides"

    def read(self, layer: Layer) -> RawConfig:
        """The raw contents of a file-backed layer (empty if it does not exist)."""
        path = self.paths.file_for(layer)
        return {} if path is None else read_layer(path, self.source(layer))

    def set_session(self, overrides: Mapping[str, Any]) -> None:
        validate_layer(Config, overrides, self.source(Layer.SESSION))
        self.session = dict(overrides)

    def resolve(
        self,
        task: Mapping[str, Any] | None = None,
        *,
        through: Layer = Layer.TASK,
        replace: Mapping[Layer, Mapping[str, Any]] | None = None,
    ) -> Config:
        """Merge layers up to and including ``through`` into a validated ``Config``.

        ``replace`` substitutes the raw data of individual layers, which lets callers check a
        proposed change against every other layer before writing it.
        """
        replace = replace or {}
        order = list(Layer)
        candidates: dict[Layer, Mapping[str, Any] | None] = {
            Layer.GLOBAL: None,
            Layer.PROJECT: None,
            Layer.SESSION: self.session,
            Layer.TASK: task or {},
        }
        merged: RawConfig = to_raw(Config())
        ceiling: CostConfig | None = None
        for layer in order[1 : order.index(through) + 1]:
            data = replace.get(layer)
            if data is None:
                data = candidates[layer]
                if data is None:
                    data = self.read(layer)
            source = self.source(layer)
            validate_layer(Config, data, source)
            if ceiling is not None:
                check_cost_ceilings(ceiling, data, source)
            merged = merge(merged, data)
            if layer is Layer.GLOBAL:
                ceiling = build(Config, merged).cost
        return build(Config, merged)

    def save(self, layer: Layer, changes: Mapping[str, Any]) -> Path:
        """Merge ``changes`` into a file-backed layer and write it, after validating everything."""
        path = self.paths.file_for(layer)
        if path is None:
            raise ConfigError(self.source(layer), None, "this layer is not stored in a file")
        document = merge(self.read(layer), changes)
        self.resolve(replace={layer: document})
        document = ordered(Config, document)
        write_yaml(path, document, self.source(layer))
        if self.paths.project_root is not None:
            ensure_runtime_ignored(self.paths.project_root, self.resolve(through=Layer.PROJECT))
        return path


def ensure_runtime_ignored(root: Path, config: Config) -> list[Path]:
    """Git-ignore project-local runtime directories (state, worktrees). Returns files changed."""
    wanted: dict[Path, list[str]] = {root / CONFIG_DIR / ".gitignore": [f"{STATE_DIR}/"]}
    worktrees = Path(config.git.worktree_dir)
    if worktrees.parts[:1] == (CONFIG_DIR,) and len(worktrees.parts) > 1:
        wanted[root / CONFIG_DIR / ".gitignore"].append(Path(*worktrees.parts[1:]).as_posix() + "/")
    else:
        wanted[root / ".gitignore"] = [f"/{worktrees.as_posix()}/"]

    changed = []
    for path, patterns in wanted.items():
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            missing = [p for p in patterns if p not in existing.splitlines()]
            if not missing:
                continue
            prefix = "" if not existing or existing.endswith("\n") else "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(existing + prefix + "\n".join(missing) + "\n", encoding="utf-8")
        except OSError as error:
            problem = f"cannot update: {error.strerror or error}"
            raise ConfigError(str(path), None, problem) from error
        changed.append(path)
    return changed

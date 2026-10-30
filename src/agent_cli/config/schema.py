"""Validation, merging, and conversion between raw YAML data and the typed models.

A configuration *layer* is raw, possibly partial, data: nested dicts as loaded from YAML. Each
layer is validated on its own (so errors name the file and key), the layers are deep-merged,
and the merged data is built into a ``Config``.
"""

from __future__ import annotations

import dataclasses
import re
import types
from collections.abc import Mapping
from functools import cache
from pathlib import PurePosixPath
from typing import Any, Literal, TypeAliasType, Union, get_args, get_origin, get_type_hints

type RawConfig = dict[str, Any]

SECRET_KEY = re.compile(r"(^|_)(api_key|key|token|secret|password|passwd|credential)s?$")
"""Key names that look like they hold a secret value rather than a reference to one."""

BRANCH_FORBIDDEN = re.compile(r"[\s~^:?*\[\\\x00-\x1f\x7f]|\.\.|//|@\{")

RESERVED_WORKTREE_DIRS = (PurePosixPath(".git"), PurePosixPath(".agent-cli"))
"""Worktrees must not be these directories; ``.git`` and ``.agent-cli/state`` must not contain
them either."""


def is_branch_prefix(value: str) -> bool:
    """Whether every branch named ``<value><name>`` can be a valid Git branch name.

    Follows ``git check-ref-format``: no spaces, control characters, ``~^:?*[\\``, ``..``,
    ``//`` or ``@{``; no leading ``/`` or ``-``; no path part starting with ``.``; and no
    complete path part ending in ``.lock``.
    """
    if not value or value.startswith(("/", "-")) or BRANCH_FORBIDDEN.search(value):
        return False
    *complete, last = value.split("/")
    return all(
        part and not part.startswith(".") and not part.endswith(".lock") for part in complete
    ) and not last.startswith(".")


def worktree_dir_problem(value: str) -> str | None:
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        return "must be a folder inside the repository"
    if pure in RESERVED_WORKTREE_DIRS:
        return f"must not be {value!r} itself"
    for container in (".git", ".agent-cli/state"):
        if pure.is_relative_to(container):
            return f"must not be inside {container}"
    return None


class ConfigError(Exception):
    """Invalid configuration, naming where it came from and the offending key."""

    def __init__(self, source: str, key: str | None, problem: str) -> None:
        self.source = source
        self.key = key
        self.problem = problem
        location = f"{source}: {key}" if key else source
        super().__init__(f"{location}: {problem}")


@cache
def hints(cls: type) -> dict[str, Any]:
    return get_type_hints(cls)


def unalias(hint: Any) -> Any:
    while isinstance(hint, TypeAliasType):
        hint = hint.__value__
    return hint


def is_optional(hint: Any) -> tuple[bool, Any]:
    """Split ``X | None`` into ``(True, X)``; anything else into ``(False, hint)``."""
    hint = unalias(hint)
    if get_origin(hint) in (Union, types.UnionType):
        members = [arg for arg in get_args(hint) if arg is not type(None)]
        if len(members) == 1 and len(get_args(hint)) == 2:
            return True, members[0]
    return False, hint


def describe(hint: Any) -> str:
    optional, hint = is_optional(hint)
    hint = unalias(hint)
    origin = get_origin(hint)
    if origin is Literal:
        text = "one of " + ", ".join(str(arg) for arg in get_args(hint))
    elif origin is tuple:
        text = f"a list of {describe(get_args(hint)[0])}"
    elif origin is dict:
        text = f"a mapping of names to {describe(get_args(hint)[1])}"
    elif dataclasses.is_dataclass(hint):
        text = "a mapping"
    else:
        text = {bool: "true or false", int: "an integer", float: "a number", str: "a string"}[hint]
    return f"{text} or null" if optional else text


def validate_layer(cls: type, data: object, source: str, prefix: str = "") -> None:
    """Check partial layer data against the schema of dataclass ``cls``."""
    if not isinstance(data, Mapping):
        raise ConfigError(source, prefix or None, f"expected a mapping, got {type_name(data)}")
    fields = {f.name: f for f in dataclasses.fields(cls)}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in fields:
            if isinstance(key, str) and SECRET_KEY.search(key):
                raise ConfigError(
                    source,
                    path,
                    "secret values are not allowed in config; reference the secret with an "
                    "environment variable (api_key_env) or keyring entry (api_key_keyring)",
                )
            raise ConfigError(source, path, f"unknown key (expected one of {', '.join(fields)})")
        check_value(hints(cls)[key], value, source, path, fields[key].metadata)


def check_required(
    cls: type, merged: Mapping[str, Any], touched: Mapping[str, Any], source: str, prefix: str = ""
) -> None:
    """Check that named entries (such as providers) touched by a layer are complete once merged.

    Layers may partially override an entry defined by a more general layer, so required fields
    can only be judged after merging. ``source`` is the layer that ``touched`` came from.
    """
    for f in dataclasses.fields(cls):
        if f.name not in touched or not isinstance(touched[f.name], Mapping):
            continue
        path = f"{prefix}.{f.name}" if prefix else f.name
        _, base = is_optional(hints(cls)[f.name])
        base = unalias(base)
        if dataclasses.is_dataclass(base):
            check_required(base, merged[f.name], touched[f.name], source, path)  # type: ignore[arg-type]
        elif get_origin(base) is dict:
            item_hint = unalias(get_args(base)[1])
            if not dataclasses.is_dataclass(item_hint):
                continue
            for name in touched[f.name]:
                entry = merged[f.name][name]
                for item_field in dataclasses.fields(item_hint):
                    if (
                        item_field.metadata.get("kind") == "required"
                        and item_field.name not in entry
                    ):
                        raise ConfigError(source, f"{path}.{name}.{item_field.name}", "is required")


def check_value(hint: Any, value: object, source: str, path: str, meta: Mapping[str, Any]) -> None:
    optional, base = is_optional(hint)
    base = unalias(base)
    if value is None:
        if not optional:
            raise ConfigError(source, path, f"must be {describe(hint)}, got null")
        return
    origin = get_origin(base)
    wrong = ConfigError(source, path, f"must be {describe(hint)}, got {type_name(value)}")

    if dataclasses.is_dataclass(base):
        validate_layer(base, value, source, path)  # type: ignore[arg-type]
    elif origin is Literal:
        if value not in get_args(base):
            raise ConfigError(source, path, f"must be {describe(hint)}, got {value!r}")
    elif origin is tuple:
        if not isinstance(value, list):
            raise wrong
        for index, item in enumerate(value):
            check_value(get_args(base)[0], item, source, f"{path}[{index}]", {})
    elif origin is dict:
        if not isinstance(value, Mapping):
            raise wrong
        item_hint = unalias(get_args(base)[1])
        for name, item in value.items():
            if not isinstance(name, str) or not name:
                raise ConfigError(source, path, f"names must be non-empty strings, got {name!r}")
            if dataclasses.is_dataclass(item_hint):
                if not isinstance(item, Mapping):
                    raise ConfigError(
                        source, f"{path}.{name}", f"expected a mapping, got {type_name(item)}"
                    )
                validate_layer(item_hint, item, source, f"{path}.{name}")  # type: ignore[arg-type]
            else:
                check_value(item_hint, item, source, f"{path}.{name}", {})
    elif base is bool:
        if not isinstance(value, bool):
            raise wrong
    elif base is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise wrong
    elif base is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise wrong
    elif base is str:
        if not isinstance(value, str):
            raise wrong
    else:  # pragma: no cover - guards against unsupported schema types
        raise TypeError(f"Unsupported config type at {path}: {hint!r}")

    check_constraints(value, source, path, meta)


def check_constraints(value: Any, source: str, path: str, meta: Mapping[str, Any]) -> None:
    minimum = meta.get("minimum")
    if minimum is not None and value < minimum:
        raise ConfigError(source, path, f"must be at least {minimum}, got {value}")
    kind = meta.get("kind")
    if kind == "required" and not value:
        raise ConfigError(source, path, "must not be empty")
    elif kind == "unique-nonempty":
        if not value:
            raise ConfigError(source, path, "must not be empty")
        if len(set(value)) != len(value):
            raise ConfigError(source, path, "must not repeat entries")
    elif kind == "paths":
        for index, item in enumerate(value):
            check_path(item, source, f"{path}[{index}]")
    elif kind == "worktree-dir":
        check_path(value, source, path)
        problem = worktree_dir_problem(value)
        if problem:
            raise ConfigError(source, path, f"{problem}, got {value!r}")
    elif kind == "branch-prefix" and not is_branch_prefix(value):
        raise ConfigError(source, path, f"is not a valid Git branch prefix: {value!r}")


def check_path(value: str, source: str, path: str) -> None:
    if not value.strip() or "\x00" in value:
        raise ConfigError(source, path, f"is not a valid path: {value!r}")


def type_name(value: object) -> str:
    if value is None:
        return "null"
    return {list: "a list", dict: "a mapping", str: "a string", bool: "a boolean"}.get(
        type(value), type(value).__name__
    )


def merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> RawConfig:
    """Deep-merge ``override`` onto ``base``. Mappings merge by key; other values replace."""
    merged: RawConfig = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = merge(current, value)
        else:
            merged[key] = value
    return merged


def build[T](cls: type[T], data: Mapping[str, Any]) -> T:
    """Construct ``cls`` from validated data; absent keys keep their defaults."""
    kwargs = {
        name: convert(hints(cls)[name], data[name])  # type: ignore[arg-type]
        for name in (f.name for f in dataclasses.fields(cls))  # type: ignore[arg-type]
        if name in data
    }
    return cls(**kwargs)


def convert(hint: Any, value: Any) -> Any:
    _, base = is_optional(hint)
    base = unalias(base)
    if value is None:
        return None
    if dataclasses.is_dataclass(base):
        return build(base, value)  # type: ignore[arg-type]
    origin = get_origin(base)
    if origin is tuple:
        return tuple(convert(get_args(base)[0], item) for item in value)
    if origin is dict:
        return {name: convert(get_args(base)[1], item) for name, item in value.items()}
    if base is float:
        return float(value)
    return value


def to_raw(obj: Any) -> Any:
    """Convert a model (or part of one) back into YAML-ready data."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_raw(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (tuple, list)):
        return [to_raw(item) for item in obj]
    if isinstance(obj, dict):
        return {name: to_raw(item) for name, item in obj.items()}
    return obj


def ordered(cls: type, data: Mapping[str, Any]) -> RawConfig:
    """Return layer data with keys in schema order, for stable, readable YAML.

    Keys the schema does not know are kept (after known keys) so this never hides invalid data.
    """
    result: RawConfig = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        _, base = is_optional(hints(cls)[f.name])
        base = unalias(base)
        if dataclasses.is_dataclass(base) and isinstance(value, Mapping):
            value = ordered(base, value)  # type: ignore[arg-type]
        elif get_origin(base) is dict and isinstance(value, Mapping):
            item_hint = unalias(get_args(base)[1])
            if dataclasses.is_dataclass(item_hint):
                value = {
                    name: ordered(item_hint, item)  # type: ignore[arg-type]
                    if isinstance(item, Mapping)
                    else item
                    for name, item in value.items()
                }
        result[f.name] = value
    result.update((key, value) for key, value in data.items() if key not in result)
    return result

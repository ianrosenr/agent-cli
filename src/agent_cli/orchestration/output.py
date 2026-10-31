"""Strict parsing of model replies into typed values.

Model output is untrusted: unknown keys, wrong types, and malformed JSON are errors, never
guessed at, so free-form prose can never reach canonical task state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from agent_cli.models.provider_classifier import strip_fence


class ModelOutputError(ValueError):
    """A model reply did not follow its output contract. Nothing was persisted."""


def load_object(text: str, what: str) -> Mapping[str, Any]:
    try:
        data = json.loads(strip_fence(text))
    except json.JSONDecodeError as error:
        raise ModelOutputError(f"{what} reply is not JSON: {error}") from error
    return mapping(data, what)


def mapping(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ModelOutputError(f"{where} must be an object")
    return value


def items(data: Mapping[str, Any], key: str, where: str) -> list[Mapping[str, Any]]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ModelOutputError(f"{where}.{key} must be a list")
    return [mapping(item, f"{where}.{key}[{index}]") for index, item in enumerate(value)]


def only(data: Mapping[str, Any], allowed: tuple[str, ...], where: str) -> None:
    unknown = sorted(str(key) for key in data if key not in allowed)
    if unknown:
        raise ModelOutputError(f"{where} has unknown field(s) {', '.join(unknown)}")


def text(data: Mapping[str, Any], key: str, where: str, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ModelOutputError(f"{where}.{key} must be a string")
    return value.strip()


def texts(data: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ModelOutputError(f"{where}.{key} must be a list of strings")
    return tuple(dict.fromkeys(item.strip() for item in value if item.strip()))


def flag(data: Mapping[str, Any], key: str, where: str, default: bool | None = None) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ModelOutputError(f"{where}.{key} must be true or false")
    return value


def compact(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

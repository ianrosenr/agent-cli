"""Vocabulary shared by configuration and adapters, so neither package depends on the other."""

from __future__ import annotations

from typing import Literal, get_args

type ImplementationMode = Literal["autopilot", "checkpoint", "plan-only"]
type AccessKind = Literal["subscription", "local", "api"]

IMPLEMENTATION_MODES: tuple[str, ...] = get_args(ImplementationMode.__value__)
ACCESS_KINDS: tuple[str, ...] = get_args(AccessKind.__value__)

"""Token estimation for skill-context budgeting.

The concrete worker model and tokenizer are not known when skills are routed, so one conservative,
deterministic estimator drives discovery, budgeting, telemetry, and ``/skills`` alike. A
model-specific tokenizer can later replace it through ``TokenEstimator``.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Protocol


class TokenEstimator(Protocol):
    @property
    def id(self) -> str:
        """A stable, versioned identifier recorded with every routing decision."""
        ...

    def estimate(self, text: str) -> int: ...


@dataclass(frozen=True, slots=True)
class Utf8ByteEstimator:
    """``ceil(utf8_byte_length / 3)``: deliberately above typical tokenizer counts."""

    bytes_per_token: int = 3

    @property
    def id(self) -> str:
        return f"utf8-bytes-div{self.bytes_per_token}@1"

    def estimate(self, text: str) -> int:
        return ceil(len(text.encode("utf-8")) / self.bytes_per_token)


DEFAULT_ESTIMATOR: TokenEstimator = Utf8ByteEstimator()

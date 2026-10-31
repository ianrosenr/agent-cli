"""The cost-estimation seam: conservative upper-bound quotes for paid calls.

Pricing is provider- and access-path-specific and versioned independently of the model registry.
The registry's coarse ``cost`` category is a routing hint and is never used here. Agent CLI ships
no real provider prices: until a trusted price source is configured, every paid API path is
``pricing_unknown`` and fails closed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from agent_cli.adapters import ProviderRequest
from agent_cli.budget.domain import CostEstimate, PaidCall
from agent_cli.skills.tokens import Utf8ByteEstimator

TOKENS_PER_MILLION = 1_000_000
REQUEST_OVERHEAD_TOKENS = 256
"""Allowance for message framing and special tokens around the prompt and system text."""
INPUT_BOUND = Utf8ByteEstimator(bytes_per_token=1)
"""One token per UTF-8 byte: tokenizers emit at most one token per byte of input."""


def input_bound(request: ProviderRequest) -> int:
    """A conservative upper bound on a direct request's input tokens."""
    text = request.prompt + (request.system or "")
    return INPUT_BOUND.estimate(text) + REQUEST_OVERHEAD_TOKENS


class CostEstimator(Protocol):
    def estimate(self, call: PaidCall) -> CostEstimate | None:
        """A conservative upper-bound estimate for ``call``, or ``None`` when its price is
        unknown or untrusted. Called only for calls with input and output bounds."""
        ...

    def price_usage(
        self, call: PaidCall, estimate: CostEstimate, input_tokens: int, output_tokens: int
    ) -> int | None:
        """The actual cost of reported token usage in ``usd_micros``, priced by the same source
        and version that produced ``estimate``; ``None`` when that source cannot price it."""
        ...


@dataclass(frozen=True, slots=True)
class Price:
    """Per-million-token rates in ``usd_micros``, so a $3.00/Mtok rate is ``3_000_000``."""

    input_micros_per_mtok: int
    output_micros_per_mtok: int

    def cost(self, input_tokens: int, output_tokens: int) -> int:
        """Exact integer cost, rounded up to the next micro-dollar."""
        numerator = (
            input_tokens * self.input_micros_per_mtok + output_tokens * self.output_micros_per_mtok
        )
        return -(-numerator // TOKENS_PER_MILLION)


@dataclass(frozen=True, slots=True)
class PriceTable:
    """A static, versioned price list keyed by provider ID and model ID (case-insensitive).

    Keys are normalized on construction into a read-only mapping, so a table can neither miss a
    differently-cased lookup nor change after its version is recorded in the ledger.
    """

    source: str
    version: str
    prices: Mapping[tuple[str, str], Price] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized: dict[tuple[str, str], Price] = {}
        for (provider_id, model_id), price in self.prices.items():
            key = (provider_id.casefold(), model_id.casefold())
            if key in normalized:
                raise ValueError(
                    f"{self.source} {self.version}: duplicate price for {provider_id}/{model_id}"
                )
            normalized[key] = price
        object.__setattr__(self, "prices", MappingProxyType(normalized))

    @classmethod
    def of(cls, source: str, version: str, rows: Iterable[tuple[str, str, Price]]) -> PriceTable:
        prices: dict[tuple[str, str], Price] = {}
        for provider_id, model_id, price in rows:
            if (provider_id, model_id) in prices:
                raise ValueError(
                    f"{source} {version}: duplicate price for {provider_id}/{model_id}"
                )
            prices[provider_id, model_id] = price
        return cls(source, version, prices)

    def price(self, provider_id: str, model_id: str) -> Price | None:
        return self.prices.get((provider_id.casefold(), model_id.casefold()))

    def estimate(self, call: PaidCall) -> CostEstimate | None:
        price = self.price(call.provider_id, call.model_id)
        if price is None or call.input_tokens is None or call.max_output_tokens is None:
            return None
        return CostEstimate(
            usd_micros=price.cost(call.input_tokens, call.max_output_tokens),
            input_tokens=call.input_tokens,
            output_tokens=call.max_output_tokens,
            source=self.source,
            source_version=self.version,
            conservative=True,
        )

    def price_usage(
        self, call: PaidCall, estimate: CostEstimate, input_tokens: int, output_tokens: int
    ) -> int | None:
        if (estimate.source, estimate.source_version) != (self.source, self.version):
            return None
        price = self.price(call.provider_id, call.model_id)
        return None if price is None else price.cost(input_tokens, output_tokens)


NO_PRICING = PriceTable("agent-cli-builtin", "1")
"""The default: no trusted prices, so every paid API path fails closed as ``pricing_unknown``."""

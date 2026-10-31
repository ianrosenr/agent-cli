"""The cost-estimation seam: conservative upper-bound quotes for paid calls.

Pricing is provider- and access-path-specific and versioned independently of the model registry.
The registry's coarse ``cost`` category is a routing hint and is never used here. Agent CLI ships
no real provider prices: until a trusted price source is configured, every paid API path is
``pricing_unknown`` and fails closed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from agent_cli.adapters import ProviderRequest
from agent_cli.budget.domain import CostEstimate, PaidCall
from agent_cli.skills.tokens import Utf8ByteEstimator

TOKENS_PER_MILLION = 1_000_000
INPUT_BYTES = Utf8ByteEstimator(bytes_per_token=1)


def input_bound(
    request: ProviderRequest, *, max_tokens_per_utf8_byte: int, request_overhead_tokens: int
) -> int:
    """A pricing-source-supplied conservative input-token bound for a direct request."""
    if max_tokens_per_utf8_byte < 1 or request_overhead_tokens < 0:
        raise ValueError("input-bound policy must be conservative and non-negative")
    text = request.prompt + (request.system or "")
    utf8_bytes = INPUT_BYTES.estimate(text)
    return utf8_bytes * max_tokens_per_utf8_byte + request_overhead_tokens


class CostEstimator(Protocol):
    def input_bound(self, call: PaidCall, request: ProviderRequest) -> int | None:
        """Return a trusted conservative request input bound, or None when unprovable."""
        ...

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
    """Versioned rate plus trusted request-bound policy for one provider/model path."""

    input_micros_per_mtok: int
    output_micros_per_mtok: int
    max_input_tokens_per_utf8_byte: int | None = None
    request_overhead_tokens: int | None = None

    def bound_input(self, request: ProviderRequest) -> int | None:
        if self.max_input_tokens_per_utf8_byte is None or self.request_overhead_tokens is None:
            return None
        return input_bound(
            request,
            max_tokens_per_utf8_byte=self.max_input_tokens_per_utf8_byte,
            request_overhead_tokens=self.request_overhead_tokens,
        )

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

    def input_bound(self, call: PaidCall, request: ProviderRequest) -> int | None:
        price = self.price(call.provider_id, call.model_id)
        return None if price is None else price.bound_input(request)

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


class ProviderEstimators:
    """Routes each call to the estimator ``lookup`` returns for its provider ID.

    A provider without one is priced by ``NO_PRICING`` and so fails closed. ``lookup`` is asked
    on every call, so it may follow a provider set that changes during a session.
    """

    def __init__(self, lookup: Callable[[str], CostEstimator | None]) -> None:
        self.lookup = lookup

    def of(self, provider_id: str) -> CostEstimator:
        estimator = self.lookup(provider_id)
        return NO_PRICING if estimator is None else estimator

    def input_bound(self, call: PaidCall, request: ProviderRequest) -> int | None:
        return self.of(call.provider_id).input_bound(call, request)

    def estimate(self, call: PaidCall) -> CostEstimate | None:
        return self.of(call.provider_id).estimate(call)

    def price_usage(
        self, call: PaidCall, estimate: CostEstimate, input_tokens: int, output_tokens: int
    ) -> int | None:
        return self.of(call.provider_id).price_usage(call, estimate, input_tokens, output_tokens)

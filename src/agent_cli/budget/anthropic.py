"""Trusted cost estimates for the first-party Anthropic provider path.

Two facts make an Anthropic quote trustworthy:

- **Input bound**: Anthropic's Token Count API, asked about the exact model, system text, and
  prompt that will be sent. Never a byte-count guess. If counting fails, the bound is unknown
  and the paid path fails closed.
- **Price**: ``ANTHROPIC_PRICING``, a reviewed static table for the standard, global, uncached,
  non-batch Messages request shape the adapter sends, keyed by exact model ID. Any other model is
  ``pricing_unknown``.

Prices: https://platform.claude.com/docs/en/about-claude/pricing (reviewed 2026-10-31).
Token counting: https://platform.claude.com/docs/en/build-with-claude/token-counting
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from agent_cli.adapters import AdapterError, ProviderRequest
from agent_cli.budget.domain import PAID_ACCESS, CostEstimate, PaidCall
from agent_cli.budget.pricing import Price, PriceTable

PROVIDER_ID = "anthropic"

ANTHROPIC_PRICING = PriceTable.of(
    "anthropic-claude-api-standard",
    "2026-10-31",
    [
        # Claude Sonnet 5.5, base input $2 / MTok, output $10 / MTok. Flat across the 1M
        # context window: no long-context premium. Batch, cache, fast-mode, and US-only
        # (inference_geo) rates differ and are out of scope.
        ("anthropic", "claude-sonnet-5-5", Price(2_000_000, 10_000_000)),
    ],
)

COUNT_MARGIN_PERCENT = 2
COUNT_MARGIN_TOKENS = 64
"""Headroom added to Anthropic's count. The docs call the count an estimate that "might differ
by a small amount" from the tokens billed, so the reserved bound stays above it."""

CACHE_SIZE = 32

type TokenCounter = Callable[[ProviderRequest], int]
"""Anthropic's input-token count for one request; raises ``AdapterError``."""


def with_margin(counted: int) -> int:
    return counted + -(-counted * COUNT_MARGIN_PERCENT // 100) + COUNT_MARGIN_TOKENS


@dataclass(slots=True)
class AnthropicCostEstimator:
    """``CostEstimator`` for the ``anthropic`` provider: counted input, reviewed prices."""

    count: TokenCounter
    prices: PriceTable = ANTHROPIC_PRICING
    _bounds: OrderedDict[tuple[str, str, str], int] = field(default_factory=OrderedDict)
    """Exact-request bounds, in memory only: path ranking and dispatch ask for the same one."""

    def input_bound(self, call: PaidCall, request: ProviderRequest) -> int | None:
        if (
            call.provider_id != PROVIDER_ID
            or call.access != PAID_ACCESS
            or request.model_id != call.model_id
            or self.prices.price(call.provider_id, call.model_id) is None
        ):
            return None
        key = (request.model_id, request.system or "", request.prompt)
        bound = self._bounds.get(key)
        if bound is not None:
            self._bounds.move_to_end(key)
            return bound
        try:
            bound = with_margin(self.count(request))
        except AdapterError:
            return None
        self._bounds[key] = bound
        if len(self._bounds) > CACHE_SIZE:
            self._bounds.popitem(last=False)
        return bound

    def estimate(self, call: PaidCall) -> CostEstimate | None:
        if call.provider_id != PROVIDER_ID or call.access != PAID_ACCESS:
            return None
        return self.prices.estimate(call)

    def price_usage(
        self, call: PaidCall, estimate: CostEstimate, input_tokens: int, output_tokens: int
    ) -> int | None:
        return self.prices.price_usage(call, estimate, input_tokens, output_tokens)

"""Direct model calls for the orchestrator's own reasoning: planning and spec authoring.

These are control-plane calls, never coding workers: no harness, no worktree, no file edits.
Each call takes its path through the existing routing seams, in order::

    explicit model pin, or the role floor
    -> V1-07 ranked model candidates over READY provider paths
    -> V1-08 budget and access filtering
    -> BudgetGuard, immediately before the paid invocation

A pinned model is never substituted: if it cannot run, the call fails and the user decides.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol

from agent_cli.adapters import AdapterError, AdapterRegistry, ProviderRequest
from agent_cli.budget import (
    BudgetBlocked,
    BudgetError,
    BudgetGuard,
    BudgetRejected,
    CallBounds,
    PathQuote,
    quote_path,
    rank_quotes,
)
from agent_cli.config import Config
from agent_cli.models import (
    ModelPath,
    ModelPin,
    ModelRegistry,
    ModelRoutingError,
    RuntimePaths,
    pinned_paths,
    ranked_model_candidates,
    requirement,
    runtime_paths,
)
from agent_cli.skills.tokens import DEFAULT_ESTIMATOR
from agent_cli.state import PinKind
from agent_cli.terms import AccessKind, Role


class PlanningUnavailable(Exception):
    """No model path can make this call now. Nothing was substituted; the user must act."""


@dataclass(frozen=True, slots=True)
class ModelReply:
    text: str
    model_id: str
    provider_id: str
    access: AccessKind | None

    def describe(self) -> str:
        return f"{self.model_id} via {self.provider_id} ({self.access or 'unknown access'})"


class ModelCall(Protocol):
    """Reach a model for one control-plane role. Raises ``PlanningUnavailable``."""

    def __call__(
        self, role: Role, purpose: str, system: str, prompt: str, max_output_tokens: int
    ) -> ModelReply: ...


@dataclass(slots=True)
class DirectModelCall:
    """Model calls for one root task: one budget scope, the root's planning pins."""

    adapters: AdapterRegistry
    config: Config
    registry: ModelRegistry
    guard: BudgetGuard
    root_task_id: str
    pins: Mapping[PinKind, str] = field(default_factory=dict)
    """Planning pins on the root task. A harness pin does not apply: no harness runs here."""
    replies: list[ModelReply] = field(default_factory=list)
    _runtime: RuntimePaths | None = None

    @property
    def runtime(self) -> RuntimePaths:
        if self._runtime is None:
            self._runtime = runtime_paths(self.adapters, self.config)
        return self._runtime

    def __call__(
        self, role: Role, purpose: str, system: str, prompt: str, max_output_tokens: int
    ) -> ModelReply:
        quote = self.choose(role, purpose, system, prompt, max_output_tokens)
        adapter = self.adapters.provider(quote.path.provider.id)
        if adapter is None:
            raise PlanningUnavailable(f"provider {quote.path.provider.id} is not registered")
        request = ProviderRequest(quote.path.model_id, prompt, system, max_output_tokens)
        try:
            response = self.guard.invoke(adapter, request, purpose, task_id=self.root_task_id)
        except (BudgetRejected, BudgetError) as error:
            raise PlanningUnavailable(f"{purpose} via {quote.describe()}: {error}") from error
        except AdapterError as error:
            raise PlanningUnavailable(
                f"{purpose} via {quote.describe()} failed: {error}"
            ) from error
        reply = ModelReply(
            response.text, quote.path.model_id, quote.path.provider.id, quote.call.access
        )
        self.replies.append(reply)
        return reply

    def choose(
        self,
        role: Role,
        purpose: str,
        system: str,
        prompt: str,
        max_output_tokens: int,
    ) -> PathQuote:
        """The budget-compliant path for this call. Reserves nothing.

        Paid quotes use the same trusted provider/model-specific input bound that BudgetGuard
        will enforce at dispatch; an untrusted generic byte+overhead estimate never chooses the
        paid path.
        """
        preference = self.config.access.preference
        context = DEFAULT_ESTIMATOR.estimate(system + prompt) + max_output_tokens
        try:
            usage = self.guard.ledger.usage(self.root_task_id)
        except BudgetError as error:
            raise PlanningUnavailable(f"cannot read the spend ledger: {error}") from error
        provider = self.pins.get("provider")
        model = self.pins.get("model")
        estimator, limits = self.guard.estimator, self.guard.limits

        def quoted(model_id: str, path: ModelPath, capability_rank: int) -> PathQuote:
            bounds = CallBounds(
                purpose,
                None,
                max_output_tokens,
                root_task_id=self.root_task_id,
                task_id=self.root_task_id,
            )
            call = bounds.call(path)
            if call.paid:
                request = ProviderRequest(path.model_id, prompt, system, max_output_tokens)
                try:
                    input_tokens = estimator.input_bound(call, request)
                except ValueError:
                    input_tokens = None
                bounds = replace(bounds, input_tokens=input_tokens)
            return quote_path(model_id, path, capability_rank, bounds, estimator, limits, usage)

        try:
            if model is not None:
                pin = ModelPin(model, f"task:{self.root_task_id}/role:{role}")
                pinned = pinned_paths(self.registry, self.runtime, preference, pin, context)
                paths = pinned.paths
                if provider is not None:
                    paths = tuple(path for path in paths if path.provider.id == provider)
                    if not paths:
                        raise PlanningUnavailable(
                            f"pinned model {pinned.model_id!r} is not served by pinned provider "
                            f"{provider!r}; ask the user how to proceed"
                        )
                budgeted = rank_quotes(
                    (quoted(pinned.model_id, path, 0) for path in paths), preference
                )
            else:
                candidates = ranked_model_candidates(
                    self.registry,
                    role,
                    self.config.routing.role_floors,
                    self.runtime,
                    preference,
                    context,
                )
                quotes = []
                for rank, candidate in enumerate(candidates):
                    for path in candidate.paths:
                        if provider is None or path.provider.id == provider:
                            quotes.append(quoted(candidate.model_id, path, rank))
                if not quotes:
                    raise PlanningUnavailable(self.no_candidates(role, provider))
                budgeted = rank_quotes(quotes, preference)
            return budgeted.first(purpose)
        except (ModelRoutingError, BudgetBlocked) as error:
            raise PlanningUnavailable(str(error)) from error

    def no_candidates(self, role: Role, provider: str | None) -> str:
        need = requirement(role, self.config.routing.role_floors)
        where = f"pinned provider {provider}" if provider else "a ready provider"
        problems = "".join(f"; {problem}" for problem in self.runtime.problems)
        return (
            f"no {role} model is available: none meeting the {need.floor} floor "
            f"({need.primary} {need.minimum}+) is served by {where}{problems}"
        )

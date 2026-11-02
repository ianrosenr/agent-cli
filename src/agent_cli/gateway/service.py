"""Routing one line of root-prompt text to an answer or to orchestration.

A pending planning question always takes the next line as its answer, before any routing, so a
question can never be skipped. Otherwise the intent router decides the workflow. Answers never
touch task state; only orchestration creates tasks.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text

from agent_cli.adapters import AdapterRegistry, ProviderAdapter, ProviderRequest, ProviderResponse
from agent_cli.budget import NO_PRICING, CostEstimator, SpendLedger
from agent_cli.config import Config, ConfigError, ConfigService
from agent_cli.gateway import NO_REPOSITORY
from agent_cli.gateway.answers import (
    Answer,
    AnswerCalls,
    DirectAnswerService,
    RepositoryAnswerService,
)
from agent_cli.gateway.intent import (
    IntentClassifier,
    IntentDecision,
    IntentRouter,
    ProviderIntentClassifier,
)
from agent_cli.models import ModelRegistry, RegistryError, load_registry, runtime_paths
from agent_cli.models.classifier_selection import select_classifier
from agent_cli.orchestration.calls import PlanningUnavailable
from agent_cli.state import PinKind, TaskStateError, TaskStore
from agent_cli.ui import blocks, error, info, muted

type Orchestrate = Callable[[str], RenderableType]
type ClassifierFactory = Callable[[Config, ModelRegistry], IntentClassifier | None]

ANSWER_HINT = "See /models for reachable models, or /startup to configure a provider."
RESTATE = (
    "Answers keep no history, so there is no earlier change to plan from.",
    "Restate the change itself, for example: add retries to the HTTP client.",
)
NAME = r"([\w.:/-]+?)[.,;:!?]?"
PIN_PHRASE = re.compile(
    rf"^\s*(?:please\s+)?(?:use|using|via|ask)\s+(?:the\s+)?{NAME}"
    rf"(?:\s+(?:via|on|through)\s+{NAME})?(?=\s|$)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class RootPromptGateway:
    config: ConfigService
    adapters: AdapterRegistry = field(default_factory=AdapterRegistry)
    orchestrate: Orchestrate | None = None
    """Plans work; ``None`` outside a Git repository."""
    store: TaskStore | None = None
    """Read only to find a pending planning question; answers never write it."""
    registry: ModelRegistry | None = None
    """The model registry; the built-in one, loaded once, when ``None``."""
    ledger: SpendLedger | None = None
    """The paid-spend ledger; the global one, opened once, when ``None``."""
    estimator: CostEstimator = NO_PRICING
    classifier: ClassifierFactory | None = None
    """Builds the intent classifier; the configured low-cost model when ``None``."""
    _calls: AnswerCalls | None = None
    _chat: DirectAnswerService | None = None
    _repository: RepositoryAnswerService | None = None

    @property
    def root(self) -> Path | None:
        return self.config.paths.project_root

    def handle(self, text: str) -> RenderableType:
        if self.orchestrate is not None and self.pending_question():
            return self.orchestrate(text)
        try:
            config = self.config.resolve()
            calls = self.calls()
        except (ConfigError, RegistryError) as problem:
            return error("Cannot answer.", str(problem))

        in_repository = self.root is not None and self.orchestrate is not None
        router = IntentRouter(lambda: self.build_classifier(config, calls))
        decision = router.route(text, in_repository)
        if decision.intent == "orchestration":
            if self.orchestrate is None:
                return NO_REPOSITORY
            if decision.refers_back:
                return info(*RESTATE)
            return self.orchestrate(text)

        pins = explicit_pins(text, calls.registry, self.adapters)
        try:
            if decision.intent == "repository_question" and self.root is not None:
                if self._repository is None:
                    self._repository = RepositoryAnswerService(calls, self.root)
                answer = self._repository.answer(text, config, pins)
            else:
                if self._chat is None:
                    self._chat = DirectAnswerService(calls)
                answer = self._chat.answer(text, config, pins)
        except PlanningUnavailable as problem:
            return error(
                f"Cannot answer: {problem}", ANSWER_HINT, *verbose_routing(config, decision)
            )
        return render_answer(answer, decision, config)

    def calls(self) -> AnswerCalls:
        """Built once: the adapters are reset in place on reload, and the registry and ledger
        do not change during a session."""
        if self._calls is None:
            registry = self.registry if self.registry is not None else load_registry()
            ledger = self.ledger if self.ledger is not None else SpendLedger.default()
            self._calls = AnswerCalls(self.adapters, registry, ledger, self.estimator)
        return self._calls

    def pending_question(self) -> bool:
        store = self.store
        if store is None:
            return False
        try:
            if not store.exists():
                return False
            session = store.latest_session()
            return (
                session is not None
                and session.open
                and store.pending_planning_question(session.id) is not None
            )
        except TaskStateError:
            return True  # let orchestration report the broken state

    def build_classifier(self, config: Config, calls: AnswerCalls) -> IntentClassifier | None:
        if self.classifier is not None:
            return self.classifier(config, calls.registry)
        runtime = runtime_paths(self.adapters, config)
        choice = select_classifier(
            config.routing.classifier, calls.registry, runtime, config.access.preference
        )
        if choice.path is None:
            return None
        adapter = self.adapters.provider(choice.path.provider.id)
        if adapter is None:
            return None
        guard = calls.guard(config)

        def invoke(adapter: ProviderAdapter, request: ProviderRequest) -> ProviderResponse:
            return guard.invoke(adapter, request, "intent_classification")

        return ProviderIntentClassifier(adapter, choice.path, invoke)


def explicit_pins(
    text: str, registry: ModelRegistry, adapters: AdapterRegistry
) -> dict[PinKind, str]:
    """A model or provider the user named outright to open the message ("use X: …",
    "ask X …", "via X, …", "use X via Y …").

    Only exact registry model names and registered provider IDs count, and only at the start,
    so a passing mention ("how do I use X?") never becomes a pin. A pin is a hard constraint:
    if it cannot run, the answer fails.
    """
    match = PIN_PHRASE.match(text)
    if match is None:
        return {}
    pins: dict[PinKind, str] = {}
    providers = {provider_id.casefold(): provider_id for provider_id in adapters.providers()}
    for name in filter(None, match.groups()):
        if "model" not in pins and registry.get(name) is not None:
            pins["model"] = name
        elif "provider" not in pins and name.casefold() in providers:
            pins["provider"] = providers[name.casefold()]
    return pins


def verbose_routing(config: Config, decision: IntentDecision) -> tuple[str, ...]:
    return (f"Routed as {decision.describe()}.",) if config.ui.verbosity == "verbose" else ()


def render_answer(answer: Answer, decision: IntentDecision, config: Config) -> RenderableType:
    """The answer alone when concise; the model used when normal; routing too when verbose."""
    verbosity = config.ui.verbosity
    body: RenderableType = Markdown(answer.text) if answer.text else Text("(empty answer)")
    if verbosity == "concise":
        return body
    notes = [f"Answered with {answer.reply.describe()}.", *verbose_routing(config, decision)]
    if verbosity == "verbose" and answer.context:
        notes.append("Repository context: " + ", ".join(answer.context) + ".")
    return blocks(body, Group(*(muted(note) for note in notes)))

"""One continuous conversation at the root prompt: each turn answered, or delegated as work.

::

    user turn + compact conversation -> semantic router -> direct answer
                                                        -> repository-aware answer (read-only)
                                                        -> orchestrator (plans; never edits)

A pending planning question always takes the next line as its answer, before any routing, so a
question can never be skipped. Conversation history lives here, in memory, apart from canonical
task state: answers never touch the task store, and only delegated work creates tasks.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text

from agent_cli.adapters import AdapterRegistry
from agent_cli.budget import NO_PRICING, CostEstimator, SpendLedger
from agent_cli.config import Config, ConfigError, ConfigService
from agent_cli.gateway import NO_REPOSITORY
from agent_cli.gateway.answers import (
    Answer,
    AnswerCalls,
    DirectAnswerService,
    Question,
    RepositoryAnswerService,
)
from agent_cli.gateway.conversation import ANSWER_VIEW, HANDOFF_VIEW, ROUTER_VIEW, Conversation
from agent_cli.gateway.intent import (
    IntentRouter,
    ProviderRouter,
    RouteDecision,
    SemanticRouter,
)
from agent_cli.models import ROUTER_POLICY, ModelRegistry, RegistryError, load_registry
from agent_cli.orchestration.calls import PlanningUnavailable
from agent_cli.orchestration.domain import Handoff
from agent_cli.state import PinKind, TaskStateError, TaskStore
from agent_cli.ui import blocks, error, muted

type Orchestrate = Callable[[str, Handoff | None], RenderableType]
type RouterFactory = Callable[[Config, ModelRegistry], SemanticRouter | None]

ANSWER_HINT = "See /models for reachable models, or /startup to configure a provider."
ANSWERED_QUESTION = "(The user answered the planner's question.)"
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
    router: RouterFactory | None = None
    """Builds the semantic router; the low-cost router model path when ``None``."""
    conversation: Conversation = field(default_factory=Conversation)
    _calls: AnswerCalls | None = None
    _chat: DirectAnswerService | None = None
    _repository: RepositoryAnswerService | None = None

    @property
    def root(self) -> Path | None:
        return self.config.paths.project_root

    def handle(self, text: str) -> RenderableType:
        if self.orchestrate is not None and self.pending_question():
            self.conversation.add(text, ANSWERED_QUESTION, "orchestration")
            return self.orchestrate(text, None)
        try:
            config = self.config.resolve()
            calls = self.calls()
        except (ConfigError, RegistryError) as problem:
            return error("Cannot answer.", str(problem))

        in_repository = self.root is not None and self.orchestrate is not None
        router = IntentRouter(lambda: self.build_router(config, calls))
        decision = router.route(text, self.conversation.recent(ROUTER_VIEW), in_repository)
        if decision.intent == "orchestration":
            return self.delegate(text, decision, config)

        question = Question(text, decision.request, self.conversation.recent(ANSWER_VIEW))
        pins = explicit_pins(text, calls.registry, self.adapters)
        try:
            if decision.intent == "repository_question" and self.root is not None:
                if self._repository is None:
                    self._repository = RepositoryAnswerService(calls, self.root)
                answer = self._repository.answer(question, config, pins)
            else:
                if self._chat is None:
                    self._chat = DirectAnswerService(calls)
                answer = self._chat.answer(question, config, pins)
        except PlanningUnavailable as problem:
            return error(
                f"Cannot answer: {problem}", ANSWER_HINT, *verbose_routing(config, decision)
            )
        self.conversation.add(text, answer.text, decision.intent)
        return render_answer(answer, decision, config)

    def delegate(self, text: str, decision: RouteDecision, config: Config) -> RenderableType:
        """Hand the resolved request, and the conversation it came from, to the orchestrator."""
        if self.orchestrate is None:
            return NO_REPOSITORY
        handoff = Handoff(decision.request or text, self.conversation.transcript(HANDOFF_VIEW))
        output = self.orchestrate(text, handoff)
        self.conversation.add(text, f"Delegated to planning: {handoff.goal}", "orchestration")
        notes = verbose_routing(config, decision)
        if not notes:
            return output
        return blocks(output, Group(*(muted(note) for note in notes)))

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

    def build_router(self, config: Config, calls: AnswerCalls) -> SemanticRouter | None:
        """The cheapest adequate model path, or the user's selected low-cost model; budgeted
        like every other call. ``routing.classifier.mode: disabled`` turns routing off, and every
        turn is then answered read-only."""
        if self.router is not None:
            return self.router(config, calls.registry)
        settings = config.routing.classifier
        if settings.mode == "disabled":
            return None
        pins: dict[PinKind, str] = {}
        if settings.mode == "selected" and settings.selected_model:
            pins["model"] = settings.selected_model
        call = calls.call(config, pins)

        def route(system: str, prompt: str, max_output_tokens: int) -> tuple[str, str]:
            reply = call(ROUTER_POLICY, "intent_routing", system, prompt, max_output_tokens)
            return reply.text, reply.describe()

        return ProviderRouter(route)


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


def verbose_routing(config: Config, decision: RouteDecision) -> tuple[str, ...]:
    if config.ui.verbosity != "verbose":
        return ()
    resolved = (f"Understood as: {decision.request}",) if decision.request else ()
    return (f"Routed as {decision.describe()}.", *resolved)


def render_answer(answer: Answer, decision: RouteDecision, config: Config) -> RenderableType:
    """The answer alone when concise; the model used when normal; routing too when verbose."""
    verbosity = config.ui.verbosity
    body: RenderableType = Markdown(answer.text) if answer.text else Text("(empty answer)")
    if verbosity == "concise":
        return body
    notes = [f"Answered with {answer.reply.describe()}.", *verbose_routing(config, decision)]
    if verbosity == "verbose" and answer.context:
        notes.append("Repository context: " + ", ".join(answer.context) + ".")
    return blocks(body, Group(*(muted(note) for note in notes)))

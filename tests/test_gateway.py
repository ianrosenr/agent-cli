"""The root-prompt conversation (V1-09.9): semantic routing in context, read-only answers, and
delegation with resolved context.

The provider here plays the models: the router's judgment is scripted per test, the way a real
low-cost model would read each turn. What is tested is what the gateway does with that judgment
and with the conversation, not which phrases trigger which mode.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_cli import cli
from agent_cli.adapters import (
    AdapterRegistry,
    ModelDescriptor,
    ProviderIdentity,
    ProviderReadiness,
    ProviderRequest,
    ProviderResponse,
    ReadinessState,
)
from agent_cli.adapters.provider import ProviderAdapter
from agent_cli.budget import SpendLedger
from agent_cli.config import ConfigPaths, ConfigService
from agent_cli.gateway import NO_REPOSITORY
from agent_cli.gateway.answers import CHAT_SYSTEM, REPOSITORY_SYSTEM, scrub
from agent_cli.gateway.conversation import ROUTER_VIEW, Conversation
from agent_cli.gateway.intent import (
    SUPERVISOR_SYSTEM,
    FakeSupervisor,
    SupervisorRouter,
    Verdict,
    parse_verdict,
)
from agent_cli.gateway.service import SupervisorService, explicit_pins
from agent_cli.models import ModelRegistry
from agent_cli.models.provider_classifier import SYSTEM as SKILL_CLASSIFIER_SYSTEM
from agent_cli.orchestration import (
    FakePlanner,
    FakeSpecAuthor,
    TaskBrief,
    ModelOutputError,
    OrchestrationService,
    PlanningInput,
    PlanProposal,
    ProposedQuestion,
)
from agent_cli.state import TaskStore
from agent_cli.terms import AccessKind
from agent_cli.ui import plain
from gitrepo import commit, git, write
from test_models import model, registry

ANSWER = "The sky looks blue because air scatters short wavelengths."
IMPROVE = "I would cap retries in src/router/route.py and log each fallback."

type Judge = Callable[[str, list[dict[str, str]], bool], dict[str, Any]]


def judge(**routes: tuple[str, str] | str) -> Judge:
    """Script semantic supervisor outcomes using the current action vocabulary."""
    routes_by_label = {
        "answer": ("answer", False, "trivial"),
        "repo_answer": ("answer", True, "trivial"),
        "scout": ("scout", True, "trivial"),
        "ship": ("ship", True, "moderate"),
    }

    def decide(message: str, conversation: list[dict[str, str]], in_repo: bool) -> dict[str, Any]:
        route = routes.get(message)
        if route is None:
            return {
                "action": "ship",
                "confident": False,
                "request": message,
                "repository": in_repo,
                "complexity": "moderate",
            }
        label, request = route if isinstance(route, tuple) else (route, message)
        action, repository, complexity = routes_by_label.get(label, (label, in_repo, "moderate"))
        return {
            "action": action,
            "confident": True,
            "request": request,
            "repository": repository,
            "complexity": complexity,
        }

    return decide


@dataclass
class ModelProvider(ProviderAdapter):
    """Serves a cheap router model and an answer model, recording every request."""

    access: AccessKind | None = "subscription"
    model_ids: Sequence[str] = ("tiny", "chat-model")
    id: str = "models"
    answer: str = ANSWER
    route: Judge = field(default_factory=lambda: judge())
    requests: list[ProviderRequest] = field(default_factory=list)

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(self.id, self.id.title(), "fake", self.access)

    def readiness(self) -> ProviderReadiness:
        return ProviderReadiness(ReadinessState.READY)

    def list_models(self) -> Sequence[ModelDescriptor]:
        return [ModelDescriptor(self.id, m) for m in self.model_ids]

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if request.system == SUPERVISOR_SYSTEM:
            payload = json.loads(request.prompt)
            verdict = self.route(
                payload["message"], payload["conversation"], payload["in_repository"]
            )
            return ProviderResponse(request.model_id, json.dumps(verdict))
        return ProviderResponse(request.model_id, self.answer)

    def routed(self) -> list[dict[str, Any]]:
        return [json.loads(r.prompt) for r in self.requests if r.system == SUPERVISOR_SYSTEM]

    def answered(self) -> list[ProviderRequest]:
        return [r for r in self.requests if r.system != SUPERVISOR_SYSTEM]


def models() -> ModelRegistry:
    return registry(model("tiny", cost="low", scores={"reasoning": 2}), model("chat-model"))


@dataclass
class Recorder:
    seen: list[tuple[str, TaskBrief | None]] = field(default_factory=list)

    def __call__(self, text: str, handoff: TaskBrief | None = None) -> str:
        self.seen.append((text, handoff))
        return f"planned: {handoff.goal if handoff else text}"


@pytest.fixture
def project(repo: Path) -> Path:
    write(repo, "src/router/route.py", "def route(turn):\n    return retry(turn)\n")
    write(repo, "src/settings.py", "API_KEY = 'hunter2'\nTIMEOUT = 3\n")
    write(repo, "tests/test_route.py", "from router.route import route\n")
    commit(repo)
    return repo


def config_for(isolated_home: Path, root: Path | None, verbosity: str = "normal") -> ConfigService:
    service = ConfigService(ConfigPaths(isolated_home / ".agent-cli" / "config.yaml", root))
    service.set_session({"ui": {"verbosity": verbosity}})
    return service


def gateway(
    config: ConfigService,
    *providers: ModelProvider,
    orchestrate: Any = None,
    store: TaskStore | None = None,
    **overrides: Any,
) -> SupervisorService:
    fields: dict[str, Any] = {
        "adapters": AdapterRegistry(providers=list(providers) or [ModelProvider()]),
        "orchestrate": orchestrate,
        "store": store,
        "registry": models(),
        "ledger": SpendLedger(config.paths.global_file.parent / "spend.sqlite"),
    }
    fields.update(overrides)
    return SupervisorService(config, **fields)


def no_task_state(store: TaskStore) -> bool:
    return not store.exists() or store.latest_session() is None


# -- The turns the conversation must get right -------------------------------------------------


def test_write_me_a_poem_is_answered_directly(project: Path, isolated_home: Path) -> None:
    provider = ModelProvider(route=judge(**{"write me a poem": "answer"}))
    plans = Recorder()
    output = plain(
        gateway(config_for(isolated_home, project), provider, orchestrate=plans).handle(
            "write me a poem"
        )
    )
    assert ANSWER in output and plans.seen == []
    [answered] = provider.answered()
    assert answered.system == CHAT_SYSTEM and answered.model_id == "chat-model"


def test_improving_this_router_is_a_repository_answer(project: Path, isolated_home: Path) -> None:
    question = "How would you improve this router?"
    provider = ModelProvider(
        answer=IMPROVE, route=judge(**{question: ("repo_answer", question)})
    )
    plans = Recorder()
    store = TaskStore.for_repository(project)
    output = plain(
        gateway(
            config_for(isolated_home, project, "verbose"), provider, orchestrate=plans, store=store
        ).handle(question)
    )

    [answered] = provider.answered()
    assert answered.system == REPOSITORY_SYSTEM
    files = {f["path"]: f for f in json.loads(answered.prompt)["repository"]["files"]}
    assert "def route" in files["src/router/route.py"]["excerpt"]
    assert IMPROVE in output and "src/router/route.py" in output
    assert plans.seen == [] and no_task_state(store)
    assert git(project, "status", "--porcelain", "--untracked-files=no") == ""


def test_checking_pytest_is_a_read_only_repository_task(project: Path, isolated_home: Path) -> None:
    question = "are there any pytests failing on this feature branch"
    provider = ModelProvider(route=judge(**{question: ("scout", question)}))
    plans = Recorder()

    output = gateway(config_for(isolated_home, project), provider, orchestrate=plans).handle(
        question
    )

    assert output == f"planned: {question}"
    [(text, handoff)] = plans.seen
    assert text == question
    assert handoff == TaskBrief(question, "", "scout", "trivial")
    assert provider.answered() == []


def test_fix_this_bug_is_delegated_to_the_orchestrator(project: Path, isolated_home: Path) -> None:
    provider = ModelProvider(
        route=judge(**{"fix this bug": ("ship", "Fix the retry bug in route()")})
    )
    plans = Recorder()
    output = gateway(config_for(isolated_home, project), provider, orchestrate=plans).handle(
        "fix this bug"
    )
    assert output == "planned: Fix the retry bug in route()"
    [(text, handoff)] = plans.seen
    assert text == "fix this bug" and handoff is not None
    assert provider.answered() == []


def test_go_ahead_and_do_that_delegates_the_change_from_the_previous_answer(
    project: Path, isolated_home: Path
) -> None:
    question = "How would you improve this router?"
    resolved = "Cap retries in src/router/route.py and log each fallback"

    def decide(message: str, conversation: list[dict[str, str]], in_repo: bool) -> dict[str, Any]:
        if message == question:
            return {
                "action": "answer",
                "confident": True,
                "request": question,
                "repository": True,
                "complexity": "trivial",
            }
        # The supervisor resolves "that" from the previous answer it was shown.
        assert conversation[-1] == {"user": question, "assistant": IMPROVE}
        return {
            "action": "ship",
            "confident": True,
            "request": resolved,
            "repository": True,
            "complexity": "moderate",
        }

    provider = ModelProvider(answer=IMPROVE, route=decide)
    plans = Recorder()
    service = gateway(config_for(isolated_home, project), provider, orchestrate=plans)

    service.handle(question)
    output = service.handle("go ahead and do that")

    assert output == f"planned: {resolved}"
    [(text, handoff)] = plans.seen
    assert text == "go ahead and do that"
    assert handoff == TaskBrief(resolved, f"User: {question}\nAssistant: {IMPROVE}")


def test_the_orchestrator_plans_the_resolved_goal_with_the_conversation_as_context(
    project: Path, isolated_home: Path
) -> None:
    store = TaskStore.for_repository(project)
    config = config_for(isolated_home, project)
    planner = FakePlanner([PlanProposal(summary="s", questions=(ProposedQuestion("q?", "w"),))])
    orchestration = OrchestrationService(
        store, config, planner=planner, spec_author=FakeSpecAuthor(), classifier=lambda *_: None
    )
    handoff = TaskBrief("Cap retries in route()", f"User: improve it?\nAssistant: {IMPROVE}")

    orchestration.handle("go ahead and do that", handoff)

    [planning] = planner.inputs
    assert planning.goal == "Cap retries in route()"
    assert [r.text for r in planning.requirements] == ["go ahead and do that"]
    assert planning.context == (handoff.context,)
    # The conversation is evidence, never the user's words: it can authorize nothing.
    assert IMPROVE not in " ".join(planning.user_texts)


def test_ordinary_conversation_creates_no_task_state(project: Path, isolated_home: Path) -> None:
    store = TaskStore.for_repository(project)
    provider = ModelProvider(
        route=judge(
            **{
                "what is a daemon": "answer",
                "how does routing work here?": "repo_answer",
                "hello": "answer",
            }
        )
    )
    service = gateway(
        config_for(isolated_home, project), provider, orchestrate=Recorder(), store=store
    )
    for turn in ("hello", "what is a daemon", "how does routing work here?", "hmm, maybe later"):
        service.handle(turn)
    assert no_task_state(store)
    assert len(service.conversation.turns) == 4


# -- Uncertainty stays read-only ---------------------------------------------------------------


def test_unclear_intent_answers_read_only_and_never_plans(
    project: Path, isolated_home: Path
) -> None:
    provider = ModelProvider()  # the router is unsure about everything
    plans = Recorder()
    config = config_for(isolated_home, project, "verbose")
    output = plain(
        gateway(config, provider, orchestrate=plans).handle("the cache might need rethinking")
    )
    assert plans.seen == []
    assert provider.answered()[0].system == REPOSITORY_SYSTEM
    assert "uncertain work intent; staying read-only" in output


def test_an_unclear_turn_outside_a_repository_is_chat(isolated_home: Path) -> None:
    provider = ModelProvider()
    output = plain(gateway(config_for(isolated_home, None), provider).handle("thoughts on caching"))
    assert ANSWER in output and provider.answered()[0].system == CHAT_SYSTEM


def test_work_without_a_concrete_request_stays_read_only() -> None:
    router = FakeSupervisor([Verdict("ship", True, "", True, "moderate")])
    decision = SupervisorRouter(lambda: router).route("do it", (), True)
    assert decision.action == "answer" and decision.repository


@pytest.mark.parametrize(
    "failure",
    [lambda: FakeSupervisor([ModelOutputError("bad")]), lambda: None],
    ids=["router-failed", "no-router"],
)
def test_routing_failures_answer_as_chat(failure: Callable[[], Any]) -> None:
    decision = SupervisorRouter(failure).route("rewrite the cache layer", (), True)
    assert decision.action == "answer" and decision.source == "default"


def test_routing_disabled_answers_every_turn_as_chat(project: Path, isolated_home: Path) -> None:
    config = config_for(isolated_home, project)
    config.set_session({"routing": {"supervisor": {"mode": "disabled"}}})
    provider = ModelProvider(route=judge(**{"fix this bug": "ship"}))
    plans = Recorder()
    gateway(config, provider, orchestrate=plans).handle("fix this bug")
    assert provider.routed() == [] and plans.seen == []


def test_delegation_outside_a_repository_explains_where_to_run(isolated_home: Path) -> None:
    provider = ModelProvider(route=judge(**{"add a health endpoint": "ship"}))
    output = gateway(config_for(isolated_home, None), provider).handle("add a health endpoint")
    assert output == NO_REPOSITORY and provider.answered() == []


def test_a_pending_planning_question_takes_the_next_line_before_routing(
    project: Path, isolated_home: Path
) -> None:
    store = TaskStore.for_repository(project)
    session = store.start_session()
    root, _ = store.start_root_task(session.id, "add caching")
    store.ask_planning_question(root.id, "Proceed with Redis?", "It adds a dependency.")
    provider = ModelProvider()
    plans = Recorder()

    gateway(config_for(isolated_home, project), provider, orchestrate=plans, store=store).handle(
        "do it"
    )

    assert plans.seen == [("do it", None)] and provider.requests == []


# -- Conversation context ----------------------------------------------------------------------


def test_follow_up_answers_see_the_recent_conversation(isolated_home: Path) -> None:
    first, follow = "compare postgres and mongodb", "what about the other option?"
    provider = ModelProvider(route=judge(**{first: "answer", follow: "answer"}))
    service = gateway(config_for(isolated_home, None), provider)
    service.handle(first)
    service.handle(follow)

    follow_up = json.loads(provider.answered()[1].prompt)
    assert follow_up["conversation"] == [{"user": first, "assistant": ANSWER}]
    assert follow_up["message"] == follow
    assert provider.routed()[1]["conversation"] == follow_up["conversation"]


def test_the_router_sees_only_a_compact_recent_conversation() -> None:
    conversation = Conversation()
    for index in range(10):
        conversation.add(f"question {index}", "x" * 5000, "answer")
    recent = conversation.recent(ROUTER_VIEW)
    assert len(recent) == ROUTER_VIEW.turns
    assert recent[-1]["user"] == "question 9"
    assert all(len(turn["assistant"]) <= ROUTER_VIEW.assistant_chars for turn in recent)


def test_greetings_need_no_model_call(isolated_home: Path) -> None:
    provider = ModelProvider()
    output = plain(gateway(config_for(isolated_home, None), provider).handle("hello!"))
    assert "what do you want to work on" in output
    assert provider.requests == []


def test_concise_verbosity_shows_only_the_answer(project: Path, isolated_home: Path) -> None:
    provider = ModelProvider(route=judge(**{"what color is the sky": "answer"}))
    config = config_for(isolated_home, project, "concise")
    output = plain(
        gateway(config, provider, orchestrate=Recorder()).handle("what color is the sky")
    )
    assert output.strip() == ANSWER


def test_verbose_verbosity_explains_routing_and_the_resolved_request(
    project: Path, isolated_home: Path
) -> None:
    provider = ModelProvider(
        route=judge(**{"fix it": ("ship", "Fix the retry bug in route()")})
    )
    config = config_for(isolated_home, project, "verbose")
    output = plain(gateway(config, provider, orchestrate=Recorder()).handle("fix it"))
    assert "Routed as ship/moderate, repository (supervisor: supervisor@3 (tiny via" in output
    assert "Understood as: Fix the retry bug in route()" in output


# -- The router model --------------------------------------------------------------------------


def test_trivial_general_answer_finishes_in_the_supervisor_call(isolated_home: Path) -> None:
    def answer_now(
        message: str, conversation: list[dict[str, str]], in_repo: bool
    ) -> dict[str, Any]:
        return {
            "action": "answer",
            "confident": True,
            "request": message,
            "repository": False,
            "complexity": "trivial",
            "answer": ANSWER,
        }

    provider = ModelProvider(route=answer_now)
    output = plain(
        gateway(config_for(isolated_home, None, "concise"), provider).handle("what is a daemon")
    )

    [routed] = provider.requests
    assert routed.system == SUPERVISOR_SYSTEM != SKILL_CLASSIFIER_SYSTEM
    assert routed.model_id == "tiny" and routed.max_output_tokens is not None
    assert output.strip() == ANSWER


def test_auto_router_uses_medium_cost_model_when_no_cheaper_router_exists(
    isolated_home: Path,
) -> None:
    provider = ModelProvider(
        model_ids=("chat-model",),
        route=judge(**{"what is a daemon": "answer"}),
    )

    gateway(
        config_for(isolated_home, None),
        provider,
        registry=registry(model("chat-model", cost="medium")),
    ).handle("what is a daemon")

    [routed, answered] = provider.requests
    assert routed.system == SUPERVISOR_SYSTEM
    assert routed.model_id == "chat-model"
    assert answered.model_id == "chat-model"


def test_a_selected_router_model_is_used_even_when_medium_cost(isolated_home: Path) -> None:
    config = config_for(isolated_home, None)
    config.set_session(
        {"routing": {"supervisor": {"mode": "selected", "selected_model": "chat-model"}}}
    )
    provider = ModelProvider(route=judge(**{"what is a daemon": "answer"}))
    gateway(config, provider).handle("what is a daemon")
    assert provider.requests[0].model_id == "chat-model"


def test_an_unpriced_paid_router_path_is_filtered_by_budget(isolated_home: Path) -> None:
    paid = ModelProvider(id="paid", access="api", model_ids=("tiny",))
    free = ModelProvider(id="free", model_ids=("tiny", "chat-model"))
    config = config_for(isolated_home, None, "verbose")
    service = gateway(config, paid, free)

    output = plain(service.handle("what is a daemon"))

    # No trusted price: the paid path is filtered before dispatch, and routing falls through
    # to the next admissible model instead.
    assert paid.requests == []
    assert free.requests[0].system == SUPERVISOR_SYSTEM and ANSWER in output


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            '{"action":"answer","confident":true,"request":"Explain closures",'
            '"repository":false,"complexity":"trivial"}',
            Verdict("answer", True, "Explain closures"),
        ),
        (
            '```json\n{"action":"ship","confident":false,"request":"",'
            '"repository":true,"complexity":"moderate"}\n```',
            Verdict("ship", False, "", True, "moderate"),
        ),
    ],
)
def test_router_replies_are_parsed_strictly(reply: str, expected: Verdict) -> None:
    assert parse_verdict(reply) == expected


@pytest.mark.parametrize(
    "reply",
    [
        "answer",
        '{"intent": "delete_everything", "confident": true}',
        '{"intent": "answer", "confident": "yes"}',
        '{"intent": "answer", "confident": true, "extra": 1}',
    ],
)
def test_malformed_router_replies_are_rejected(reply: str) -> None:
    with pytest.raises(ModelOutputError):
        parse_verdict(reply)


# -- Final model routing: policy, pins, access, budget -----------------------------------------


def answer_models() -> ModelRegistry:
    return registry(
        model("tiny", cost="low", scores={"reasoning": 2}),
        model("premium", cost="high", scores={"reasoning": 5}),
        model("everyday", cost="medium"),
    )


def chat_provider(*model_ids: str) -> ModelProvider:
    return ModelProvider(model_ids=("tiny", *model_ids))


def test_answers_prefer_a_cheaper_adequate_model(isolated_home: Path) -> None:
    provider = chat_provider("premium", "everyday")
    gateway(config_for(isolated_home, None), provider, registry=answer_models()).handle("hm?")
    assert provider.answered()[0].model_id == "everyday"


def test_an_explicit_model_pin_applies_to_the_answer(isolated_home: Path) -> None:
    provider = chat_provider("premium", "everyday")
    gateway(config_for(isolated_home, None), provider, registry=answer_models()).handle(
        "use premium: what is a daemon?"
    )
    assert provider.answered()[0].model_id == "premium"


def test_an_unavailable_pinned_model_is_never_substituted(isolated_home: Path) -> None:
    provider = chat_provider("everyday")
    output = plain(
        gateway(config_for(isolated_home, None), provider, registry=answer_models()).handle(
            "ask premium what a daemon is"
        )
    )
    assert provider.answered() == []
    assert "Cannot answer" in output and "premium" in output


def test_an_explicit_provider_pin_applies_to_the_answer(isolated_home: Path) -> None:
    first = ModelProvider(id="alpha")
    second = ModelProvider(id="beta")
    gateway(config_for(isolated_home, None), first, second).handle("via beta, what is a daemon?")
    assert first.answered() == [] and len(second.answered()) == 1


def test_passing_words_are_not_pins() -> None:
    known = registry(model("premium"))
    adapters = AdapterRegistry(providers=[ModelProvider(id="beta")])
    assert explicit_pins("use a hammer via the stairs", known, adapters) == {}
    assert explicit_pins("is premium better than beta?", known, adapters) == {}
    assert explicit_pins("how do I use beta embeddings?", known, adapters) == {}


def test_a_leading_model_and_provider_pin_are_both_read() -> None:
    known = registry(model("premium"))
    adapters = AdapterRegistry(providers=[ModelProvider(id="beta")])
    assert explicit_pins("use premium via beta: what is a daemon?", known, adapters) == {
        "model": "premium",
        "provider": "beta",
    }


def test_access_preference_applies_to_the_answer(isolated_home: Path) -> None:
    local = ModelProvider(id="local-box", access="local")
    subscription = ModelProvider(id="sub", access="subscription")
    config = config_for(isolated_home, None)
    config.set_session({"access": {"preference": ["local", "subscription", "api"]}})
    gateway(config, subscription, local).handle("what is a daemon")
    assert len(local.requests) == 2 and subscription.requests == []


def test_budget_limits_apply_to_paid_answers(isolated_home: Path) -> None:
    paid = ModelProvider(access="api")
    output = plain(gateway(config_for(isolated_home, None), paid).handle("what is a daemon"))
    assert paid.requests == []
    assert "Cannot answer" in output


def test_the_registry_and_ledger_are_built_once_per_session(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.gateway import service as service_module

    loads: list[int] = []
    ledgers: list[int] = []

    def load() -> ModelRegistry:
        loads.append(1)
        return models()

    def default(home: Path | None = None) -> SpendLedger:
        ledgers.append(1)
        return SpendLedger(isolated_home / "ledger.sqlite")

    monkeypatch.setattr(service_module, "load_registry", load)
    monkeypatch.setattr(SpendLedger, "default", staticmethod(default))
    service = gateway(config_for(isolated_home, None), registry=None, ledger=None)
    for _ in range(3):
        service.handle("what is a daemon")
    assert loads == [1] and ledgers == [1]


# -- Repository context safety -----------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        '"password": "hunter2",',
        "AWS_SECRET_ACCESS_KEY=hunter2",
        "DATABASE_URL=postgres://user:hunter2@db/app",
        "client_secret: hunter2",
        "key = 'AKIAABCDEFGHIJKLMNOP'",
        "token = 'ghp_hunter2hunter2hunter2'",
    ],
)
def test_secret_lines_are_scrubbed(line: str) -> None:
    assert scrub(f"TIMEOUT = 3\n{line}\nRETRIES = 2") == "TIMEOUT = 3\nRETRIES = 2"


def test_private_key_blocks_are_scrubbed() -> None:
    text = "a = 1\n-----BEGIN RSA PRIVATE KEY-----\nMIIxyz\n-----END RSA PRIVATE KEY-----\nb = 2"
    assert "MIIxyz" not in scrub(text) and "b = 2" in scrub(text)


def repository_turn(project: Path, isolated_home: Path, question: str) -> dict[str, Any]:
    provider = ModelProvider(route=judge(**{question: "repo_answer"}))
    gateway(config_for(isolated_home, project), provider, orchestrate=Recorder()).handle(question)
    prompt = json.loads(provider.answered()[0].prompt)
    return {f["path"]: f for f in prompt["repository"]["files"]}


def test_repository_excerpts_drop_secret_looking_lines(project: Path, isolated_home: Path) -> None:
    files = repository_turn(project, isolated_home, "what does src/settings.py configure?")
    assert "hunter2" not in files["src/settings.py"]["excerpt"]
    assert "TIMEOUT" in files["src/settings.py"]["excerpt"]


def test_sensitive_files_are_never_excerpted(project: Path, isolated_home: Path) -> None:
    write(project, "config/credentials.py", "ROTATION_DAYS = 30\n")
    commit(project)
    files = repository_turn(project, isolated_home, "what does config/credentials.py configure?")
    assert "excerpt" not in files["config/credentials.py"]


# -- Wiring ------------------------------------------------------------------------------------


def test_the_shell_answers_chat_outside_a_repository(isolated_home: Path) -> None:
    config = config_for(isolated_home, None)
    provider = ModelProvider(
        model_ids=("claude-sonnet-5-5",),
        route=judge(**{"what color is the sky": "answer", "add a health endpoint": "ship"}),
    )
    respond = cli.gateway(config, AdapterRegistry(providers=[provider]), None)
    assert respond is not None
    assert ANSWER in plain(respond("what color is the sky"))
    assert respond("add a health endpoint") == cli.NO_ORCHESTRATION


def test_planning_context_is_rendered_for_the_planner() -> None:
    from agent_cli.orchestration.domain import RepositoryContext
    from agent_cli.orchestration.planner import render_prompt

    planning = PlanningInput(
        goal="Cap retries",
        requirements=(),
        decisions=(),
        answers=(),
        repository=RepositoryContext(),
        context=("User: improve it?\nAssistant: cap retries",),
    )
    assert json.loads(render_prompt(planning))["context"] == [planning.context[0]]

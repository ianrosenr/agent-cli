"""Root-prompt intent routing (V1-09.9): chat, read-only repository answers, orchestration."""

from __future__ import annotations

import json
from collections.abc import Sequence
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
from agent_cli.gateway.intent import (
    INTENT_SYSTEM,
    FakeIntentClassifier,
    IntentRouter,
    ProviderIntentClassifier,
    RootIntent,
    classify_by_rules,
    parse_intent,
)
from agent_cli.gateway.service import RootPromptGateway, explicit_pins
from agent_cli.models import ModelRegistry
from agent_cli.models.provider_classifier import SYSTEM as SKILL_CLASSIFIER_SYSTEM
from agent_cli.orchestration import ModelOutputError
from agent_cli.state import TaskStore
from agent_cli.terms import AccessKind
from agent_cli.ui import plain
from gitrepo import commit, git, write
from test_models import model, registry

ANSWER = "The sky looks blue because air scatters short wavelengths."


@dataclass
class AnsweringProvider(ProviderAdapter):
    """Answers chat prompts in prose and intent prompts with a scripted intent."""

    access: AccessKind | None = "subscription"
    model_ids: Sequence[str] = ("chat-model",)
    id: str = "answering"
    answer: str = ANSWER
    intent: dict[str, Any] = field(default_factory=lambda: {"intent": "chat", "confident": True})
    requests: list[ProviderRequest] = field(default_factory=list)

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(self.id, self.id.title(), "fake", self.access)

    def readiness(self) -> ProviderReadiness:
        return ProviderReadiness(ReadinessState.READY)

    def list_models(self) -> Sequence[ModelDescriptor]:
        return [ModelDescriptor(self.id, m) for m in self.model_ids]

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if request.system == INTENT_SYSTEM:
            return ProviderResponse(request.model_id, json.dumps(self.intent))
        return ProviderResponse(request.model_id, self.answer)


@dataclass
class Recorder:
    seen: list[str] = field(default_factory=list)

    def __call__(self, text: str) -> str:
        self.seen.append(text)
        return f"planned: {text}"


@pytest.fixture
def project(repo: Path) -> Path:
    write(repo, "src/auth/login.py", "def authenticate(user):\n    return user.ok\n")
    write(repo, "src/settings.py", "API_KEY = 'hunter2'\nTIMEOUT = 3\n")
    write(repo, "tests/test_login.py", "from auth.login import authenticate\n")
    commit(repo)
    return repo


def config_for(isolated_home: Path, root: Path | None, verbosity: str = "normal") -> ConfigService:
    service = ConfigService(ConfigPaths(isolated_home / ".agent-cli" / "config.yaml", root))
    service.set_session({"ui": {"verbosity": verbosity}})
    return service


def gateway(
    config: ConfigService,
    *providers: AnsweringProvider,
    orchestrate: Recorder | None = None,
    store: TaskStore | None = None,
    models: ModelRegistry | None = None,
    **overrides: Any,
) -> RootPromptGateway:
    fields: dict[str, Any] = {
        "adapters": AdapterRegistry(providers=list(providers) or [AnsweringProvider()]),
        "orchestrate": orchestrate,
        "store": store,
        "registry": models or registry(model("chat-model")),
        "classifier": lambda *_: None,
    }
    fields.update(overrides)
    return RootPromptGateway(config, **fields)


# -- Chat --------------------------------------------------------------------------------------


def test_what_color_is_the_sky_gets_a_direct_answer(project: Path, isolated_home: Path) -> None:
    provider = AnsweringProvider()
    plans = Recorder()
    store = TaskStore.for_repository(project)
    output = plain(
        gateway(
            config_for(isolated_home, project), provider, orchestrate=plans, store=store
        ).handle("what color is the sky")
    )

    assert ANSWER in output
    assert "Answered with chat-model via answering (subscription)." in output
    assert "planned" not in output and plans.seen == []
    [request] = provider.requests
    assert request.system == CHAT_SYSTEM
    assert request.prompt == "what color is the sky"


def test_ordinary_chat_creates_no_task_state(project: Path, isolated_home: Path) -> None:
    store = TaskStore.for_repository(project)
    service = gateway(config_for(isolated_home, project), orchestrate=Recorder(), store=store)

    service.handle("what is a daemon")
    service.handle("explain dependency injection")

    assert not store.exists() or store.latest_session() is None


def test_chat_works_outside_a_git_repository(tmp_path: Path, isolated_home: Path) -> None:
    output = plain(gateway(config_for(isolated_home, None)).handle("compare postgres and mongodb"))
    assert ANSWER in output


def test_change_requests_outside_a_repository_explain_where_to_run(
    isolated_home: Path,
) -> None:
    provider = AnsweringProvider()
    output = gateway(config_for(isolated_home, None), provider).handle("add a health endpoint")
    assert output == NO_REPOSITORY
    assert provider.requests == []


def test_concise_verbosity_shows_only_the_answer(project: Path, isolated_home: Path) -> None:
    config = config_for(isolated_home, project, "concise")
    output = plain(gateway(config, orchestrate=Recorder()).handle("what color is the sky"))
    assert output.strip() == ANSWER


def test_verbose_verbosity_explains_the_routing(project: Path, isolated_home: Path) -> None:
    config = config_for(isolated_home, project, "verbose")
    output = plain(gateway(config, orchestrate=Recorder()).handle("what color is the sky"))
    assert "Routed as chat (rules: general question)." in output


# -- Repository questions ----------------------------------------------------------------------


def test_repository_questions_get_bounded_read_only_context(
    project: Path, isolated_home: Path
) -> None:
    provider = AnsweringProvider(answer="Authentication lives in src/auth/login.py.")
    plans = Recorder()
    store = TaskStore.for_repository(project)
    config = config_for(isolated_home, project, "verbose")
    output = plain(
        gateway(config, provider, orchestrate=plans, store=store).handle(
            "where is authenticate defined in this repo?"
        )
    )

    [request] = provider.requests
    assert request.system == REPOSITORY_SYSTEM
    prompt = json.loads(request.prompt)
    files = {f["path"]: f for f in prompt["repository"]["files"]}
    assert "src/auth/login.py" in files
    assert "def authenticate" in files["src/auth/login.py"]["excerpt"]
    assert len(files) <= 12
    assert "src/auth/login.py" in output
    assert plans.seen == []
    assert not store.exists() or store.latest_session() is None
    assert git(project, "status", "--porcelain", "--untracked-files=no") == ""


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


def test_sensitive_files_are_never_excerpted(project: Path, isolated_home: Path) -> None:
    write(project, "config/credentials.py", "ROTATION_DAYS = 30\n")
    commit(project)
    provider = AnsweringProvider()
    gateway(config_for(isolated_home, project), provider, orchestrate=Recorder()).handle(
        "what does config/credentials.py configure?"
    )
    prompt = json.loads(provider.requests[0].prompt)
    files = {f["path"]: f for f in prompt["repository"]["files"]}
    assert "config/credentials.py" in files
    assert "excerpt" not in files["config/credentials.py"]


def test_repository_excerpts_drop_secret_looking_lines(project: Path, isolated_home: Path) -> None:
    provider = AnsweringProvider()
    gateway(config_for(isolated_home, project), provider, orchestrate=Recorder()).handle(
        "what does src/settings.py configure?"
    )
    prompt = json.loads(provider.requests[0].prompt)
    excerpt = next(f for f in prompt["repository"]["files"] if f["path"] == "src/settings.py")
    assert "hunter2" not in excerpt.get("excerpt", "")
    assert "TIMEOUT" in excerpt["excerpt"]


# -- Orchestration and the safety boundary -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "implement issue #10",
        "add expiration support",
        "fix this bug",
        "refactor provider selection",
        "go ahead and fix this bug",
    ],
)
def test_implementation_requests_still_route_to_orchestration(
    project: Path, isolated_home: Path, text: str
) -> None:
    provider = AnsweringProvider()
    plans = Recorder()
    output = gateway(config_for(isolated_home, project), provider, orchestrate=plans).handle(text)
    assert output == f"planned: {text}"
    assert provider.requests == []


def test_a_pending_planning_question_takes_the_next_line_before_routing(
    project: Path, isolated_home: Path
) -> None:
    store = TaskStore.for_repository(project)
    session = store.start_session()
    root, _ = store.start_root_task(session.id, "add caching")
    store.ask_planning_question(root.id, "Which backend?", "It changes the data model.")
    plans = Recorder()

    output = gateway(config_for(isolated_home, project), orchestrate=plans, store=store).handle(
        "what is redis"
    )

    assert output == "planned: what is redis"


@pytest.mark.parametrize(
    "text", ["go ahead and make that change", "do it", "ok, make those changes you suggested"]
)
def test_a_bare_go_ahead_asks_for_the_change_instead_of_planning_it(
    project: Path, isolated_home: Path, text: str
) -> None:
    provider = AnsweringProvider()
    plans = Recorder()
    output = plain(
        gateway(config_for(isolated_home, project), provider, orchestrate=plans).handle(text)
    )
    assert plans.seen == [] and provider.requests == []
    assert "Restate the change" in output


def test_a_go_ahead_still_answers_a_pending_planning_question(
    project: Path, isolated_home: Path
) -> None:
    store = TaskStore.for_repository(project)
    session = store.start_session()
    root, _ = store.start_root_task(session.id, "add caching")
    store.ask_planning_question(root.id, "Proceed with Redis?", "It adds a dependency.")
    plans = Recorder()
    gateway(config_for(isolated_home, project), orchestrate=plans, store=store).handle("do it")
    assert plans.seen == ["do it"]


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ([("orchestration", False)], "classifier unsure"),
        ([ModelOutputError("bad")], "classifier failed"),
    ],
)
def test_ambiguous_intent_fails_toward_chat_never_work(
    project: Path, isolated_home: Path, script: list[Any], reason: str
) -> None:
    provider = AnsweringProvider()
    plans = Recorder()
    classifier = FakeIntentClassifier(script)
    config = config_for(isolated_home, project, "verbose")
    output = plain(
        gateway(config, provider, orchestrate=plans, classifier=lambda *_: classifier).handle(
            "write a poem about the sea"
        )
    )
    assert plans.seen == []
    assert classifier.seen == ["write a poem about the sea"]
    assert ANSWER in output and reason in output


def test_ambiguous_intent_without_a_classifier_is_chat(project: Path, isolated_home: Path) -> None:
    plans = Recorder()
    output = plain(
        gateway(config_for(isolated_home, project), orchestrate=plans).handle(
            "write a poem about the sea"
        )
    )
    assert plans.seen == [] and ANSWER in output


def test_a_confident_classifier_can_route_ambiguous_text_to_orchestration(
    project: Path, isolated_home: Path
) -> None:
    plans = Recorder()
    classifier = FakeIntentClassifier([("orchestration", True)])
    gateway(
        config_for(isolated_home, project), orchestrate=plans, classifier=lambda *_: classifier
    ).handle("the cache should expire entries after an hour")
    assert plans.seen == ["the cache should expire entries after an hour"]


def test_clear_prompts_never_build_the_classifier(project: Path, isolated_home: Path) -> None:
    def explode(*_: Any) -> None:
        raise AssertionError("classifier built for a clear prompt")

    service = gateway(
        config_for(isolated_home, project), orchestrate=Recorder(), classifier=explode
    )
    service.handle("what color is the sky")
    service.handle("fix this bug")


# -- The intent classifier ---------------------------------------------------------------------


def test_root_intent_classifier_uses_the_cheap_configured_model_not_the_skill_classifier(
    project: Path, isolated_home: Path
) -> None:
    cheap = AnsweringProvider(
        id="cheap", model_ids=("tiny",), intent={"intent": "chat", "confident": True}
    )
    strong = AnsweringProvider(id="strong", model_ids=("chat-model",))
    models = registry(
        model("tiny", cost="low", scores={"reasoning": 2}),
        model("chat-model"),
    )
    service = RootPromptGateway(
        config_for(isolated_home, project, "verbose"),
        AdapterRegistry(providers=[cheap, strong]),
        Recorder(),
        registry=models,
    )

    output = plain(service.handle("write a poem about the sea"))

    [classified] = cheap.requests
    assert classified.system == INTENT_SYSTEM != SKILL_CLASSIFIER_SYSTEM
    assert classified.model_id == "tiny"
    assert classified.max_output_tokens is not None
    [answered] = strong.requests
    assert answered.system == CHAT_SYSTEM
    assert "intent-classifier@1 (tiny via cheap" in output


def test_a_paid_intent_classifier_goes_through_the_budget_guard(
    project: Path, isolated_home: Path
) -> None:
    cheap = AnsweringProvider(id="cheap", model_ids=("tiny",), access="api")
    models = registry(model("tiny", cost="low", scores={"reasoning": 2}), model("chat-model"))
    ledger = SpendLedger(isolated_home / "ledger.sqlite")
    service = RootPromptGateway(
        config_for(isolated_home, project, "verbose"),
        AdapterRegistry(providers=[cheap, AnsweringProvider()]),
        Recorder(),
        registry=models,
        ledger=ledger,
    )

    output = plain(service.handle("write a poem about the sea"))

    assert cheap.requests == []  # no trusted price: rejected before dispatch
    assert "classifier failed" in output and ANSWER in output
    assert ledger.admissions()


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"intent": "chat", "confident": true}', ("chat", True)),
        ('```json\n{"intent": "orchestration", "confident": false}\n```', ("orchestration", False)),
    ],
)
def test_intent_replies_are_parsed_strictly(reply: str, expected: tuple[RootIntent, bool]) -> None:
    assert parse_intent(reply) == expected


@pytest.mark.parametrize(
    "reply",
    [
        "chat",
        '{"intent": "delete_everything", "confident": true}',
        '{"intent": "chat", "confident": "yes"}',
        '{"intent": "chat", "confident": true, "extra": 1}',
    ],
)
def test_malformed_intent_replies_are_rejected(reply: str) -> None:
    with pytest.raises(ModelOutputError):
        parse_intent(reply)


def test_provider_intent_classifier_sends_only_the_message() -> None:
    provider = AnsweringProvider(intent={"intent": "repository_question", "confident": True})
    path = AdapterRegistry(providers=[provider])
    from agent_cli.config import Config
    from agent_cli.models import runtime_paths

    [model_path] = runtime_paths(path, Config()).serving(("chat-model",))
    classifier = ProviderIntentClassifier(provider, model_path, lambda a, r: a.invoke(r))
    assert classifier.classify("how is X built?", True) == ("repository_question", True)
    assert "how is X built?" in provider.requests[0].prompt


def test_repository_intent_outside_a_repository_becomes_chat() -> None:
    router = IntentRouter(lambda: FakeIntentClassifier([("repository_question", True)]))
    assert router.route("the frobnicator is slow", False).intent == "chat"


# -- Rules -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "in_repository", "intent"),
    [
        ("what color is the sky", False, "chat"),
        ("what color is the sky", True, "chat"),
        ("what is a daemon", True, "chat"),
        ("compare postgres and mongodb", True, "chat"),
        ("help me think through this architecture", True, "chat"),
        ("hello", True, "chat"),
        ("can you explain closures?", True, "chat"),
        ("explain dependency injection", False, "chat"),
        ("where is auth handled?", True, "repository_question"),
        ("why does this test fail?", True, "repository_question"),
        ("how does our provider routing work?", True, "repository_question"),
        ("what would you change about this module?", True, "repository_question"),
        ("where is auth handled?", False, "chat"),
        ("implement issue #10", True, "orchestration"),
        ("add expiration support", True, "orchestration"),
        ("fix this bug", True, "orchestration"),
        ("refactor provider selection", True, "orchestration"),
        ("can you fix the login bug?", True, "orchestration"),
        ("go ahead and make that change", True, "orchestration"),
        ("do it", True, "orchestration"),
        ("go ahead please", True, "orchestration"),
        ("go ahead with the changes", True, "orchestration"),
        ("write tests for the cache", True, "orchestration"),
        ("update src/cache/store.py", True, "orchestration"),
        ("write a poem about the sea", True, None),
        ("fix my sentence: he go home", False, None),
        ("remove duplicates from this list: a, b, a", True, None),
        ("fix the grammar in this sentence: he go home", True, None),
        ("write a summary of python 3.12 features", True, None),
        ("write an email, e.g. a thank-you note", True, None),
        ("what's the difference between TCP/IP and UDP?", True, "chat"),
    ],
)
def test_rules_route_clear_prompts_and_leave_the_rest_to_the_classifier(
    text: str, in_repository: bool, intent: RootIntent | None
) -> None:
    decided = classify_by_rules(text, in_repository)
    assert (decided and decided.intent) == intent


# -- Final model routing: policy, pins, access, budget -----------------------------------------


def test_chat_prefers_a_cheaper_adequate_model(isolated_home: Path) -> None:
    provider = AnsweringProvider(model_ids=("premium", "everyday"))
    models = registry(
        model("premium", cost="high", scores={"reasoning": 5}),
        model("everyday", cost="low"),
    )
    gateway(config_for(isolated_home, None), provider, models=models).handle("what is a daemon")
    assert provider.requests[0].model_id == "everyday"


def test_an_explicit_model_pin_applies_to_the_answer(isolated_home: Path) -> None:
    provider = AnsweringProvider(model_ids=("premium", "everyday"))
    models = registry(
        model("premium", cost="high", scores={"reasoning": 5}),
        model("everyday", cost="low"),
    )
    gateway(config_for(isolated_home, None), provider, models=models).handle(
        "use premium: what is a daemon?"
    )
    assert provider.requests[0].model_id == "premium"


def test_an_unavailable_pinned_model_is_never_substituted(isolated_home: Path) -> None:
    provider = AnsweringProvider(model_ids=("everyday",))
    models = registry(model("premium"), model("everyday"))
    output = plain(
        gateway(config_for(isolated_home, None), provider, models=models).handle(
            "ask premium what a daemon is"
        )
    )
    assert provider.requests == []
    assert "Cannot answer" in output and "premium" in output


def test_an_explicit_provider_pin_applies_to_the_answer(isolated_home: Path) -> None:
    first = AnsweringProvider(id="alpha")
    second = AnsweringProvider(id="beta")
    gateway(config_for(isolated_home, None), first, second).handle("via beta, what is a daemon?")
    assert first.requests == [] and len(second.requests) == 1


def test_passing_words_are_not_pins() -> None:
    models = registry(model("premium"))
    adapters = AdapterRegistry(providers=[AnsweringProvider(id="beta")])
    assert explicit_pins("use a hammer via the stairs", models, adapters) == {}
    assert explicit_pins("is premium better than beta?", models, adapters) == {}
    assert explicit_pins("how do I use beta embeddings?", models, adapters) == {}
    assert explicit_pins("should I ask premium or beta?", models, adapters) == {}


def test_a_leading_model_and_provider_pin_are_both_read() -> None:
    models = registry(model("premium"))
    adapters = AdapterRegistry(providers=[AnsweringProvider(id="beta")])
    assert explicit_pins("use premium via beta: what is a daemon?", models, adapters) == {
        "model": "premium",
        "provider": "beta",
    }


def test_a_passing_provider_mention_does_not_block_the_answer(isolated_home: Path) -> None:
    ready = AnsweringProvider(id="ready")
    output = plain(
        gateway(
            config_for(isolated_home, None),
            ready,
            AnsweringProvider(id="beta", model_ids=()),
        ).handle("how do I use beta embeddings?")
    )
    assert ANSWER in output and len(ready.requests) == 1


def test_access_preference_applies_to_the_answer(isolated_home: Path) -> None:
    local = AnsweringProvider(id="local-box", access="local")
    subscription = AnsweringProvider(id="sub", access="subscription")
    config = config_for(isolated_home, None)
    config.set_session({"access": {"preference": ["local", "subscription", "api"]}})
    gateway(config, subscription, local).handle("what is a daemon")
    assert len(local.requests) == 1 and subscription.requests == []


def test_budget_limits_apply_to_paid_answers(isolated_home: Path) -> None:
    paid = AnsweringProvider(access="api")
    ledger = SpendLedger(isolated_home / "ledger.sqlite")
    output = plain(
        gateway(config_for(isolated_home, None), paid, ledger=ledger).handle("what is a daemon")
    )
    assert paid.requests == []
    assert "Cannot answer" in output


def test_the_registry_and_ledger_are_built_once_per_session(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.gateway import service as service_module

    loads: list[int] = []
    ledgers: list[int] = []
    models = registry(model("chat-model"))

    def load() -> ModelRegistry:
        loads.append(1)
        return models

    def default(home: Path | None = None) -> SpendLedger:
        ledgers.append(1)
        return SpendLedger(isolated_home / "ledger.sqlite")

    monkeypatch.setattr(service_module, "load_registry", load)
    monkeypatch.setattr(SpendLedger, "default", staticmethod(default))
    service = gateway(config_for(isolated_home, None), registry=None)
    for _ in range(3):
        service.handle("what is a daemon")
    assert loads == [1] and ledgers == [1]


# -- Shell wiring ------------------------------------------------------------------------------


def test_the_shell_answers_chat_outside_a_repository(isolated_home: Path) -> None:
    config = config_for(isolated_home, None)
    provider = AnsweringProvider(model_ids=("claude-sonnet-5-5",))
    respond = cli.gateway(config, AdapterRegistry(providers=[provider]), None)
    assert respond is not None
    assert ANSWER in plain(respond("what color is the sky"))
    assert respond("add a health endpoint") == cli.NO_ORCHESTRATION

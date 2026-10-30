from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from agent_cli.commands import CommandContext, build_registry
from agent_cli.config import Config, ConfigError, ConfigPaths, ConfigService, Layer
from agent_cli.guided_setup import CANCELED, SETTINGS, GuidedSetup

ENTER_THROUGH = [""] * (len(SETTINGS) + 2)  # target layer, every setting, confirmation
ADVANCED = ("role_floors", "orchestrator", "routing", "capability", "fallback", "classifier")


class Conversation:
    """Scripted answers for the setup prompts, recording everything shown."""

    def __init__(self, *answers: str | type[BaseException]) -> None:
        self.answers: Iterator[str | type[BaseException]] = iter(answers)
        self.prompts: list[str] = []
        self.output: list[str] = []

    def read_line(self, prompt: str) -> str:
        self.prompts.append(prompt)
        answer = next(self.answers, EOFError)
        if isinstance(answer, str):
            return answer
        raise answer

    @property
    def transcript(self) -> str:
        return "\n".join(self.output + self.prompts)


@pytest.fixture
def paths(tmp_path: Path) -> ConfigPaths:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return ConfigPaths(tmp_path / "home" / ".agent-cli" / "config.yaml", repo)


@pytest.fixture
def service(paths: ConfigPaths) -> ConfigService:
    return ConfigService(paths)


def run(service: ConfigService, *answers: str | type[BaseException]) -> tuple[str, Conversation]:
    conversation = Conversation(*answers)
    result = GuidedSetup(service, conversation.read_line, conversation.output.append).run()
    return result, conversation


def answers_for(**values: str) -> list[str]:
    """Answers in prompt order: Enter for every setting except those named by dotted key
    (with ``.`` written as ``__``)."""
    wanted = {key.replace("__", "."): value for key, value in values.items()}
    return [wanted.pop(setting.key, "") for setting in SETTINGS]


def load(path: Path | None) -> object:
    assert path is not None
    return yaml.safe_load(path.read_text())


def test_enter_through_writes_a_valid_complete_global_config(
    service: ConfigService, paths: ConfigPaths
) -> None:
    result, _ = run(service, *ENTER_THROUGH)
    assert result == f"Settings saved to {paths.global_file}."
    assert ConfigService(paths).resolve() == Config()
    saved = load(paths.global_file)
    assert isinstance(saved, dict)
    assert saved["workers"] == {"max_concurrency": 3}
    assert saved["implementation"] == {"mode": "autopilot"}
    assert "routing" not in saved


def test_enter_through_outside_a_repository_writes_global(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / ".agent-cli" / "config.yaml", None)
    result, conversation = run(ConfigService(paths), *ENTER_THROUGH[1:])
    assert result == f"Settings saved to {paths.global_file}."
    assert "Not inside a Git repository" in conversation.transcript
    assert ConfigService(paths).resolve() == Config()


def test_answers_are_persisted(service: ConfigService, paths: ConfigPaths) -> None:
    answers = answers_for(
        access__preference="local, subscription",
        cost__monthly_limit_usd="$50",
        cost__per_task_limit_usd="2.5",
        cost__per_call_limit_usd="none",
        workers__max_concurrency="6",
        skills__paths="~/skills, /opt/team skills",
        implementation__mode="checkpoint",
        review__before_merge_request="n",
        git__use_worktrees="no",
        git__worktree_dir=".agent-cli/trees",
        git__branch_prefix="bot/",
    )
    result, _ = run(service, "global", *answers, "y")
    assert result.startswith("Settings saved")
    config = ConfigService(paths).resolve()
    assert config.access.preference == ("local", "subscription")
    assert config.cost.monthly_limit_usd == 50.0
    assert config.cost.per_task_limit_usd == 2.5
    assert config.cost.per_call_limit_usd is None
    assert config.workers.max_concurrency == 6
    assert config.skills.paths == ("~/skills", "/opt/team skills")
    assert config.implementation.mode == "checkpoint"
    assert config.review.before_merge_request is False
    assert config.git == type(config.git)(False, ".agent-cli/trees", "bot/")


def test_rerun_shows_current_values_as_defaults_and_keeps_them(
    service: ConfigService, paths: ConfigPaths
) -> None:
    run(
        service,
        "global",
        *answers_for(workers__max_concurrency="5", implementation__mode="plan-only"),
        "",
    )
    before = paths.global_file.read_text()

    result, conversation = run(service, *ENTER_THROUGH)
    assert result.startswith("Settings saved")
    assert "Maximum parallel workers [5]: " in conversation.prompts
    assert "Implementation mode [plan-only]: " in conversation.prompts
    assert paths.global_file.read_text() == before


def test_rerun_preserves_settings_startup_does_not_manage(
    service: ConfigService, paths: ConfigPaths
) -> None:
    paths.global_file.parent.mkdir(parents=True)
    paths.global_file.write_text(
        yaml.safe_dump(
            {
                "providers": {"vllm": {"type": "openai-compatible", "api_key_env": "VLLM_KEY"}},
                "routing": {"role_floors": {"implementer": "mid"}},
                "review": {"max_rounds": 5},
            }
        )
    )
    run(service, *ENTER_THROUGH)
    config = ConfigService(paths).resolve()
    assert config.providers["vllm"].api_key_env == "VLLM_KEY"
    assert config.routing.role_floors.implementer == "mid"
    assert config.review.max_rounds == 5


def test_project_target_writes_only_changed_values(
    service: ConfigService, paths: ConfigPaths
) -> None:
    result, conversation = run(
        service, "project", *answers_for(cost__monthly_limit_usd="5", git__branch_prefix="x/"), ""
    )
    assert result == f"Settings saved to {paths.project_file}."
    assert load(paths.project_file) == {
        "cost": {"monthly_limit_usd": 5.0},
        "git": {"branch_prefix": "x/"},
    }
    assert not paths.global_file.exists()
    summary = next(line for line in conversation.output if line.startswith("Summary"))
    assert "project config" in summary
    assert "→ project (was $20.00)" in summary
    assert "inherited" in summary


def test_project_is_the_default_target_once_it_has_config(
    service: ConfigService, paths: ConfigPaths
) -> None:
    run(service, "project", *answers_for(workers__max_concurrency="2"), "")
    _, conversation = run(service, *ENTER_THROUGH)
    assert conversation.prompts[0] == "Save to [project]: "
    assert "Maximum parallel workers [2]: " in conversation.prompts


def test_global_target_shows_global_values_and_flags_project_overrides(
    service: ConfigService, paths: ConfigPaths
) -> None:
    run(service, "project", *answers_for(workers__max_concurrency="2"), "")
    result, conversation = run(service, "global", *ENTER_THROUGH[1:])
    assert result.startswith("Settings saved")
    assert "Maximum parallel workers [3]: " in conversation.prompts
    assert "(overridden by project config)" in conversation.transcript
    assert ConfigService(paths).resolve(through=Layer.GLOBAL).workers.max_concurrency == 3
    assert ConfigService(paths).resolve().workers.max_concurrency == 2


def test_project_cost_cannot_exceed_global_and_is_asked_again(
    service: ConfigService, paths: ConfigPaths
) -> None:
    answers = answers_for(cost__monthly_limit_usd="50")
    index = [s.key for s in SETTINGS].index("cost.monthly_limit_usd")
    answers.insert(index + 1, "10")
    result, conversation = run(service, "project", *answers, "")
    assert result.startswith("Settings saved")
    assert "$50.00 is above the default limit of $20.00" in conversation.transcript
    assert load(paths.project_file) == {"cost": {"monthly_limit_usd": 10.0}}


def test_lowering_global_below_a_project_limit_is_rejected(
    service: ConfigService, paths: ConfigPaths
) -> None:
    run(service, "project", *answers_for(cost__per_task_limit_usd="0.8"), "")
    answers = answers_for(cost__per_task_limit_usd="0.5")
    index = [s.key for s in SETTINGS].index("cost.per_task_limit_usd")
    answers.insert(index + 1, "0.9")
    _, conversation = run(service, "global", *answers, "")
    assert "project config" in conversation.transcript
    assert "above the global limit of $0.50" in conversation.transcript
    assert ConfigService(paths).resolve().cost.per_task_limit_usd == 0.8


def budget_answers(*rounds: tuple[str, str, str]) -> list[str]:
    """Answers with the three budget prompts filled in once per round."""
    keys = [s.key for s in SETTINGS]
    start = keys.index("cost.monthly_limit_usd")
    answers = [""] * len(SETTINGS)
    answers[start : start + 3] = [answer for round in rounds for answer in round]
    return answers


def test_budget_limits_can_be_lowered_in_any_prompt_order(
    service: ConfigService, paths: ConfigPaths
) -> None:
    result, conversation = run(service, "global", *budget_answers(("0.5", "0.25", "0.1")), "")
    assert result.startswith("Settings saved")
    assert "cannot be above" not in conversation.transcript
    cost = ConfigService(paths).resolve().cost
    assert (cost.monthly_limit_usd, cost.per_task_limit_usd, cost.per_call_limit_usd) == (
        0.5,
        0.25,
        0.1,
    )


def test_contradictory_budget_limits_are_asked_again_with_answers_as_defaults(
    service: ConfigService, paths: ConfigPaths
) -> None:
    answers = budget_answers(("0.5", "", ""), ("", "0.4", ""))
    result, conversation = run(service, "global", *answers, "")
    assert result.startswith("Settings saved")
    assert (
        "  the per-task limit ($1.00) cannot be above the monthly limit ($0.50). "
        "Enter the budget limits again." in conversation.output
    )
    assert conversation.prompts.count("Monthly paid-API limit [$0.50]: ") == 1
    assert "Per-task paid-API limit [$1.00]: " in conversation.prompts
    cost = ConfigService(paths).resolve().cost
    assert (cost.monthly_limit_usd, cost.per_task_limit_usd) == (0.5, 0.4)


def test_project_budget_cannot_exceed_a_lowered_project_limit_in_session(
    service: ConfigService, paths: ConfigPaths
) -> None:
    run(service, "project", *budget_answers(("10", "", "")), "")
    service.set_session({"cost": {"monthly_limit_usd": 12}})
    with pytest.raises(ConfigError, match=r"above the project limit of \$10\.00"):
        service.resolve()


@pytest.mark.parametrize(
    ("key", "bad", "message"),
    [
        ("workers.max_concurrency", "many", "Enter a whole number."),
        ("workers.max_concurrency", "0", "must be at least 1"),
        ("cost.monthly_limit_usd", "-5", "must be at least 0"),
        ("cost.per_call_limit_usd", "cheap", "Enter an amount in USD"),
        ("implementation.mode", "yolo", "Choose one of: autopilot, checkpoint, plan-only."),
        ("review.before_merge_request", "maybe", "Enter yes or no."),
        ("access.preference", "cloud", "List one or more of: subscription, local, api."),
        ("access.preference", "api api", "must not repeat entries"),
        ("git.worktree_dir", "../elsewhere", "must be a path inside the repository"),
        ("git.branch_prefix", "bad prefix", "is not a valid Git branch prefix"),
    ],
)
def test_invalid_answers_are_explained_and_asked_again(
    service: ConfigService, paths: ConfigPaths, key: str, bad: str, message: str
) -> None:
    answers = [""] * len(SETTINGS)
    answers.insert([s.key for s in SETTINGS].index(key), bad)
    result, conversation = run(service, "global", *answers, "")
    assert result.startswith("Settings saved")
    assert any(message in line for line in conversation.output)
    assert ConfigService(paths).resolve() == Config()


def test_invalid_target_is_asked_again(service: ConfigService, paths: ConfigPaths) -> None:
    result, conversation = run(service, "galaxy", *ENTER_THROUGH)
    assert result.startswith("Settings saved")
    assert "  Enter global or project." in conversation.output


@pytest.mark.parametrize("decline", ["n", "no"])
def test_declining_the_summary_writes_nothing(
    service: ConfigService, paths: ConfigPaths, decline: str
) -> None:
    result, _ = run(service, *ENTER_THROUGH[:-1], decline)
    assert result == CANCELED
    assert not paths.global_file.exists()
    assert paths.project_root is not None and not (paths.project_root / ".agent-cli").exists()


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, EOFError])
@pytest.mark.parametrize("after", [0, 3, len(ENTER_THROUGH) - 1])
def test_canceling_midway_writes_nothing(
    service: ConfigService, paths: ConfigPaths, interrupt: type[BaseException], after: int
) -> None:
    result, _ = run(service, *ENTER_THROUGH[:after], interrupt)
    assert result == CANCELED
    assert not paths.global_file.exists()
    assert paths.project_root is not None and not (paths.project_root / ".agent-cli").exists()


def test_summary_lists_every_setting_and_target_before_confirming(
    service: ConfigService, paths: ConfigPaths
) -> None:
    _, conversation = run(service, *ENTER_THROUGH[:-1], "n")
    summary = conversation.output[-1]
    assert summary.startswith(
        f"Summary: changes are written to global config ({paths.global_file})"
    )
    for setting in SETTINGS:
        assert f"  {setting.label} " in summary
    assert summary.count("→ global") == len(SETTINGS)
    assert "git-ignored" in summary
    assert conversation.prompts[-1] == "Write these settings? [Y/n]: "


def test_advanced_settings_are_not_shown(service: ConfigService) -> None:
    _, conversation = run(service, *ENTER_THROUGH)
    transcript = conversation.transcript.lower()
    for word in ADVANCED:
        assert word not in transcript


def test_harness_and_provider_status_is_honest_placeholder(service: ConfigService) -> None:
    _, conversation = run(service, *ENTER_THROUGH)
    assert "detection and provider setup arrive in V1-04" in conversation.transcript
    assert "Configured harnesses: none" in conversation.transcript
    assert "Configured providers: none" in conversation.transcript


def test_harness_provider_status_hook_can_be_supplied(service: ConfigService) -> None:
    conversation = Conversation(*ENTER_THROUGH)
    seen: list[Config] = []

    def status(config: Config) -> list[str]:
        seen.append(config)
        return ["Detected harness: example"]

    GuidedSetup(service, conversation.read_line, conversation.output.append, status).run()
    assert seen == [Config()]
    assert "Detected harness: example" in conversation.output


def test_invalid_existing_config_stops_setup_without_writing(
    service: ConfigService, paths: ConfigPaths
) -> None:
    paths.global_file.parent.mkdir(parents=True)
    paths.global_file.write_text("workers:\n  max_parallel: 4\n")
    result, conversation = run(service, *ENTER_THROUGH)
    assert result.startswith("Cannot run setup: global config (")
    assert "workers.max_parallel: unknown key" in result
    assert conversation.prompts == []
    assert paths.global_file.read_text() == "workers:\n  max_parallel: 4\n"


def test_no_secret_values_are_written(service: ConfigService, paths: ConfigPaths) -> None:
    paths.global_file.parent.mkdir(parents=True)
    paths.global_file.write_text(
        yaml.safe_dump({"providers": {"p": {"type": "openai-compatible", "api_key_env": "P_KEY"}}})
    )
    run(service, *ENTER_THROUGH)
    text = paths.global_file.read_text()
    assert "api_key_env: P_KEY" in text
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        key = line.split(":", 1)[0].strip()
        assert key in {"api_key_env", "api_key_keyring"} or not any(
            word in key for word in ("key", "token", "secret", "password")
        )


def test_startup_command_runs_the_guided_flow(service: ConfigService, paths: ConfigPaths) -> None:
    conversation = Conversation(*ENTER_THROUGH)
    context = CommandContext(
        registry=build_registry(),
        config=service,
        read_line=conversation.read_line,
        write_line=conversation.output.append,
    )
    result = context.registry.dispatch("/startup", context)
    assert result == f"Settings saved to {paths.global_file}."
    assert conversation.output[0].startswith("Agent CLI setup")


def test_startup_command_without_config_service_explains() -> None:
    context = CommandContext(registry=build_registry())
    assert context.registry.dispatch("/startup", context).startswith("Configuration is unavailable")

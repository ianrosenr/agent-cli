from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from rich.console import RenderableType

from agent_cli.commands import CommandContext, build_registry
from agent_cli.config import Config, ConfigError, ConfigPaths, ConfigService, Layer
from agent_cli.config.schema import to_raw
from agent_cli.guided_setup import CANCELED, SETTINGS, GuidedSetup, Setting, lookup
from agent_cli.ui import plain

ENTER_THROUGH = [""] * (len(SETTINGS) + 2)  # target layer, every setting, confirmation
ADVANCED = ("role_floors", "orchestrator", "routing", "capability", "fallback", "classifier")


class Conversation:
    """Scripted answers for the setup prompts, recording everything shown."""

    def __init__(self, *answers: str | type[BaseException]) -> None:
        self.answers: Iterator[str | type[BaseException]] = iter(answers)
        self.prompts: list[str] = []
        self.output: list[str] = []

    def write_line(self, shown: RenderableType) -> None:
        """Record output as plain text, one entry per line."""
        self.output += plain(shown).splitlines() or [""]

    def read_line(self, prompt: str) -> str:
        self.prompts.append(prompt)
        answer = next(self.answers, EOFError)
        if isinstance(answer, str):
            return answer
        raise answer

    @property
    def transcript(self) -> str:
        return "\n".join(self.output + self.prompts)


def run(service: ConfigService, *answers: str | type[BaseException]) -> tuple[str, Conversation]:
    conversation = Conversation(*answers)
    result = GuidedSetup(service, conversation.read_line, conversation.write_line).run()
    return plain(result), conversation


def summary_of(conversation: Conversation) -> str:
    """The summary shown before the save confirmation, through the end of the output."""
    start = next(i for i, line in enumerate(conversation.output) if line.startswith("Summary"))
    return "\n".join(conversation.output[start:])


def hint(how: str, example: str, default: str = "") -> list[str]:
    """The aligned answer hint lines, as rendered."""
    lines = [f"  How to answer  {how}", f"  Example        {example}"]
    if default:
        lines.append(f"  Default        {default}  press Enter to keep it")
    return lines


def defaults_for(conversation: Conversation, label: str) -> list[str]:
    """Every default shown for the step or question titled ``label``, in order."""
    shown, current = [], None
    for line in conversation.output:
        header = re.match(r"^(?:Step \d+ of \d+  )?(.+)$", line)
        if line and not line.startswith(" ") and header:
            current = header[1]
        elif line.startswith("  Default        ") and current == label:
            shown.append(line.removeprefix("  Default        ").split("  ")[0])
    return shown


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
    assert result == f"Done  Settings saved to {paths.global_file}."
    assert ConfigService(paths).resolve() == Config()
    saved = load(paths.global_file)
    assert isinstance(saved, dict)
    assert saved["workers"] == {"max_concurrency": 3}
    assert saved["implementation"] == {"mode": "autopilot"}
    assert "routing" not in saved


def test_enter_through_outside_a_repository_writes_global(tmp_path: Path) -> None:
    paths = ConfigPaths(tmp_path / ".agent-cli" / "config.yaml", None)
    result, conversation = run(ConfigService(paths), *ENTER_THROUGH[1:])
    assert result == f"Done  Settings saved to {paths.global_file}."
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
    result, _ = run(service, "y", *answers, "y")
    assert result.startswith("Done  Settings saved")
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


def test_worker_concurrency_guidance_allows_eight(
    service: ConfigService, paths: ConfigPaths
) -> None:
    result, conversation = run(service, "y", *answers_for(workers__max_concurrency="8"), "")
    assert result.startswith("Done  Settings saved")
    assert ConfigService(paths).resolve().workers.max_concurrency == 8
    assert "Recommended: 2-4. Maximum: 16." in conversation.transcript
    assert "Type a whole number from 1 to 16." in conversation.transcript


def test_rerun_shows_current_values_as_defaults_and_keeps_them(
    service: ConfigService, paths: ConfigPaths
) -> None:
    run(
        service,
        "y",
        *answers_for(workers__max_concurrency="5", implementation__mode="plan-only"),
        "",
    )
    before = paths.global_file.read_text()

    result, conversation = run(service, *ENTER_THROUGH)
    assert result.startswith("Done  Settings saved")
    assert defaults_for(conversation, "Parallel workers") == ["5"]
    assert defaults_for(conversation, "Implementation mode") == ["plan-only"]
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
        service, "n", *answers_for(cost__monthly_limit_usd="5", git__branch_prefix="x/"), ""
    )
    assert result == f"Done  Settings saved to {paths.project_file}."
    assert load(paths.project_file) == {
        "cost": {"monthly_limit_usd": 5.0},
        "git": {"branch_prefix": "x/"},
    }
    assert not paths.global_file.exists()
    summary = summary_of(conversation)
    assert "project config" in summary
    assert "→ project (was $20.00)" in summary
    assert "inherited" in summary


def test_target_prompt_is_yes_for_global_and_no_for_project(
    service: ConfigService, paths: ConfigPaths
) -> None:
    run(service, "n", *answers_for(workers__max_concurrency="2"), "")
    _, conversation = run(service, "n", *ENTER_THROUGH[1:])
    assert conversation.prompts[0] == "Save to (y/n): "
    assert defaults_for(conversation, "Where to save") == ["global"]
    assert defaults_for(conversation, "Parallel workers") == ["2"]
    assert not paths.global_file.exists()


def test_enter_at_the_target_prompt_saves_globally(
    service: ConfigService, paths: ConfigPaths
) -> None:
    run(service, *ENTER_THROUGH)
    assert paths.global_file.exists()
    assert paths.project_file is not None and not paths.project_file.exists()


def test_global_target_shows_global_values_and_flags_project_overrides(
    service: ConfigService, paths: ConfigPaths
) -> None:
    run(service, "n", *answers_for(workers__max_concurrency="2"), "")
    result, conversation = run(service, "y", *ENTER_THROUGH[1:])
    assert result.startswith("Done  Settings saved")
    assert defaults_for(conversation, "Parallel workers") == ["3"]
    assert "(overridden by project config)" in conversation.transcript
    assert ConfigService(paths).resolve(through=Layer.GLOBAL).workers.max_concurrency == 3
    assert ConfigService(paths).resolve().workers.max_concurrency == 2


def test_project_cost_cannot_exceed_global_and_is_asked_again(
    service: ConfigService, paths: ConfigPaths
) -> None:
    answers = answers_for(cost__monthly_limit_usd="50")
    index = [s.key for s in SETTINGS].index("cost.monthly_limit_usd")
    answers.insert(index + 1, "10")
    result, conversation = run(service, "n", *answers, "")
    assert result.startswith("Done  Settings saved")
    assert "$50.00 is above the default limit of $20.00" in conversation.transcript
    assert load(paths.project_file) == {"cost": {"monthly_limit_usd": 10.0}}


def test_lowering_global_below_a_project_limit_is_rejected(
    service: ConfigService, paths: ConfigPaths
) -> None:
    run(service, "n", *answers_for(cost__per_task_limit_usd="0.8"), "")
    answers = answers_for(cost__per_task_limit_usd="0.5")
    index = [s.key for s in SETTINGS].index("cost.per_task_limit_usd")
    answers.insert(index + 1, "0.9")
    _, conversation = run(service, "y", *answers, "")
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
    result, conversation = run(service, "y", *budget_answers(("0.5", "0.25", "0.1")), "")
    assert result.startswith("Done  Settings saved")
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
    result, conversation = run(service, "y", *answers, "")
    assert result.startswith("Done  Settings saved")
    failure = conversation.output.index(
        "  Warning  Not allowed: the per-task limit ($1.00) cannot be above the monthly limit "
        "($0.50)."
    )
    assert conversation.output[failure + 1].strip() == "Please enter the spending limits again."
    assert defaults_for(conversation, "Monthly spending limit").count("$0.50") == 1
    assert "$1.00" in defaults_for(conversation, "Per-task spending limit")
    cost = ConfigService(paths).resolve().cost
    assert (cost.monthly_limit_usd, cost.per_task_limit_usd) == (0.5, 0.4)


def test_project_budget_cannot_exceed_a_lowered_project_limit_in_session(
    service: ConfigService, paths: ConfigPaths
) -> None:
    run(service, "n", *budget_answers(("10", "", "")), "")
    service.set_session({"cost": {"monthly_limit_usd": 12}})
    with pytest.raises(ConfigError, match=r"above the project limit of \$10\.00"):
        service.resolve()


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("access.preference", "cloud"),
        ("access.preference", "api api"),
        ("access.preference", "api, api"),
        ("access.preference", "local,"),
        ("cost.monthly_limit_usd", "-5"),
        ("cost.monthly_limit_usd", "twenty"),
        ("cost.per_task_limit_usd", "1.234"),
        ("cost.per_call_limit_usd", "1,000"),
        ("workers.max_concurrency", "many"),
        ("workers.max_concurrency", "0"),
        ("workers.max_concurrency", "17"),
        ("workers.max_concurrency", "2.5"),
        ("skills.paths", "/a,,/b"),
        ("implementation.mode", "yolo"),
        ("review.before_merge_request", "maybe"),
        ("review.before_merge_request", "true"),
        ("git.use_worktrees", "1"),
        ("git.worktree_dir", "../elsewhere"),
        ("git.worktree_dir", "/abs/path"),
        ("git.branch_prefix", "bad prefix"),
    ],
)
def test_answers_not_in_the_stated_format_are_asked_again(
    service: ConfigService, paths: ConfigPaths, key: str, bad: str
) -> None:
    index = [s.key for s in SETTINGS].index(key)
    answers = [""] * len(SETTINGS)
    answers.insert(index, bad)
    result, conversation = run(service, "y", *answers, "")
    assert result.startswith("Done  Settings saved")
    setting = SETTINGS[index]
    failure = conversation.output.index("  Warning  Format not followed, please try again.")
    note = f"  ({setting.example_note})" if setting.example_note else ""
    default = setting.format(lookup(to_raw(Config()), setting.key))
    assert conversation.output[failure + 1 : failure + 4] == hint(
        setting.how, setting.example + note, default
    )
    assert conversation.prompts.count(conversation.prompts[index + 1]) == 2
    assert ConfigService(paths).resolve() == Config()


@pytest.mark.parametrize("setting", SETTINGS, ids=[s.key for s in SETTINGS])
def test_every_example_is_a_working_answer(
    service: ConfigService, paths: ConfigPaths, setting: Setting
) -> None:
    for layer in ("y", "n"):
        answers = [""] * len(SETTINGS)
        answers[SETTINGS.index(setting)] = setting.example
        result, conversation = run(ConfigService(paths), layer, *answers, "")
        assert result.startswith("Done  Settings saved"), conversation.transcript
        assert "Format not followed" not in conversation.transcript
        assert "Not allowed" not in conversation.transcript


def test_every_step_shows_what_it_does_how_to_answer_and_an_example(
    service: ConfigService,
) -> None:
    _, conversation = run(service, *ENTER_THROUGH)
    for number, setting in enumerate(SETTINGS, start=1):
        header = conversation.output.index(f"Step {number} of {len(SETTINGS)}  {setting.label}")
        what = setting.what.splitlines()
        body = conversation.output[header + 1 : header + len(what) + 5]
        assert body[: len(what)] == [f"  {line.removeprefix('  ')}" for line in what]
        assert body[len(what)] == ""
        note = f"  ({setting.example_note})" if setting.example_note else ""
        default = setting.format(lookup(to_raw(Config()), setting.key))
        assert body[len(what) + 1 :] == hint(setting.how, setting.example + note, default)


def test_access_preference_explains_that_kinds_can_be_left_out(
    service: ConfigService, paths: ConfigPaths
) -> None:
    index = [s.key for s in SETTINGS].index("access.preference")
    answers = [""] * len(SETTINGS)
    answers[index] = "subscription, local"
    _, conversation = run(service, "y", *answers, "")
    assert "  You do not need all three: any kind you leave out is never used." in (
        conversation.transcript
    )
    assert (
        "  Example        subscription, local  "
        "(uses your plans first, then local models, and never paid APIs)" in conversation.output
    )
    assert ConfigService(paths).resolve().access.preference == ("subscription", "local")


@pytest.mark.parametrize("answer", ["local", "api", "subscription, local, api"])
def test_access_preference_accepts_one_two_or_three_kinds(
    service: ConfigService, paths: ConfigPaths, answer: str
) -> None:
    answers = [""] * len(SETTINGS)
    answers[[s.key for s in SETTINGS].index("access.preference")] = answer
    result, _ = run(service, "y", *answers, "")
    assert result.startswith("Done  Settings saved")
    assert ConfigService(paths).resolve().access.preference == tuple(answer.split(", "))


def test_setting_a_project_value_back_to_global_removes_the_override(
    service: ConfigService, paths: ConfigPaths
) -> None:
    run(service, "n", *answers_for(workers__max_concurrency="5", git__branch_prefix="x/"), "")
    _, conversation = run(service, "n", *answers_for(workers__max_concurrency="3"), "")
    assert load(paths.project_file) == {"git": {"branch_prefix": "x/"}}
    summary = summary_of(conversation)
    workers = next(line for line in summary.splitlines() if "Parallel workers" in line)
    assert "inherited from global (project override removed) (was 5)" in workers
    prefix = next(line for line in summary.splitlines() if "Branch prefix" in line)
    assert "→ project" in prefix


def test_unchanged_project_values_are_labeled_as_project_values(
    service: ConfigService, paths: ConfigPaths
) -> None:
    run(service, "n", *answers_for(implementation__mode="plan-only"), "")
    _, conversation = run(service, "n", *ENTER_THROUGH[1:])
    summary = summary_of(conversation)
    mode = next(line for line in summary.splitlines() if "Implementation mode" in line)
    assert mode.rstrip().endswith("→ project")
    workers = next(line for line in summary.splitlines() if "Parallel workers" in line)
    assert workers.rstrip().endswith("inherited from global")
    assert load(paths.project_file) == {"implementation": {"mode": "plan-only"}}


def test_setup_reads_config_files_once_however_many_retries(
    service: ConfigService, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_cli.config.service as service_module

    real = service_module.read_layer
    calls: list[Path] = []

    def counting(path: Path, source: str) -> object:
        calls.append(path)
        return real(path, source)

    monkeypatch.setattr(service_module, "read_layer", counting)
    run(service, *ENTER_THROUGH)
    quiet = len(calls)
    calls.clear()
    index = [s.key for s in SETTINGS].index("workers.max_concurrency")
    answers = [""] * len(SETTINGS)
    answers[index:index] = ["0", "-1", "x", "0"]
    run(service, "y", *answers, "")
    assert len(calls) == quiet


def test_rule_violations_explain_the_rule_instead_of_the_format(
    service: ConfigService,
) -> None:
    answers = budget_answers(("50", "", ""))
    index = [s.key for s in SETTINGS].index("cost.monthly_limit_usd")
    answers.insert(index + 1, "")
    _, conversation = run(service, "n", *answers, "")
    assert (
        "  Warning  Not allowed: $50.00 is above the default limit of $20.00; more-specific "
        "config may only lower cost limits." in conversation.output
    )
    assert "Format not followed" not in conversation.transcript


def test_invalid_target_is_asked_again(service: ConfigService, paths: ConfigPaths) -> None:
    result, conversation = run(service, "galaxy", *ENTER_THROUGH)
    assert result.startswith("Done  Settings saved")
    assert conversation.prompts[0] == "Save to (y/n): "
    assert "  Warning  Format not followed, please try again." in conversation.output


@pytest.mark.parametrize("decline", ["n", "no"])
def test_declining_the_summary_writes_nothing(
    service: ConfigService, paths: ConfigPaths, decline: str
) -> None:
    result, _ = run(service, *ENTER_THROUGH[:-1], decline)
    assert result == f"Note  {CANCELED}"
    assert not paths.global_file.exists()
    assert paths.project_root is not None and not (paths.project_root / ".agent-cli").exists()


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, EOFError])
@pytest.mark.parametrize("after", [0, 3, len(ENTER_THROUGH) - 1])
def test_canceling_midway_writes_nothing(
    service: ConfigService, paths: ConfigPaths, interrupt: type[BaseException], after: int
) -> None:
    result, _ = run(service, *ENTER_THROUGH[:after], interrupt)
    assert result == f"Note  {CANCELED}"
    assert not paths.global_file.exists()
    assert paths.project_root is not None and not (paths.project_root / ".agent-cli").exists()


def test_summary_lists_every_setting_and_target_before_confirming(
    service: ConfigService, paths: ConfigPaths
) -> None:
    _, conversation = run(service, *ENTER_THROUGH[:-1], "n")
    summary = summary_of(conversation)
    assert conversation.output[-3:] == hint(
        "Type y to save these settings or n to cancel.", "y", "yes"
    )
    assert summary.startswith(
        f"Summary  changes are written to global config ({paths.global_file})"
    )
    for setting in SETTINGS:
        assert f"  {setting.label} " in summary
    assert summary.count("→ global") == len(SETTINGS)
    assert "git-ignored" not in summary
    assert conversation.prompts[-1] == "Save these settings? (y/n): "


def test_advanced_settings_are_not_shown(service: ConfigService) -> None:
    _, conversation = run(service, *ENTER_THROUGH)
    transcript = conversation.transcript.lower()
    for word in ADVANCED:
        assert word not in transcript


def test_harness_and_provider_status_without_adapters(service: ConfigService) -> None:
    _, conversation = run(service, *ENTER_THROUGH)
    assert "Harnesses  none configured or detected" in conversation.transcript
    assert "Providers  none configured or detected" in conversation.transcript


def test_harness_provider_status_hook_can_be_supplied(service: ConfigService) -> None:
    conversation = Conversation(*ENTER_THROUGH)
    seen: list[Config] = []

    def status(config: Config) -> str:
        seen.append(config)
        return "Detected harness: example"

    GuidedSetup(service, conversation.read_line, conversation.write_line, status).run()
    assert seen == [Config()]
    assert "Detected harness: example" in conversation.output


def test_invalid_existing_config_stops_setup_without_writing(
    service: ConfigService, paths: ConfigPaths
) -> None:
    paths.global_file.parent.mkdir(parents=True)
    paths.global_file.write_text("workers:\n  max_parallel: 4\n")
    result, conversation = run(service, *ENTER_THROUGH)
    assert result.startswith("Error  Cannot run setup.\n")
    assert "workers.max_parallel: unknown key" in " ".join(result.split())
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
        write_line=conversation.write_line,
    )
    result = plain(context.registry.dispatch("/startup", context))
    assert result == f"Done  Settings saved to {paths.global_file}."
    assert conversation.output[0].startswith("Agent CLI setup")


def test_startup_command_without_config_service_explains() -> None:
    context = CommandContext(registry=build_registry())
    output = plain(context.registry.dispatch("/startup", context))
    assert output.startswith("Error  Configuration is unavailable")

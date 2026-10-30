"""The guided ``/startup`` setup flow.

This module owns the conversation only. Loading, validation, and persistence belong to
``ConfigService``; the flow asks for values, checks each one against the service, and writes
nothing until the user confirms the summary.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_cli.adapters import AdapterRegistry, adapter_status
from agent_cli.config import Config, ConfigError, ConfigService, Layer, RawConfig
from agent_cli.config.model import MAX_WORKER_CONCURRENCY
from agent_cli.config.schema import merge, to_raw
from agent_cli.config.service import CostLimitError, CostOrderError
from agent_cli.terms import ACCESS_KINDS, IMPLEMENTATION_MODES

type ReadLine = Callable[[str], str]
type WriteLine = Callable[[str], None]
type HarnessProviderStatus = Callable[[Config], Sequence[str]]
"""Lines describing configured and detected harnesses and providers for the setup screen."""

INTRO = """\
Agent CLI setup

How this works:
  - Each step explains one setting and shows exactly what to type.
  - The current value is shown in [brackets]. Press Enter to keep it.
  - Press Ctrl-C at any time to cancel. Nothing is saved until you confirm at the end."""
CANCELED = "Setup canceled. Nothing was written."
FORMAT_NOT_FOLLOWED = "Format not followed, please try again."
YES = ("y", "yes")
NO = ("n", "no")
NONE = "none"
MONEY = re.compile(r"^\$?(\d+(\.\d{1,2})?|\.\d{1,2})$")
WHOLE_NUMBER = re.compile(r"^\d+$")
YES_NO = "Type y for yes or n for no."


class InvalidAnswer(ValueError):
    """The answer does not follow the format the step asked for."""


def parse_money(text: str) -> float | None:
    if text.lower() == NONE:
        return None
    if not MONEY.match(text):
        raise InvalidAnswer
    return round(float(text.removeprefix("$")), 2)


def format_money(value: Any) -> str:
    return "none" if value is None else f"${value:.2f}"


def parse_workers(text: str) -> int:
    if not WHOLE_NUMBER.match(text):
        raise InvalidAnswer
    return int(text)


def parse_bool(text: str) -> bool:
    if text.lower() in YES:
        return True
    if text.lower() in NO:
        return False
    raise InvalidAnswer


def format_bool(value: Any) -> str:
    return "yes" if value else "no"


def parse_access(text: str) -> list[str]:
    kinds = [item.strip() for item in text.lower().split(",")]
    if not all(kinds):
        raise InvalidAnswer
    return kinds


def parse_paths(text: str) -> list[str]:
    if text.lower() == NONE:
        return []
    paths = [item.strip() for item in text.split(",")]
    if not all(paths):
        raise InvalidAnswer
    return paths


def format_list(value: Any) -> str:
    return ", ".join(value) if value else "none"


def parse_mode(text: str) -> str:
    return text.lower()


def parse_text(text: str) -> str:
    return text


@dataclass(frozen=True, slots=True)
class Setting:
    key: str
    """Dotted config key, e.g. ``cost.monthly_limit_usd``."""
    label: str
    what: str
    """One or two lines saying what the setting does."""
    how: str
    """Exactly what to type. Shown with the step, and again when the format is not followed."""
    example: str
    """An answer that works, shown with ``how``."""
    parse: Callable[[str], Any]
    """Turns an answer into a value; raises ``InvalidAnswer`` if it does not match ``how``."""
    format: Callable[[Any], str] = str
    yes_no: bool = False
    example_note: str = ""
    """What the example answer means, when that is not obvious."""

    @property
    def example_line(self) -> str:
        note = f"  ({self.example_note})" if self.example_note else ""
        return f"Example: {self.example}{note}"


BUDGET_KEYS = ("cost.monthly_limit_usd", "cost.per_task_limit_usd", "cost.per_call_limit_usd")
"""Asked consecutively, in this order, and checked against each other as a group."""

MONEY_HOW = 'Type a dollar amount, or type "none" for no limit.'

SETTINGS: tuple[Setting, ...] = (
    Setting(
        "access.preference",
        "Access preference",
        "Which kinds of model access Agent CLI may use, in the order to try them.\n"
        "  subscription = your existing plans, local = models on your machine, api = paid APIs.\n"
        "  You do not need all three: any kind you leave out is never used.",
        f"Type one, two, or all three of {', '.join(ACCESS_KINDS)}, separated by commas, "
        "most preferred first.",
        "subscription, local",
        parse_access,
        format_list,
        example_note="uses your plans first, then local models, and never paid APIs",
    ),
    Setting(
        "cost.monthly_limit_usd",
        "Monthly spending limit",
        "The most to spend on paid APIs in a month. Subscription and local use are free.",
        MONEY_HOW,
        "15",
        parse_money,
        format_money,
    ),
    Setting(
        "cost.per_task_limit_usd",
        "Per-task spending limit",
        "The most one task may spend on paid APIs. Must not be above the monthly limit.",
        MONEY_HOW,
        "0.75",
        parse_money,
        format_money,
    ),
    Setting(
        "cost.per_call_limit_usd",
        "Per-call spending limit",
        "The most a single paid API call may cost. Must not be above the per-task limit.",
        MONEY_HOW,
        "0.10",
        parse_money,
        format_money,
    ),
    Setting(
        "workers.max_concurrency",
        "Parallel workers",
        "How many workers may run at the same time. Recommended: 2–4. "
        f"Maximum: {MAX_WORKER_CONCURRENCY}.",
        f"Type a whole number from 1 to {MAX_WORKER_CONCURRENCY}.",
        "2",
        parse_workers,
    ),
    Setting(
        "skills.paths",
        "Extra skill folders",
        "Skills always load from the built-in skills, ~/.agent-cli/skills, and\n"
        "  <repo>/.agent-cli/skills. Add more folders here if you keep skills elsewhere.",
        'Type folder paths separated by commas, or type "none" for no extra folders.',
        "~/my-skills, /opt/team-skills",
        parse_paths,
        format_list,
    ),
    Setting(
        "implementation.mode",
        "Implementation mode",
        "How workers apply changes once a plan is approved.\n"
        "  autopilot  = make every approved change without asking each time\n"
        "  checkpoint = ask before each batch of changes\n"
        "  plan-only  = change no files; explain the plan instead",
        f"Type {', '.join(IMPLEMENTATION_MODES[:-1])}, or {IMPLEMENTATION_MODES[-1]}.",
        "checkpoint",
        parse_mode,
    ),
    Setting(
        "review.before_merge_request",
        "Review before merge requests",
        "Require a code review before any merge request is opened.",
        YES_NO,
        "y",
        parse_bool,
        format_bool,
        yes_no=True,
    ),
    Setting(
        "git.use_worktrees",
        "Use Git worktrees",
        "Give each worker its own copy of the repository (a Git worktree) so workers\n"
        "  never overwrite each other's changes.",
        YES_NO,
        "y",
        parse_bool,
        format_bool,
        yes_no=True,
    ),
    Setting(
        "git.worktree_dir",
        "Worktree folder",
        "Where worker worktrees are created, relative to the repository root.",
        "Type a folder path inside the repository. Do not start it with / or use ..",
        ".agent-cli/worktrees",
        parse_text,
    ),
    Setting(
        "git.branch_prefix",
        "Branch prefix",
        "Text added to the start of every branch name workers create.",
        "Type a prefix with no spaces.",
        "bot/",
        parse_text,
    ),
)


def lookup(data: Mapping[str, Any], key: str) -> Any:
    value: Any = data
    for part in key.split("."):
        value = value[part]
    return value


def contains(data: Mapping[str, Any], key: str) -> bool:
    try:
        lookup(data, key)
    except (KeyError, TypeError):
        return False
    return True


def nested(key: str, value: Any) -> RawConfig:
    head, _, rest = key.partition(".")
    return {head: nested(rest, value) if rest else value}


def propose(layer_data: RawConfig, answers: Mapping[str, Any]) -> RawConfig:
    """``layer_data`` with every answer so far applied."""
    for key, value in answers.items():
        layer_data = merge(layer_data, nested(key, value))
    return layer_data


def default_status(config: Config) -> list[str]:
    """Configured harnesses and providers, with no adapters to report runtime readiness."""
    return adapter_status(AdapterRegistry(), config)


@dataclass(frozen=True, slots=True)
class Problem:
    message: str
    format: bool
    """The answer is malformed, as opposed to well-formed but against a rule."""


@dataclass(slots=True)
class GuidedSetup:
    service: ConfigService
    read_line: ReadLine
    write_line: WriteLine
    harness_provider_status: HarnessProviderStatus = default_status
    files: dict[Layer, RawConfig] = field(default_factory=dict)
    """Global and project config as read once at the start, so every step checks the same data."""

    def run(self) -> str:
        """Run the whole flow and return the closing message. Never writes unless confirmed."""
        try:
            return self.converse()
        except (KeyboardInterrupt, EOFError):
            return CANCELED

    def converse(self) -> str:
        try:
            self.files = {
                layer: self.service.read(layer) for layer in (Layer.GLOBAL, Layer.PROJECT)
            }
            self.resolve(Layer.PROJECT)
        except ConfigError as error:
            return f"Cannot run setup: {error}\nFix or remove that file, then run /startup again."

        self.write_line(INTRO)
        target = self.ask_target()
        current = to_raw(self.resolve(target))
        inherited = to_raw(self.resolve(Layer.GLOBAL)) if target is Layer.PROJECT else current
        layer_data = self.files[target]

        self.write_line("")
        self.write_line("\n".join(self.harness_provider_status(self.resolve(Layer.PROJECT))))

        answers: RawConfig = {}
        index = 0
        while index < len(SETTINGS):
            setting = SETTINGS[index]
            self.write_line("")
            self.write_line(f"Step {index + 1} of {len(SETTINGS)}: {setting.label}")
            self.write_line(f"  {setting.what}")
            self.write_line(f"  How to answer: {setting.how}")
            self.write_line(f"  {setting.example_line}")
            default = answers.get(setting.key, lookup(current, setting.key))
            answers[setting.key] = self.ask(setting, default, target, layer_data, answers)
            index += 1
            if setting.key == BUDGET_KEYS[-1]:
                # Budget limits must agree with each other, which can only be judged once all
                # of them are answered. On a conflict, ask them again with the answers as defaults.
                problem = self.check(target, propose(layer_data, answers), key=None)
                if problem is not None:
                    self.write_line(
                        f"  Not allowed: {problem.message}. Please enter the spending limits again."
                    )
                    index = [s.key for s in SETTINGS].index(BUDGET_KEYS[0])

        changes, removals = self.plan(target, inherited, layer_data, answers)
        self.write_line("")
        self.write_line(self.summary(target, current, inherited, answers, changes, removals))
        self.write_line("How to answer: type y to save these settings or n to cancel.")
        self.write_line("Example: y")
        if not self.confirm("Save these settings?"):
            return CANCELED
        try:
            path = self.service.save(target, changes, removals)
        except ConfigError as error:
            return f"Could not save settings: {error}"
        return f"Settings saved to {path}."

    def resolve(self, through: Layer, proposal: tuple[Layer, RawConfig] | None = None) -> Config:
        """Resolve from the files read at the start, optionally with one layer replaced."""
        replace = dict(self.files)
        if proposal is not None:
            replace[proposal[0]] = proposal[1]
        return self.service.resolve(through=through, replace=replace)

    def ask_target(self) -> Layer:
        project = self.service.paths.project_file
        if project is None:
            self.write_line(
                f"\nNot inside a Git repository; settings go to {self.service.paths.global_file}."
            )
            return Layer.GLOBAL
        self.write_line(
            "\nWhere to save\n"
            f"  global   {self.service.paths.global_file}  (all projects)\n"
            f"  project  {project}  (this repository only; spending limits may only be lower)\n"
            "  How to answer: type y for global or n for this project. Press Enter for global.\n"
            "  Example: y"
        )
        while True:
            answer = self.read_line("Save to [global] (y/n): ").strip()
            try:
                return Layer.GLOBAL if not answer or parse_bool(answer) else Layer.PROJECT
            except InvalidAnswer:
                self.format_not_followed("Type y for global or n for this project.", "Example: y")

    def ask(
        self,
        setting: Setting,
        default: Any,
        target: Layer,
        layer_data: RawConfig,
        answers: RawConfig,
    ) -> Any:
        choices = " (y/n)" if setting.yes_no else ""
        while True:
            prompt = f"{setting.label} [{setting.format(default)}]{choices}: "
            answer = self.read_line(prompt).strip()
            try:
                value = setting.parse(answer) if answer else default
            except InvalidAnswer:
                self.format_not_followed(setting.how, setting.example_line)
                continue
            proposed = propose(layer_data, {**answers, setting.key: value})
            problem = self.check(
                target, proposed, key=setting.key, defer_budget_order=setting.key in BUDGET_KEYS
            )
            if problem is None:
                return value
            if problem.format:
                self.format_not_followed(setting.how, setting.example_line)
            else:
                self.write_line(f"  Not allowed: {problem.message}. Please try again.")

    def format_not_followed(self, how: str, example_line: str) -> None:
        self.write_line(f"  {FORMAT_NOT_FOLLOWED}")
        self.write_line(f"  How to answer: {how}")
        self.write_line(f"  {example_line}")

    def check(
        self,
        target: Layer,
        layer_data: RawConfig,
        *,
        key: str | None,
        defer_budget_order: bool = False,
    ) -> Problem | None:
        """Validate a proposed layer against every other layer; return the problem, if any.

        A schema error on ``key`` itself means the answer is malformed. Spending-rule errors, and
        errors in other layers, are reported as rules. With ``defer_budget_order``, contradictory
        budget limits are allowed for now, because the user may not have reached the other one.
        """
        try:
            self.resolve(Layer.PROJECT, (target, layer_data))
        except ConfigError as error:
            if isinstance(error, CostOrderError) and defer_budget_order:
                return None
            own = error.source == self.service.source(target)
            message = error.problem if own else str(error)
            malformed = (
                own
                and not isinstance(error, CostLimitError)
                and key is not None
                and error.key is not None
                and (error.key == key or error.key.startswith((f"{key}[", f"{key}.")))
            )
            return Problem(message, format=malformed)
        return None

    def plan(
        self, target: Layer, inherited: RawConfig, layer_data: RawConfig, answers: RawConfig
    ) -> tuple[RawConfig, tuple[str, ...]]:
        """What to write and what to remove.

        Global setup records every setting. Project setup records only values that differ from
        what the project would inherit from global config, and removes project overrides that
        were set back to the inherited value, so the project keeps following global changes.
        """
        changes: RawConfig = {}
        removals: list[str] = []
        for key, value in answers.items():
            if target is Layer.GLOBAL or value != lookup(inherited, key):
                changes = merge(changes, nested(key, value))
            elif contains(layer_data, key):
                removals.append(key)
        return changes, tuple(removals)

    def summary(
        self,
        target: Layer,
        current: RawConfig,
        inherited: RawConfig,
        answers: RawConfig,
        changes: RawConfig,
        removals: tuple[str, ...],
    ) -> str:
        overridden = self.files[Layer.PROJECT] if target is Layer.GLOBAL else {}
        rows = []
        for setting in SETTINGS:
            value = answers[setting.key]
            if contains(changes, setting.key):
                note = f"→ {target.value}"
            elif setting.key in removals:
                note = "inherited from global (project override removed)"
            else:
                note = "inherited from global"
            if value != lookup(current, setting.key):
                note += f" (was {setting.format(lookup(current, setting.key))})"
            if contains(overridden, setting.key):
                note += " (overridden by project config)"
            rows.append((setting.label, setting.format(value), note))

        label_width = max(len(label) for label, _, _ in rows)
        value_width = max(len(value) for _, value, _ in rows)
        lines = [f"Summary: changes are written to {self.service.source(target)}.", ""]
        lines += [
            f"  {label:<{label_width}}  {value:<{value_width}}  {note}"
            for label, value, note in rows
        ]
        if target is Layer.PROJECT:
            lines += ["", "Project runtime directories (state, worktrees) will be git-ignored."]
        return "\n".join(lines)

    def confirm(self, question: str) -> bool:
        while True:
            answer = self.read_line(f"{question} [yes] (y/n): ").strip()
            if not answer:
                return True
            try:
                return parse_bool(answer)
            except InvalidAnswer:
                self.format_not_followed(YES_NO + " Press Enter for yes.", "Example: y")

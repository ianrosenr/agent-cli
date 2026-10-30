"""The guided ``/startup`` setup flow.

This module owns the conversation only. Loading, validation, and persistence belong to
``ConfigService``; the flow asks for values, checks each one against the service, and writes
nothing until the user confirms the summary.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from agent_cli.config import Config, ConfigError, ConfigService, Layer, RawConfig
from agent_cli.config.model import ACCESS_KINDS, IMPLEMENTATION_MODES
from agent_cli.config.schema import BRANCH_PREFIX, merge, to_raw
from agent_cli.config.service import CostOrderError

type ReadLine = Callable[[str], str]
type WriteLine = Callable[[str], None]
type HarnessProviderStatus = Callable[[Config], Sequence[str]]
"""Hook for V1-04: lines describing detected harnesses and providers for the setup screen."""

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
    if not WHOLE_NUMBER.match(text) or int(text) < 1:
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
    if any(kind not in ACCESS_KINDS for kind in kinds) or len(set(kinds)) != len(kinds):
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
    if text.lower() not in IMPLEMENTATION_MODES:
        raise InvalidAnswer
    return text.lower()


def parse_worktree_dir(text: str) -> str:
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or "\x00" in text:
        raise InvalidAnswer
    return text


def parse_branch_prefix(text: str) -> str:
    if not BRANCH_PREFIX.match(text):
        raise InvalidAnswer
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


BUDGET_KEYS = ("cost.monthly_limit_usd", "cost.per_task_limit_usd", "cost.per_call_limit_usd")
"""Asked consecutively, in this order, and checked against each other as a group."""

MONEY_HOW = 'Type a dollar amount, or type "none" for no limit.'

SETTINGS: tuple[Setting, ...] = (
    Setting(
        "access.preference",
        "Access preference",
        "Which kinds of model access to use, in the order to try them.\n"
        "  subscription = your existing plans, local = models on your machine, api = paid APIs.",
        "Type one or more of subscription, local, api, separated by commas, most preferred first.",
        "local, subscription",
        parse_access,
        format_list,
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
        "How many workers may run at the same time.",
        "Type a whole number, 1 or more.",
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
        "Type autopilot, checkpoint, or plan-only.",
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
        parse_worktree_dir,
    ),
    Setting(
        "git.branch_prefix",
        "Branch prefix",
        "Text added to the start of every branch name workers create.",
        "Type a prefix with no spaces.",
        "bot/",
        parse_branch_prefix,
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
    def names(entries: Mapping[str, Any]) -> str:
        return ", ".join(sorted(entries)) or "none"

    return [
        "Harnesses and providers: detection and provider setup arrive in V1-04.",
        f"  Configured harnesses: {names(config.harnesses)}",
        f"  Configured providers: {names(config.providers)}",
    ]


@dataclass(slots=True)
class GuidedSetup:
    service: ConfigService
    read_line: ReadLine
    write_line: WriteLine
    harness_provider_status: HarnessProviderStatus = default_status

    def run(self) -> str:
        """Run the whole flow and return the closing message. Never writes unless confirmed."""
        try:
            return self.converse()
        except (KeyboardInterrupt, EOFError):
            return CANCELED

    def converse(self) -> str:
        try:
            self.service.resolve(through=Layer.PROJECT)
        except ConfigError as error:
            return f"Cannot run setup: {error}\nFix or remove that file, then run /startup again."

        self.write_line(INTRO)
        target = self.ask_target()
        base = to_raw(self.service.resolve(through=target))
        layer_data = self.service.read(target)

        self.write_line("")
        self.write_line("\n".join(self.harness_provider_status(self.service.resolve())))

        answers: RawConfig = {}
        index = 0
        while index < len(SETTINGS):
            setting = SETTINGS[index]
            self.write_line("")
            self.write_line(f"Step {index + 1} of {len(SETTINGS)}: {setting.label}")
            self.write_line(f"  {setting.what}")
            self.write_line(f"  How to answer: {setting.how}")
            self.write_line(f"  Example: {setting.example}")
            current = answers.get(setting.key, lookup(base, setting.key))
            answers[setting.key] = self.ask(setting, current, target, layer_data, answers)
            index += 1
            if setting.key == BUDGET_KEYS[-1]:
                # Budget limits must agree with each other, which can only be judged once all
                # of them are answered. On a conflict, ask them again with the answers as defaults.
                problem = self.check(target, propose(layer_data, answers))
                if problem is not None:
                    self.write_line(
                        f"  Not allowed: {problem}. Please enter the spending limits again."
                    )
                    index = [s.key for s in SETTINGS].index(BUDGET_KEYS[0])

        changes = self.changes(target, base, answers)
        self.write_line("")
        self.write_line(self.summary(target, base, answers, changes))
        self.write_line("How to answer: type y to save these settings or n to cancel.")
        self.write_line("Example: y")
        if not self.confirm("Save these settings?"):
            return CANCELED
        try:
            path = self.service.save(target, changes)
        except ConfigError as error:
            return f"Could not save settings: {error}"
        return f"Settings saved to {path}."

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
                self.format_not_followed("Type y for global or n for this project.", "y")

    def ask(
        self,
        setting: Setting,
        current: Any,
        target: Layer,
        layer_data: RawConfig,
        answers: RawConfig,
    ) -> Any:
        choices = " (y/n)" if setting.yes_no else ""
        while True:
            prompt = f"{setting.label} [{setting.format(current)}]{choices}: "
            answer = self.read_line(prompt).strip()
            try:
                value = setting.parse(answer) if answer else current
            except InvalidAnswer:
                self.format_not_followed(setting.how, setting.example)
                continue
            proposed = propose(layer_data, {**answers, setting.key: value})
            problem = self.check(target, proposed, defer_budget_order=setting.key in BUDGET_KEYS)
            if problem is None:
                return value
            self.write_line(f"  Not allowed: {problem}. Please try again.")

    def format_not_followed(self, how: str, example: str) -> None:
        self.write_line(f"  {FORMAT_NOT_FOLLOWED}")
        self.write_line(f"  How to answer: {how}")
        self.write_line(f"  Example: {example}")

    def check(
        self, target: Layer, layer_data: RawConfig, *, defer_budget_order: bool = False
    ) -> str | None:
        """Validate a proposed layer against every other layer; return the problem, if any.

        With ``defer_budget_order``, budget limits that contradict each other are allowed for
        now, because the user may not have reached the other limit yet.
        """
        try:
            self.service.resolve(through=Layer.PROJECT, replace={target: layer_data})
        except CostOrderError as error:
            if defer_budget_order:
                return None
            return error.problem if error.source == self.service.source(target) else str(error)
        except ConfigError as error:
            return error.problem if error.source == self.service.source(target) else str(error)
        return None

    def changes(self, target: Layer, base: RawConfig, answers: RawConfig) -> RawConfig:
        """Global setup records every setting; project setup records only what differs, so the
        project keeps inheriting everything else from global config."""
        changes: RawConfig = {}
        for key, value in answers.items():
            if target is Layer.GLOBAL or value != lookup(base, key):
                changes = merge(changes, nested(key, value))
        return changes

    def summary(
        self, target: Layer, base: RawConfig, answers: RawConfig, changes: RawConfig
    ) -> str:
        project_data = self.service.read(Layer.PROJECT) if target is Layer.GLOBAL else {}
        rows = []
        for setting in SETTINGS:
            value = answers[setting.key]
            note = f"→ {target.value}" if contains(changes, setting.key) else "inherited"
            if value != lookup(base, setting.key):
                note += f" (was {setting.format(lookup(base, setting.key))})"
            if contains(project_data, setting.key):
                note += " (overridden by project config)"
            rows.append((setting.label, setting.format(value), note))

        label_width = max(len(label) for label, _, _ in rows)
        value_width = max(len(value) for _, value, _ in rows)
        lines = [f"Summary: changes are written to {self.service.source(target)}.", ""]
        lines += [
            f"  {label:<{label_width}}  {value:<{value_width}}  {note}"
            for label, value, note in rows
        ]
        if self.service.paths.project_root is not None:
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
                self.format_not_followed(YES_NO + " Press Enter for yes.", "y")

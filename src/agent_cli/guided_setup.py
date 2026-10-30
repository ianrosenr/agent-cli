"""The guided ``/startup`` setup flow.

This module owns the conversation only. Loading, validation, and persistence belong to
``ConfigService``; the flow asks for values, checks each one against the service, and writes
nothing until the user confirms the summary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent_cli.config import Config, ConfigError, ConfigService, Layer, RawConfig
from agent_cli.config.model import ACCESS_KINDS, IMPLEMENTATION_MODES
from agent_cli.config.schema import merge, to_raw

type ReadLine = Callable[[str], str]
type WriteLine = Callable[[str], None]
type HarnessProviderStatus = Callable[[Config], Sequence[str]]
"""Hook for V1-04: lines describing detected harnesses and providers for the setup screen."""

INTRO = (
    "Agent CLI setup\n\nPress Enter to keep the value in [brackets]. Ctrl-C cancels without saving."
)
CANCELED = "Setup canceled. Nothing was written."
YES = ("y", "yes", "true", "on")
NO = ("n", "no", "false", "off")
NONE = ("none", "no limit", "null", "-")


class InvalidAnswer(ValueError):
    pass


def parse_money(text: str) -> float | None:
    if text.lower() in NONE:
        return None
    try:
        value = float(text.removeprefix("$").replace(",", ""))
    except ValueError:
        raise InvalidAnswer("Enter an amount in USD, e.g. 20 or 0.30, or 'none'.") from None
    return round(value, 2)


def format_money(value: Any) -> str:
    return "no limit" if value is None else f"${value:.2f}"


def parse_int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise InvalidAnswer("Enter a whole number.") from None


def parse_bool(text: str) -> bool:
    if text.lower() in YES:
        return True
    if text.lower() in NO:
        return False
    raise InvalidAnswer("Enter yes or no.")


def format_bool(value: Any) -> str:
    return "yes" if value else "no"


def split_list(text: str) -> list[str]:
    return [item.strip() for item in text.replace(",", " ").split() if item.strip()]


def parse_access(text: str) -> list[str]:
    kinds = split_list(text.lower())
    unknown = [kind for kind in kinds if kind not in ACCESS_KINDS]
    if unknown or not kinds:
        raise InvalidAnswer(f"List one or more of: {', '.join(ACCESS_KINDS)}.")
    return kinds


def parse_paths(text: str) -> list[str]:
    if text.lower() in NONE:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def format_list(value: Any) -> str:
    return ", ".join(value) if value else "none"


def parse_mode(text: str) -> str:
    if text.lower() not in IMPLEMENTATION_MODES:
        raise InvalidAnswer(f"Choose one of: {', '.join(IMPLEMENTATION_MODES)}.")
    return text.lower()


@dataclass(frozen=True, slots=True)
class Setting:
    key: str
    """Dotted config key, e.g. ``cost.monthly_limit_usd``."""
    label: str
    parse: Callable[[str], Any]
    format: Callable[[Any], str] = str
    intro: str | None = None


SKILL_LOCATIONS = (
    "Skills load from the built-in skills, ~/.agent-cli/skills, and <repo>/.agent-cli/skills.\n"
    "Add extra skill directories below, separated by commas."
)

SETTINGS: tuple[Setting, ...] = (
    Setting(
        "access.preference",
        "Access preference",
        parse_access,
        format_list,
        intro="Access: kinds of model access to use, most preferred first.",
    ),
    Setting(
        "cost.monthly_limit_usd",
        "Monthly paid-API limit",
        parse_money,
        format_money,
        intro="Budgets: hard limits on paid API spend. Subscription and local use are not counted.",
    ),
    Setting("cost.per_task_limit_usd", "Per-task paid-API limit", parse_money, format_money),
    Setting("cost.per_call_limit_usd", "Per-call paid-API limit", parse_money, format_money),
    Setting("workers.max_concurrency", "Maximum parallel workers", parse_int),
    Setting(
        "skills.paths", "Extra skill directories", parse_paths, format_list, intro=SKILL_LOCATIONS
    ),
    Setting(
        "implementation.mode",
        "Implementation mode",
        parse_mode,
        intro=f"Implementation: how workers apply changes ({', '.join(IMPLEMENTATION_MODES)}).",
    ),
    Setting(
        "review.before_merge_request",
        "Require review before merge requests",
        parse_bool,
        format_bool,
    ),
    Setting(
        "git.use_worktrees",
        "Use Git worktrees for workers",
        parse_bool,
        format_bool,
        intro="Git: workers edit isolated worktrees on their own branches.",
    ),
    Setting("git.worktree_dir", "Worktree directory", str.strip),
    Setting("git.branch_prefix", "Branch prefix", str.strip),
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
        for setting in SETTINGS:
            if setting.intro:
                self.write_line("")
                self.write_line(setting.intro)
            answers[setting.key] = self.ask(setting, lookup(base, setting.key), target, layer_data)

        changes = self.changes(target, base, answers)
        self.write_line("")
        self.write_line(self.summary(target, base, answers, changes))
        if not self.confirm("Write these settings?"):
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
        default = "project" if project.exists() else "global"
        self.write_line(
            "\nWhere should these settings be saved?\n"
            f"  global   {self.service.paths.global_file}  (all projects)\n"
            f"  project  {project}  (this repository; cost limits may only be lower)"
        )
        while True:
            answer = self.read_line(f"Save to [{default}]: ").strip().lower() or default
            if answer in ("global", "project"):
                return Layer(answer)
            self.write_line("  Enter global or project.")

    def ask(self, setting: Setting, current: Any, target: Layer, layer_data: RawConfig) -> Any:
        while True:
            answer = self.read_line(f"{setting.label} [{setting.format(current)}]: ").strip()
            try:
                value = setting.parse(answer) if answer else current
            except InvalidAnswer as error:
                self.write_line(f"  {error}")
                continue
            problem = self.check(target, merge(layer_data, nested(setting.key, value)))
            if problem is None:
                return value
            self.write_line(f"  {problem}")

    def check(self, target: Layer, layer_data: RawConfig) -> str | None:
        """Validate a proposed layer against every other layer; return the problem, if any."""
        try:
            self.service.resolve(through=Layer.PROJECT, replace={target: layer_data})
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
            answer = self.read_line(f"{question} [Y/n]: ").strip()
            if not answer:
                return True
            try:
                return parse_bool(answer)
            except InvalidAnswer as error:
                self.write_line(f"  {error}")

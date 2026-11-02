from __future__ import annotations

from collections.abc import Iterable

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from rich.console import RenderableType

from agent_cli.commands import Command, CommandContext, CommandRegistry, build_registry
from agent_cli.commands.completion import CommandCompleter, matching_commands, unique_completion


def noop(context: CommandContext, args: str) -> RenderableType:
    return ""


def registry_of(*names: str) -> CommandRegistry:
    registry = CommandRegistry()
    for name in names:
        registry.register(Command(name, f"About {name}.", noop))
    return registry


def complete(registry: CommandRegistry, text: str) -> list[tuple[str, str]]:
    completions = CommandCompleter(registry).get_completions(Document(text), CompleteEvent())
    return [(completion.text, completion.display_meta_text) for completion in completions]


def test_slash_alone_lists_every_registered_command() -> None:
    registry = build_registry()
    names = [text for text, _ in complete(registry, "/")]
    assert names == [f"/{command.name}" for command in registry]


def test_prefix_filters_and_shows_descriptions() -> None:
    registry = build_registry()
    assert complete(registry, "/st") == [
        ("/startup", registry.get("startup").description),  # type: ignore[union-attr]
        ("/status", registry.get("status").description),  # type: ignore[union-attr]
    ]


def test_prefix_matches_rank_before_substring_matches() -> None:
    registry = registry_of("models", "mode", "autopilot", "rerun")
    assert [command.name for command in matching_commands(registry, "mod")] == ["models", "mode"]
    assert [command.name for command in matching_commands(registry, "un")] == ["rerun"]
    assert [command.name for command in matching_commands(registry, "PIL")] == ["autopilot"]


def test_completion_replaces_the_whole_partial_command() -> None:
    completion = next(
        CommandCompleter(build_registry()).get_completions(Document("/hel"), CompleteEvent())
    )
    assert completion.text == "/help"
    assert completion.start_position == -len("/hel")


def test_registry_changes_reach_completion() -> None:
    registry = registry_of("alpha")
    assert complete(registry, "/b") == []
    registry.register(Command("beta", "Second.", noop))
    assert complete(registry, "/b") == [("/beta", "Second.")]


def test_unknown_partial_commands_get_no_suggestions() -> None:
    assert complete(build_registry(), "/zzz") == []


def test_ordinary_text_gets_no_suggestions() -> None:
    registry = build_registry()
    assert complete(registry, "") == []
    assert complete(registry, "fix the status page") == []
    assert complete(registry, "use /status later") == []


def test_arguments_complete_through_the_command() -> None:
    def models(args: str) -> Iterable[str]:
        return [name for name in ("opus", "sonnet") if name.startswith(args)]

    registry = CommandRegistry()
    registry.register(Command("models", "Models.", noop, complete_args=models))
    registry.register(Command("help", "Help.", noop))
    assert complete(registry, "/models so") == [("sonnet", "")]
    assert complete(registry, "/help x") == []
    assert complete(registry, "/nope x") == []


def test_enter_completes_a_partial_command_with_one_match() -> None:
    registry = build_registry()
    assert unique_completion(registry, "/rev") == "/review"
    assert unique_completion(registry, "/re") == "/review"


def test_enter_leaves_other_input_alone() -> None:
    registry = build_registry()
    assert unique_completion(registry, "/st") is None  # ambiguous
    assert unique_completion(registry, "/status") is None  # already complete
    assert unique_completion(registry, "/zzz") is None
    assert unique_completion(registry, "/rev now") is None
    assert unique_completion(registry, "review the diff") is None

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agent_cli.commands import (
    Command,
    CommandContext,
    CommandRegistry,
    ExitShell,
    build_registry,
)
from agent_cli.commands.builtin import help_command
from agent_cli.commands.registry import parse_command
from agent_cli.config import ConfigPaths, ConfigService, Layer
from agent_cli.skills import Discovery, RoutingRequest, SkillRouter
from agent_cli.state import TaskStatus, TaskStore

REQUIRED_COMMANDS = {
    "help",
    "exit",
    "startup",
    "status",
    "budget",
    "skills",
    "providers",
    "models",
    "autopilot",
    "review",
    "diff",
}


@pytest.fixture
def context() -> CommandContext:
    return CommandContext(registry=build_registry())


def test_registry_contains_every_required_command() -> None:
    assert {command.name for command in build_registry()} == REQUIRED_COMMANDS


def test_help_lists_every_registered_command_and_description(context: CommandContext) -> None:
    output = context.registry.dispatch("/help", context)
    for command in context.registry:
        line = next(line for line in output.splitlines() if line.startswith(f"  /{command.name} "))
        assert command.description in line


def test_help_explains_how_to_use_the_cli_before_listing_commands(
    context: CommandContext,
) -> None:
    output = context.registry.dispatch("/help", context)
    assert output.startswith("Agent CLI help")
    assert output.index("> prompt") < output.index("Commands:")


def test_help_separates_working_commands_from_planned_ones(context: CommandContext) -> None:
    output = context.registry.dispatch("/help", context)
    available, planned = output.split("Coming in later issues:")
    assert "/help" in available and "/exit" in available
    for command in context.registry:
        if command.planned_in is not None:
            assert f"/{command.name}" not in available
            line = next(
                line for line in planned.splitlines() if line.startswith(f"  /{command.name} ")
            )
            assert line.endswith(f"  {command.planned_in}")


def test_help_omits_planned_section_when_nothing_is_planned() -> None:
    registry = CommandRegistry()
    registry.register(Command("help", "Help.", help_command))
    context = CommandContext(registry=registry)
    assert "Coming in later issues" not in registry.dispatch("/help", context)


def test_help_reflects_newly_registered_commands(context: CommandContext) -> None:
    context.registry.register(Command("extra", "An extra command.", lambda ctx, args: ""))
    assert "/extra" in context.registry.dispatch("/help", context)


def test_dispatch_calls_handler_with_args() -> None:
    calls: list[str] = []

    def handler(context: CommandContext, args: str) -> str:
        calls.append(args)
        return "ran"

    registry = CommandRegistry()
    registry.register(Command("echo", "Echo.", handler))
    context = CommandContext(registry=registry)

    assert registry.dispatch("/echo  hello world ", context) == "ran"
    assert calls == ["hello world"]


IMPLEMENTED_COMMANDS = {
    "help",
    "exit",
    "startup",
    "providers",
    "status",
    "skills",
    "models",
    "budget",
    "autopilot",
}


@pytest.mark.parametrize("name", sorted(REQUIRED_COMMANDS - IMPLEMENTED_COMMANDS))
def test_placeholder_commands_say_not_implemented(context: CommandContext, name: str) -> None:
    command = context.registry.get(name)
    assert command is not None and command.planned_in is not None
    output = context.registry.dispatch(f"/{name}", context)
    assert output.startswith("Not implemented yet:")
    assert output.endswith(f"arrives in {command.planned_in}.")


@pytest.mark.parametrize("line", ["/nope", "/", "/HELP"])
def test_unknown_command_is_reported(context: CommandContext, line: str) -> None:
    output = context.registry.dispatch(line, context)
    assert output.startswith(f"Unknown command: {line}.")
    assert "/help" in output


@pytest.mark.parametrize("name", sorted(IMPLEMENTED_COMMANDS))
def test_implemented_commands_are_not_placeholders(context: CommandContext, name: str) -> None:
    command = context.registry.get(name)
    assert command is not None and command.planned_in is None


def test_startup_is_listed_as_available_in_help(context: CommandContext) -> None:
    available = context.registry.dispatch("/help", context).split("Coming in later issues:")[0]
    assert "/startup" in available


def test_status_without_task_state_reports_no_active_task(
    context: CommandContext, tmp_path: Path
) -> None:
    assert context.registry.dispatch("/status", context) == "No active task."
    store = TaskStore(tmp_path / "tasks.sqlite")
    with_store = replace(context, tasks=store)
    assert with_store.registry.dispatch("/status", with_store) == "No active task."
    assert not store.exists()


def test_status_shows_the_persisted_task_tree_pins_and_blockers(
    context: CommandContext, tmp_path: Path
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite")
    session = store.start_session("cache expiration")
    parent = store.create_task(session.id, "Add expiration support", role="orchestrator")
    store.set_status(parent.id, TaskStatus.IN_PROGRESS)
    child = store.create_task(session.id, "Implement TTL", parent_id=parent.id, role="implementer")
    store.pin(child.id, "model", "claude-opus-5-5")
    store.pin(child.id, "harness", "pi")
    store.add_spec_version(child.id, "spec")
    store.add_blocker(child.id, "Choose the default TTL")
    store.close()

    restarted = replace(context, tasks=TaskStore(tmp_path / "tasks.sqlite"))
    lines = restarted.registry.dispatch("/status", restarted).splitlines()
    assert lines[0].startswith(f"Session {session.id}: cache expiration (open")
    assert f"  [in_progress] Add expiration support ({parent.id}) · role: orchestrator" in lines
    assert (
        f"    [pending] Implement TTL ({child.id}) · role: implementer"
        " · pinned: model=claude-opus-5-5, harness=pi · spec v1 pending"
    ) in lines
    assert lines[-2:] == ["Blockers:", f"  {child.id}: Choose the default TTL (waiting on user)"]
    assert not any("session_started" in line for line in lines)


def test_status_reports_unreadable_task_state(context: CommandContext, tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite"
    path.write_bytes(b"garbage" * 100)
    broken = replace(context, tasks=TaskStore(path))
    output = broken.registry.dispatch("/status", broken)
    assert output.startswith("Cannot read task state:")
    assert path.read_bytes() == b"garbage" * 100


def test_skills_lists_the_catalog_without_task_state(context: CommandContext) -> None:
    lines = context.registry.dispatch("/skills", context).splitlines()
    assert lines[0].startswith("Skills: ")
    assert "budget 8000 tokens" in lines[0] and "estimator utf8-bytes-div3@1" in lines[0]
    assert lines[2].split() == ["Skill", "Source", "Version", "Activation", "Tokens"]
    row = next(line for line in lines if line.split()[:1] == ["security"])
    cells = row.split()
    assert cells[1].startswith("builtin:")
    assert cells[2:4] == ["1.0.0", "auto"]
    assert "Routing:" not in lines
    assert not any("trust boundar" in line.lower() for line in lines[3:])  # no skill bodies


def test_skills_shows_effective_source_activation_and_routing(
    context: CommandContext, paths: ConfigPaths, tmp_path: Path
) -> None:
    config = ConfigService(paths)
    config.save(Layer.PROJECT, {"skills": {"activation": {"data": "explicit"}}})
    assert paths.project_root is not None
    override = paths.project_root / ".agent-cli" / "skills" / "testing" / "SKILL.md"
    override.parent.mkdir(parents=True)
    override.write_text("---\nname: testing\ndescription: Ours.\nversion: 3.1.0\n---\nBody\n")
    store = TaskStore(tmp_path / "tasks.sqlite")
    session = store.start_session()
    task = store.create_task(session.id, "Add refresh endpoint", role="implementer")
    router = SkillRouter.from_config(config, store=store)
    first = router.route([RoutingRequest(task.id, "implementer", requested=("research",))])[0]
    router.escalate(
        first,
        RoutingRequest(task.id, "implementer", requested=("research",)),
        Discovery("Touches the session table.", signals=("schema",)),
    )

    with_state = replace(context, config=config, tasks=TaskStore(store.path))
    lines = with_state.registry.dispatch("/skills", with_state).splitlines()
    testing = next(line for line in lines if line.split()[:1] == ["testing"]).split()
    assert testing[1] == f"project:{override.parent.parent.resolve()}"
    assert testing[2:4] == ["3.1.0", "auto"]
    assert next(line for line in lines if line.split()[:1] == ["data"]).split()[3] == "explicit"
    routing = lines[lines.index("Routing:") + 1 :]
    assert routing[0].startswith(f"  Add refresh endpoint ({task.id}) · role: implementer")
    assert " · escalation · " in routing[0] and "no classifier" in routing[0]
    assert routing[1] == "    escalated: Touches the session table."
    assert "    + software-engineering — role default for implementer" in routing
    assert "    + research — explicitly requested" in routing
    assert "    - data — activation: explicit" in routing
    assert routing[-1] == "    signals — escalation: schema"


def test_skills_warns_about_unknown_activation_names(
    context: CommandContext, paths: ConfigPaths
) -> None:
    config = ConfigService(paths)
    config.save(Layer.PROJECT, {"skills": {"activation": {"securty": "always"}}})
    with_config = replace(context, config=config)
    output = with_config.registry.dispatch("/skills", with_config)
    assert "Ignored skills.activation entries (no such skill): securty" in output


def test_skills_reports_invalid_skill_packages(context: CommandContext, tmp_path: Path) -> None:
    bad = Path.home() / ".agent-cli" / "skills" / "bad" / "SKILL.md"
    bad.parent.mkdir(parents=True)
    bad.write_text("no front matter")
    output = context.registry.dispatch("/skills", context)
    assert output.startswith("Cannot load skills:") and "missing metadata" in output


def test_exit_command_signals_shell_exit(context: CommandContext) -> None:
    with pytest.raises(ExitShell):
        context.registry.dispatch("/exit", context)


def test_duplicate_registration_is_rejected() -> None:
    registry = CommandRegistry()
    registry.register(Command("a", "A.", lambda ctx, args: ""))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(Command("a", "Again.", lambda ctx, args: ""))


@pytest.mark.parametrize("name", ["", "/help", "two words"])
def test_invalid_command_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="Invalid command name"):
        Command(name, "Bad.", lambda ctx, args: "")


def test_parse_command_splits_name_and_args() -> None:
    parsed = parse_command("/review  --strict now")
    assert (parsed.name, parsed.args) == ("review", "--strict now")


def test_parse_command_rejects_non_command_input() -> None:
    with pytest.raises(ValueError):
        parse_command("help")

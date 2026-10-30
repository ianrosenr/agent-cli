from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_cli.config import Config, ConfigError, ConfigPaths, ConfigService, Layer
from agent_cli.config.model import CostConfig
from agent_cli.config.schema import ordered, to_raw
from agent_cli.config.service import FILE_HEADER, CostOrderError

CANONICAL_DEFAULTS = yaml.safe_load(
    """
cost:
  monthly_limit_usd: 20.00
  daily_limit_usd: null
  per_task_limit_usd: 1.00
  per_call_limit_usd: 0.30
access:
  preference: [subscription, local, api]
implementation:
  mode: autopilot
review:
  before_merge_request: true
  max_rounds: 3
workers:
  max_concurrency: 3
git:
  use_worktrees: true
  worktree_dir: .agent-cli/worktrees
  branch_prefix: agent-cli/
skills:
  paths: []
  activation: {}
  context_budget_tokens: 8000
harnesses: {}
providers: {}
routing:
  role_floors:
    orchestrator: high
    spec_author: high
    research_investigator: high
    security_investigator: high
    spec_reviewer: mid
    code_reviewer: mid
    implementer: low
"""
)


@pytest.fixture
def paths(tmp_path: Path) -> ConfigPaths:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return ConfigPaths(tmp_path / "home" / ".agent-cli" / "config.yaml", repo)


@pytest.fixture
def service(paths: ConfigPaths) -> ConfigService:
    return ConfigService(paths)


def write(path: Path | None, data: Any) -> None:
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data), encoding="utf-8")


def test_built_in_defaults_match_the_canonical_configuration(service: ConfigService) -> None:
    assert to_raw(service.resolve()) == CANONICAL_DEFAULTS
    assert service.resolve() == Config()


def test_global_config_is_loaded(service: ConfigService, paths: ConfigPaths) -> None:
    write(paths.global_file, {"workers": {"max_concurrency": 5}})
    config = service.resolve()
    assert config.workers.max_concurrency == 5
    assert config.review.max_rounds == 3


def test_project_overrides_global(service: ConfigService, paths: ConfigPaths) -> None:
    write(paths.global_file, {"implementation": {"mode": "checkpoint"}})
    write(paths.project_file, {"implementation": {"mode": "plan-only"}})
    assert service.resolve().implementation.mode == "plan-only"


def test_session_overrides_project(service: ConfigService, paths: ConfigPaths) -> None:
    write(paths.project_file, {"workers": {"max_concurrency": 2}})
    service.set_session({"workers": {"max_concurrency": 6}})
    assert service.resolve().workers.max_concurrency == 6


def test_task_overrides_session(service: ConfigService) -> None:
    service.set_session({"review": {"max_rounds": 5}})
    assert service.resolve(task={"review": {"max_rounds": 1}}).review.max_rounds == 1
    assert service.resolve().review.max_rounds == 5


def test_nested_partial_overrides_inherit_sibling_keys(
    service: ConfigService, paths: ConfigPaths
) -> None:
    write(paths.global_file, {"git": {"branch_prefix": "me/"}})
    write(paths.project_file, {"git": {"use_worktrees": False}, "routing": {"role_floors": {}}})
    config = service.resolve(task={"routing": {"role_floors": {"implementer": "mid"}}})
    assert config.git.branch_prefix == "me/"
    assert config.git.use_worktrees is False
    assert config.git.worktree_dir == ".agent-cli/worktrees"
    assert config.routing.role_floors.implementer == "mid"
    assert config.routing.role_floors.orchestrator == "high"


def test_lists_replace_rather_than_append(service: ConfigService, paths: ConfigPaths) -> None:
    write(paths.global_file, {"skills": {"paths": ["/a", "/b"]}})
    write(paths.project_file, {"skills": {"paths": ["/c"]}})
    assert service.resolve().skills.paths == ("/c",)


def test_resolve_through_stops_at_the_given_layer(
    service: ConfigService, paths: ConfigPaths
) -> None:
    write(paths.global_file, {"workers": {"max_concurrency": 4}})
    write(paths.project_file, {"workers": {"max_concurrency": 2}})
    assert service.resolve(through=Layer.GLOBAL).workers.max_concurrency == 4
    assert service.resolve(through=Layer.DEFAULTS).workers.max_concurrency == 3


def test_integer_money_values_are_floats(service: ConfigService, paths: ConfigPaths) -> None:
    write(paths.global_file, {"cost": {"monthly_limit_usd": 15}})
    assert service.resolve().cost.monthly_limit_usd == 15.0
    assert isinstance(service.resolve().cost.monthly_limit_usd, float)


def test_empty_file_is_an_empty_layer(service: ConfigService, paths: ConfigPaths) -> None:
    write(paths.global_file, "# nothing here\n")
    assert service.resolve() == Config()


def test_no_project_layer_outside_a_repository(tmp_path: Path) -> None:
    paths = ConfigPaths.discover(home=tmp_path, cwd=tmp_path)
    assert paths.project_root is None and paths.project_file is None
    assert ConfigService(paths).resolve() == Config()


def test_discover_finds_the_repository_root(tmp_path: Path) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    nested = tmp_path / "repo" / "src" / "pkg"
    nested.mkdir(parents=True)
    paths = ConfigPaths.discover(home=tmp_path / "home", cwd=nested)
    assert paths.project_file == tmp_path / "repo" / ".agent-cli" / "config.yaml"
    assert paths.global_file == tmp_path / "home" / ".agent-cli" / "config.yaml"


@pytest.mark.parametrize(
    ("data", "key", "problem"),
    [
        ({"agents": {"max_parallel": 4}}, "agents", "unknown key"),
        ({"workers": {"max_parallel": 4}}, "workers.max_parallel", "unknown key"),
        ({"workers": {"max_concurrency": "3"}}, "workers.max_concurrency", "an integer"),
        ({"workers": {"max_concurrency": 0}}, "workers.max_concurrency", "at least 1"),
        ({"workers": {"max_concurrency": True}}, "workers.max_concurrency", "an integer"),
        ({"workers": 3}, "workers", "expected a mapping"),
        ({"cost": {"per_call_limit_usd": -0.1}}, "cost.per_call_limit_usd", "at least 0"),
        ({"cost": {"monthly_limit_usd": "lots"}}, "cost.monthly_limit_usd", "a number"),
        ({"implementation": {"mode": "yolo"}}, "implementation.mode", "one of autopilot"),
        ({"access": {"preference": ["api", "cloud"]}}, "access.preference[1]", "one of"),
        ({"access": {"preference": []}}, "access.preference", "must not be empty"),
        ({"access": {"preference": ["api", "api"]}}, "access.preference", "must not repeat"),
        ({"review": {"before_merge_request": "yes"}}, "review.before_merge_request", "true"),
        ({"review": {"max_rounds": None}}, "review.max_rounds", "got null"),
        ({"skills": {"activation": {"security": "sometimes"}}}, "skills.activation.security", ""),
        ({"skills": {"paths": "/one/path"}}, "skills.paths", "a list"),
        ({"skills": {"paths": [""]}}, "skills.paths[0]", "not a valid path"),
        ({"git": {"worktree_dir": "/abs/path"}}, "git.worktree_dir", "inside the repository"),
        ({"git": {"worktree_dir": "../outside"}}, "git.worktree_dir", "inside the repository"),
        ({"git": {"branch_prefix": "has space/"}}, "git.branch_prefix", "branch prefix"),
        ({"routing": {"role_floors": {"intern": "low"}}}, "routing.role_floors.intern", ""),
        ({"routing": {"role_floors": {"implementer": "top"}}}, "routing.role_floors", "one of"),
        ({"providers": {"local": {"base_url": "http://x"}}}, "providers.local.type", "required"),
        ({"providers": {"local": "openai"}}, "providers.local", "expected a mapping"),
        ({"harnesses": {"pi": {"path": "/bin/pi"}}}, "harnesses.pi.path", "unknown key"),
    ],
)
def test_invalid_config_names_the_file_and_key(
    service: ConfigService, paths: ConfigPaths, data: Any, key: str, problem: str
) -> None:
    write(paths.project_file, data)
    with pytest.raises(ConfigError) as caught:
        service.resolve()
    assert caught.value.source == f"project config ({paths.project_file})"
    assert caught.value.key is not None and caught.value.key.startswith(key)
    assert problem in caught.value.problem
    assert str(paths.project_file) in str(caught.value)


def test_unknown_key_in_session_overrides_is_rejected(service: ConfigService) -> None:
    with pytest.raises(ConfigError, match="session overrides: bogus: unknown key"):
        service.set_session({"bogus": 1})


def test_invalid_task_overrides_are_rejected(service: ConfigService) -> None:
    with pytest.raises(ConfigError, match=r"task overrides: workers\.max_concurrency"):
        service.resolve(task={"workers": {"max_concurrency": -1}})


@pytest.mark.parametrize("text", ["cost: [unclosed", "- just\n- a list\n", "42\n"])
def test_malformed_yaml_fails_clearly(
    service: ConfigService, paths: ConfigPaths, text: str
) -> None:
    write(paths.global_file, text)
    with pytest.raises(ConfigError, match=r"global config \(.*config\.yaml\)"):
        service.resolve()


def test_unreadable_config_fails_clearly(service: ConfigService, paths: ConfigPaths) -> None:
    paths.global_file.mkdir(parents=True)
    with pytest.raises(ConfigError, match="cannot read file"):
        service.resolve()


@pytest.mark.parametrize("key", ["api_key", "token", "secret", "password", "openai_api_key"])
def test_secret_values_are_rejected_with_guidance(
    service: ConfigService, paths: ConfigPaths, key: str
) -> None:
    write(paths.global_file, {"providers": {"x": {"type": "openai-compatible", key: "sk-123"}}})
    with pytest.raises(ConfigError, match=r"secret values are not allowed.*api_key_env"):
        service.resolve()


def test_credential_references_are_accepted(service: ConfigService, paths: ConfigPaths) -> None:
    write(
        paths.global_file,
        {
            "providers": {
                "local-vllm": {
                    "type": "openai-compatible",
                    "base_url": "http://localhost:8000/v1",
                    "api_key_env": "LOCAL_VLLM_API_KEY",
                },
                "other": {"type": "openai-compatible", "api_key_keyring": "agent-cli/other"},
            },
            "harnesses": {"pi": {"command": "pi"}},
        },
    )
    config = service.resolve()
    assert config.providers["local-vllm"].api_key_env == "LOCAL_VLLM_API_KEY"
    assert config.providers["other"].api_key_keyring == "agent-cli/other"
    assert config.harnesses["pi"].enabled is True


GENEROUS_GLOBAL_COST = {
    "monthly_limit_usd": 100.0,
    "daily_limit_usd": 50.0,
    "per_task_limit_usd": 5.0,
    "per_call_limit_usd": 5.0,
}


def set_layer(service: ConfigService, paths: ConfigPaths, layer: str, data: Any) -> Config:
    """Apply ``data`` as the given layer and resolve."""
    if layer == "project":
        write(paths.project_file, data)
        return service.resolve()
    if layer == "session":
        service.set_session(data)
        return service.resolve()
    return service.resolve(task=data)


@pytest.mark.parametrize("layer", ["project", "session", "task"])
@pytest.mark.parametrize("key", list(to_raw(CostConfig())))
def test_more_specific_layers_cannot_raise_cost_ceilings(
    service: ConfigService, paths: ConfigPaths, layer: str, key: str
) -> None:
    write(paths.global_file, {"cost": GENEROUS_GLOBAL_COST})
    limit = GENEROUS_GLOBAL_COST[key]
    with pytest.raises(ConfigError, match=rf"above the global limit of \${limit:.2f}") as caught:
        set_layer(service, paths, layer, {"cost": {key: limit + 0.01}})
    assert caught.value.key == f"cost.{key}"
    assert caught.value.source.startswith(layer)


def test_more_specific_layers_may_lower_cost_ceilings(
    service: ConfigService, paths: ConfigPaths
) -> None:
    write(paths.project_file, {"cost": {"monthly_limit_usd": 10, "per_call_limit_usd": 0.30}})
    service.set_session({"cost": {"per_task_limit_usd": 0.5}})
    cost = service.resolve(task={"cost": {"per_call_limit_usd": 0.1}}).cost
    assert (cost.monthly_limit_usd, cost.per_task_limit_usd, cost.per_call_limit_usd) == (
        10.0,
        0.5,
        0.1,
    )


def test_session_cannot_raise_a_limit_above_the_project_limit(
    service: ConfigService, paths: ConfigPaths
) -> None:
    write(paths.project_file, {"cost": {"monthly_limit_usd": 10}})
    service.set_session({"cost": {"monthly_limit_usd": 15}})
    with pytest.raises(ConfigError, match=r"above the project limit of \$10\.00") as caught:
        service.resolve()
    assert caught.value.source == "session overrides"
    assert caught.value.key == "cost.monthly_limit_usd"


def test_task_cannot_raise_a_limit_above_the_session_limit(service: ConfigService) -> None:
    service.set_session({"cost": {"per_task_limit_usd": 0.5}})
    with pytest.raises(ConfigError, match=r"above the session limit of \$0\.50") as caught:
        service.resolve(task={"cost": {"per_task_limit_usd": 0.75}})
    assert caught.value.source == "task overrides"


def test_task_cannot_raise_a_limit_above_the_project_limit_through_an_empty_session(
    service: ConfigService, paths: ConfigPaths
) -> None:
    write(paths.project_file, {"cost": {"per_call_limit_usd": 0.1}})
    with pytest.raises(ConfigError, match=r"above the project limit of \$0\.10"):
        service.resolve(task={"cost": {"per_call_limit_usd": 0.2}})


def test_ceiling_is_the_default_when_global_sets_none(
    service: ConfigService, paths: ConfigPaths
) -> None:
    write(paths.project_file, {"cost": {"monthly_limit_usd": 25}})
    with pytest.raises(ConfigError, match=r"above the default limit of \$20\.00"):
        service.resolve()


@pytest.mark.parametrize(
    ("layers", "message"),
    [
        ({"project": {"monthly_limit_usd": None}}, "cannot remove the default limit of $20.00"),
        (
            {"global": {"monthly_limit_usd": 30}, "project": {"monthly_limit_usd": None}},
            "cannot remove the global limit of $30.00",
        ),
        (
            {"project": {"per_task_limit_usd": 0.5}, "session": {"per_task_limit_usd": None}},
            "cannot remove the project limit of $0.50",
        ),
    ],
)
def test_more_specific_layers_cannot_remove_a_limit(
    service: ConfigService, paths: ConfigPaths, layers: dict[str, Any], message: str
) -> None:
    if "global" in layers:
        write(paths.global_file, {"cost": layers["global"]})
    if "project" in layers:
        write(paths.project_file, {"cost": layers["project"]})
    if "session" in layers:
        service.set_session({"cost": layers["session"]})
    with pytest.raises(ConfigError) as caught:
        service.resolve()
    assert message in caught.value.problem


def test_unlimited_global_allows_any_specific_limit(
    service: ConfigService, paths: ConfigPaths
) -> None:
    write(paths.global_file, {"cost": {"daily_limit_usd": None}})
    write(paths.project_file, {"cost": {"daily_limit_usd": 15}})
    assert service.resolve().cost.daily_limit_usd == 15.0


def test_global_config_may_raise_limits(service: ConfigService, paths: ConfigPaths) -> None:
    write(paths.global_file, {"cost": {"monthly_limit_usd": 100}})
    write(paths.project_file, {"cost": {"monthly_limit_usd": 50}})
    assert service.resolve().cost.monthly_limit_usd == 50.0


@pytest.mark.parametrize(
    ("cost", "key", "message"),
    [
        (
            {"monthly_limit_usd": 0.5},
            "cost.monthly_limit_usd",
            "the per-task limit ($1.00) cannot be above the monthly limit ($0.50)",
        ),
        (
            {"per_task_limit_usd": 25},
            "cost.per_task_limit_usd",
            "the per-task limit ($25.00) cannot be above the monthly limit ($20.00)",
        ),
        (
            {"per_call_limit_usd": 2},
            "cost.per_call_limit_usd",
            "the per-call limit ($2.00) cannot be above the per-task limit ($1.00)",
        ),
        (
            {"daily_limit_usd": 0.1},
            "cost.daily_limit_usd",
            "the per-call limit ($0.30) cannot be above the daily limit ($0.10)",
        ),
        (
            {"daily_limit_usd": 30},
            "cost.daily_limit_usd",
            "the daily limit ($30.00) cannot be above the monthly limit ($20.00)",
        ),
    ],
)
def test_contradictory_cost_limits_are_rejected(
    service: ConfigService, paths: ConfigPaths, cost: dict[str, Any], key: str, message: str
) -> None:
    write(paths.global_file, {"cost": cost})
    with pytest.raises(CostOrderError) as caught:
        service.resolve()
    assert caught.value.source == f"global config ({paths.global_file})"
    assert caught.value.key == key
    assert caught.value.problem == message


def test_lowering_a_limit_below_an_inherited_narrower_one_is_rejected(
    service: ConfigService, paths: ConfigPaths
) -> None:
    write(paths.project_file, {"cost": {"monthly_limit_usd": 0.5}})
    with pytest.raises(CostOrderError, match="per-task limit") as caught:
        service.resolve()
    assert caught.value.source.startswith("project config")


@pytest.mark.parametrize(
    "cost",
    [
        {"monthly_limit_usd": None, "per_task_limit_usd": 500, "per_call_limit_usd": 100},
        {"per_task_limit_usd": None, "per_call_limit_usd": 10},
        {"daily_limit_usd": 0.5},
        {"daily_limit_usd": 1.0, "per_task_limit_usd": 2.0},
    ],
)
def test_consistent_or_unlimited_cost_limits_are_accepted(
    service: ConfigService, paths: ConfigPaths, cost: dict[str, Any]
) -> None:
    write(paths.global_file, {"cost": cost})
    service.resolve()


def test_save_persists_and_reloads(service: ConfigService, paths: ConfigPaths) -> None:
    path = service.save(Layer.GLOBAL, {"workers": {"max_concurrency": 4}})
    assert path == paths.global_file
    text = path.read_text()
    assert text.startswith(FILE_HEADER)
    assert yaml.safe_load(text) == {"workers": {"max_concurrency": 4}}
    assert ConfigService(paths).resolve().workers.max_concurrency == 4


def test_save_merges_into_existing_layer_in_schema_order(
    service: ConfigService, paths: ConfigPaths
) -> None:
    write(
        paths.project_file,
        {"providers": {"x": {"type": "openai-compatible"}}, "git": {"use_worktrees": False}},
    )
    service.save(
        Layer.PROJECT, {"git": {"branch_prefix": "bot/"}, "cost": {"per_call_limit_usd": 0.1}}
    )
    saved = yaml.safe_load(paths.project_file.read_text())  # type: ignore[union-attr]
    assert saved == {
        "cost": {"per_call_limit_usd": 0.1},
        "git": {"use_worktrees": False, "branch_prefix": "bot/"},
        "providers": {"x": {"type": "openai-compatible"}},
    }
    assert list(saved) == ["cost", "git", "providers"]


def test_save_rejects_invalid_changes_without_writing(
    service: ConfigService, paths: ConfigPaths
) -> None:
    with pytest.raises(ConfigError, match="above the default limit"):
        service.save(Layer.PROJECT, {"cost": {"monthly_limit_usd": 999}})
    with pytest.raises(ConfigError, match="unknown key"):
        service.save(Layer.GLOBAL, {"agents": {"max_parallel": 2}})
    assert not paths.global_file.exists()
    assert paths.project_file is not None and not paths.project_file.exists()


def test_save_rejects_secret_values(service: ConfigService, paths: ConfigPaths) -> None:
    with pytest.raises(ConfigError, match="secret values are not allowed"):
        service.save(Layer.GLOBAL, {"providers": {"x": {"type": "t", "api_key": "sk-live"}}})
    assert not paths.global_file.exists()


def test_save_rejects_non_file_layers(service: ConfigService) -> None:
    with pytest.raises(ConfigError, match="not stored in a file"):
        service.save(Layer.SESSION, {})


def test_save_fails_clearly_on_filesystem_errors(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    service = ConfigService(ConfigPaths(locked / "config.yaml", None))
    try:
        with pytest.raises(ConfigError, match=r"global config \(.*\): cannot write file"):
            service.save(Layer.GLOBAL, {"workers": {"max_concurrency": 2}})
    finally:
        locked.chmod(0o700)
    assert list(locked.iterdir()) == []


def test_ordering_keeps_unknown_keys_so_validation_sees_them() -> None:
    data = {"zzz": 1, "workers": {"bogus": 2, "max_concurrency": 3}, "cost": {}}
    assert ordered(Config, data) == {
        "cost": {},
        "workers": {"max_concurrency": 3, "bogus": 2},
        "zzz": 1,
    }


def test_save_leaves_no_temporary_files(service: ConfigService, paths: ConfigPaths) -> None:
    service.save(Layer.GLOBAL, {"workers": {"max_concurrency": 2}})
    assert [p.name for p in paths.global_file.parent.iterdir()] == ["config.yaml"]


def test_save_git_ignores_project_runtime_directories(
    service: ConfigService, paths: ConfigPaths
) -> None:
    assert paths.project_root is not None
    service.save(Layer.GLOBAL, {"workers": {"max_concurrency": 2}})
    ignore = paths.project_root / ".agent-cli" / ".gitignore"
    assert ignore.read_text().splitlines() == ["state/", "worktrees/"]
    service.save(Layer.GLOBAL, {"workers": {"max_concurrency": 3}})
    assert ignore.read_text().splitlines() == ["state/", "worktrees/"]
    assert not (paths.project_root / ".gitignore").exists()


def test_worktrees_outside_agent_cli_are_ignored_at_the_repo_root(
    service: ConfigService, paths: ConfigPaths
) -> None:
    assert paths.project_root is not None
    (paths.project_root / ".gitignore").write_text("node_modules/")
    service.save(Layer.PROJECT, {"git": {"worktree_dir": "build/trees"}})
    assert (paths.project_root / ".gitignore").read_text() == "node_modules/\n/build/trees/\n"
    assert (paths.project_root / ".agent-cli" / ".gitignore").read_text() == "state/\n"

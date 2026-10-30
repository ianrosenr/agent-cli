from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.config import ConfigPaths, ConfigService
from gitrepo import init_repo


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point HOME at a temporary directory so no test can touch the real ~/.agent-cli/."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def paths(tmp_path: Path) -> ConfigPaths:
    """Config locations in a temporary home and a temporary Git repository."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return ConfigPaths(tmp_path / "home" / ".agent-cli" / "config.yaml", repo)


@pytest.fixture
def service(paths: ConfigPaths) -> ConfigService:
    return ConfigService(paths)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An empty, real Git repository."""
    return init_repo(tmp_path / "project")

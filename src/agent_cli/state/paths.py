"""Where Agent CLI keeps local runtime state.

Project state lives in ``<repo>/.agent-cli/state/`` and global caches in
``~/.agent-cli/cache/``. Neither is configuration, and neither is ever committed.
"""

from __future__ import annotations

from pathlib import Path

AGENT_DIR = ".agent-cli"
STATE_DIR = "state"
CACHE_DIR = "cache"
SELF_IGNORE = "# Agent CLI runtime state: never commit.\n*\n"


def project_state_dir(root: Path) -> Path:
    return root / AGENT_DIR / STATE_DIR


def global_cache_dir(home: Path | None = None) -> Path:
    return (Path.home() if home is None else home) / AGENT_DIR / CACHE_DIR


def ensure_private_dir(directory: Path) -> Path:
    """Create ``directory`` with a ``.gitignore`` that ignores everything in it, itself included.

    This keeps runtime state out of Git even when the repository's own ignore rules do not
    mention it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    ignore = directory / ".gitignore"
    if not ignore.exists():
        ignore.write_text(SELF_IGNORE, encoding="utf-8")
    return directory

"""Where the shipped built-in assets (skills, policies, model registry) live."""

from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent


def builtin_root() -> Path:
    """Shipped built-ins: inside the installed package, or the repository's ``builtins/``."""
    packaged = PACKAGE_ROOT / "builtins"
    if packaged.is_dir():
        return packaged
    return PACKAGE_ROOT.parent.parent / "builtins"

"""The pluggable structural-analyzer interface.

An analyzer turns file *content* into path-independent structure (symbols and imports), so a
result can be cached by ``blob ID + analyzer name + analyzer version`` and reused for identical
content in any branch, worktree, or repository. Anything that depends on where a file lives
(module names, import resolution, relationships) is derived later by the repository index.

New analyzers (Tree-sitter, SCIP, language servers, ...) implement ``Analyzer``; nothing in
the index storage contract or task inspection changes when one is added.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    qualname: str
    """Dotted name within the file, such as ``SessionCache.get``."""
    kind: str
    """``function``, ``class``, or ``method``."""
    line: int
    end_line: int
    exported: bool
    """Whether the symbol is public: listed in ``__all__``, or otherwise not underscored."""


@dataclass(frozen=True, slots=True)
class Import:
    module: str
    """The imported module as written, without leading dots."""
    names: tuple[str, ...]
    """Names imported ``from`` the module; empty for ``import module``."""
    level: int
    """Relative-import depth: 0 for absolute imports, 1 for ``from .``, and so on."""
    line: int


@dataclass(frozen=True, slots=True)
class Analysis:
    language: str
    symbols: tuple[Symbol, ...] = ()
    imports: tuple[Import, ...] = ()
    error: str | None = None
    """Why structure could not be extracted (for example a syntax error)."""

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> Analysis:
        data: dict[str, Any] = json.loads(text)
        return cls(
            language=data["language"],
            symbols=tuple(Symbol(**symbol) for symbol in data["symbols"]),
            imports=tuple(
                Import(**{**item, "names": tuple(item["names"])}) for item in data["imports"]
            ),
            error=data["error"],
        )


class Analyzer(Protocol):
    name: str
    version: str
    """Changing it causes re-analysis of every file this analyzer handles, and nothing else."""
    suffixes: frozenset[str]
    """File suffixes this analyzer handles, such as ``.py``."""

    def analyze(self, content: bytes) -> Analysis:
        """Extract structure. Must not raise for bad input: report it in ``Analysis.error``."""
        ...


@dataclass(frozen=True, slots=True)
class AnalyzerSet:
    analyzers: Sequence[Analyzer] = field(default_factory=tuple)

    def for_path(self, path: str) -> Analyzer | None:
        suffix = PurePosixPath(path).suffix.lower()
        return next((a for a in self.analyzers if suffix in a.suffixes), None)

    def versions(self) -> dict[str, str]:
        return {analyzer.name: analyzer.version for analyzer in self.analyzers}

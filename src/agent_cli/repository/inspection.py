"""Low-cost, deterministic task inspection over the repository fact index.

Given explicit anchors (paths, symbols, changed files, modules, terms), return a small, ranked
neighborhood of files with their facts and evidence: never source dumps and never the whole
repository graph. Natural-language goals are not interpreted here; a caller (the planner, or
skill routing in V1-06) supplies anchors and consumes ``Inspection.facts()``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum

from agent_cli.repository.index import (
    Fact,
    IndexInfo,
    RefreshReport,
    RepositoryIndex,
    SymbolRecord,
)

DEFAULT_LIMIT = 20
NEIGHBOR_LIMIT = 8
"""Most neighbors of one kind (tests, dependencies, dependents) expanded from an anchor file."""
WEAK_NEIGHBOR_LIMIT = 1
"""Most neighbors of one kind expanded from a file found only by mention or lexical match."""
DIRECTORY_LIMIT = 8
DEFINITION_LIMIT = 5
"""Most definitions kept for one symbol anchor; exact qualified-name matches come first."""
LEXICAL_LIMIT = 10
BONUS_PER_POINT = 0.01
BONUS_CAP = 0.9
"""Extra reasons only break ties: the bonus stays below the gap between any two weights."""
SYMBOLS_PER_FILE = 5
SUMMARY_FACT_PREFIXES = ("language:", "signal:")


class Weight(IntEnum):
    """Ranking order from the strongest, most direct evidence to the weakest."""

    EXPLICIT_FILE = 100
    EXPLICIT_SYMBOL = 95
    MODULE = 90
    EXACT_IDENTIFIER = 70
    CHANGED_FILE = 60
    NEIGHBOR = 40
    RELATED_TEST = 35
    LEXICAL = 20
    WEAK_NEIGHBOR = 10


ANCHORED = frozenset(
    {Weight.EXPLICIT_FILE, Weight.EXPLICIT_SYMBOL, Weight.MODULE, Weight.CHANGED_FILE}
)
"""Evidence that makes a file an anchor, whose neighbors are expanded fully. Files found only by
mention or lexical match get a much smaller expansion."""


@dataclass(frozen=True, slots=True)
class Anchors:
    paths: tuple[str, ...] = ()
    """Files or directories named by the task or spec."""
    symbols: tuple[str, ...] = ()
    """Symbol names or dotted qualified names, such as ``SessionCache.get``."""
    changed: tuple[str, ...] = ()
    """Files currently changed by the task."""
    modules: tuple[str, ...] = ()
    """Dotted module or package names."""
    terms: tuple[str, ...] = ()
    """Exact identifiers, error strings, config keys, or other literal phrases."""


@dataclass(frozen=True, slots=True)
class Reason:
    weight: Weight
    detail: str
    """Specific evidence, such as the anchor that matched or the line of an import."""


@dataclass(frozen=True, slots=True)
class InspectedFile:
    path: str
    score: float
    reasons: tuple[Reason, ...]
    facts: tuple[Fact, ...]
    symbols: tuple[SymbolRecord, ...]
    """Matched symbols first, then a few exported ones; never the file's contents."""
    analyzer: str | None
    blob: str


@dataclass(frozen=True, slots=True)
class RepositoryFacts:
    """Compact facts for skill routing. No skill names, no source text."""

    categories: tuple[str, ...]
    signals: tuple[str, ...]
    languages: tuple[str, ...]
    symbols: tuple[str, ...]
    paths: tuple[str, ...]
    evidence: tuple[Fact, ...]


@dataclass(frozen=True, slots=True)
class Inspection:
    anchors: Anchors
    files: tuple[InspectedFile, ...]
    unresolved: tuple[str, ...]
    """Anchors that matched nothing: evidence for diagnosing an inspection miss."""
    truncated: bool
    """Whether candidates were dropped: past ``limit``, or by a per-anchor neighbor cap."""
    index: IndexInfo
    refresh: RefreshReport | None = None
    capped: tuple[str, ...] = ()
    """Anchors that matched more than was kept (a large directory, a common symbol name)."""

    def facts(self) -> RepositoryFacts:
        facts = [fact for file in self.files for fact in file.facts]
        plain = [f.fact for f in facts if not f.fact.startswith(SUMMARY_FACT_PREFIXES)]
        return RepositoryFacts(
            categories=unique(plain),
            signals=unique(f.fact[len("signal:") :] for f in facts if f.fact.startswith("signal:")),
            languages=unique(
                f.fact[len("language:") :] for f in facts if f.fact.startswith("language:")
            ),
            symbols=unique(s.qualname for file in self.files for s in file.symbols),
            paths=tuple(file.path for file in self.files),
            evidence=tuple(facts),
        )


@dataclass
class _Candidates:
    reasons: dict[str, list[Reason]] = field(default_factory=dict)
    matched: dict[str, list[SymbolRecord]] = field(default_factory=dict)

    def add(self, path: str, weight: Weight, detail: str) -> None:
        reasons = self.reasons.setdefault(path, [])
        if all(r.detail != detail for r in reasons):
            reasons.append(Reason(weight, detail))

    def score(self, path: str) -> float:
        weights = sorted((r.weight for r in self.reasons[path]), reverse=True)
        return weights[0] + min(BONUS_PER_POINT * sum(weights[1:]), BONUS_CAP)


class TaskInspector:
    def __init__(self, index: RepositoryIndex) -> None:
        self.index = index
        self._dropped = False

    def inspect(
        self, anchors: Anchors, limit: int = DEFAULT_LIMIT, refresh: bool = True
    ) -> Inspection:
        """Rank the files most directly connected to ``anchors``.

        By default the index is refreshed first (incrementally), so results describe the
        current working tree rather than a stale snapshot.
        """
        report = self.index.refresh() if refresh else None
        index = self.index
        found = _Candidates()
        unresolved: list[str] = []
        capped: list[str] = []
        self._dropped = False

        for path in anchors.paths:
            if not path.strip("./"):
                unresolved.append(f"path:{path}")  # the whole repository is not an anchor
            elif index.file(path) is not None:
                found.add(path, Weight.EXPLICIT_FILE, f"named file '{path}'")
            elif under := index.files(path):
                if len(under) > DIRECTORY_LIMIT:
                    capped.append(f"path:{path} ({len(under)} files, kept {DIRECTORY_LIMIT})")
                for file in under[:DIRECTORY_LIMIT]:
                    found.add(file.path, Weight.EXPLICIT_FILE, f"inside named directory '{path}'")
            else:
                unresolved.append(f"path:{path}")

        for name in anchors.symbols:
            matches = index.find_symbol(name)
            matches.sort(key=lambda symbol: symbol.qualname != name)
            if len(matches) > DEFINITION_LIMIT:
                capped.append(
                    f"symbol:{name} ({len(matches)} definitions, kept {DEFINITION_LIMIT})"
                )
            definitions = matches[:DEFINITION_LIMIT]
            for symbol in definitions:
                detail = f"defines {symbol.kind} '{symbol.qualname}' (line {symbol.line})"
                found.add(symbol.path, Weight.EXPLICIT_SYMBOL, detail)
                found.matched.setdefault(symbol.path, []).append(symbol)
            mentions = [p for p in index.search(name, LEXICAL_LIMIT) if p not in found.matched]
            for path in mentions:
                found.add(path, Weight.EXACT_IDENTIFIER, f"mentions '{name}'")
            if not definitions and not mentions:
                unresolved.append(f"symbol:{name}")

        for module in anchors.modules:
            paths = index.module_paths(module)
            for path in paths:
                found.add(path, Weight.MODULE, f"module '{module}'")
            if not paths:
                unresolved.append(f"module:{module}")

        for path in anchors.changed:
            if index.file(path) is not None:
                found.add(path, Weight.CHANGED_FILE, "changed file")
            else:
                unresolved.append(f"changed:{path}")

        for term in anchors.terms:
            paths = index.search(term, LEXICAL_LIMIT)
            for path in paths:
                found.add(path, Weight.LEXICAL, f"contains '{term}'")
            if not paths:
                unresolved.append(f"term:{term}")

        self._expand(found)

        ranked = sorted(found.reasons, key=lambda p: (-found.score(p), p))
        files = tuple(self._describe(path, found) for path in ranked[:limit])
        return Inspection(
            anchors=anchors,
            files=files,
            unresolved=tuple(unresolved),
            truncated=len(ranked) > limit or self._dropped,
            index=index.info(),
            refresh=report,
            capped=tuple(capped),
        )

    def _expand(self, found: _Candidates) -> None:
        """Add direct structural neighbors: one hop only, capped per anchor and per kind.

        Each kind is capped separately, tests first, so a widely imported module still
        brings in its tests.
        """
        for path, reasons in list(found.reasons.items()):
            anchored = any(r.weight in ANCHORED for r in reasons)
            limit = NEIGHBOR_LIMIT if anchored else WEAK_NEIGHBOR_LIMIT
            tests: list[tuple[str, Weight, str]] = []
            dependencies: list[tuple[str, Weight, str]] = []
            dependents: list[tuple[str, Weight, str]] = []
            for relation in self.index.relations(path):
                if relation.kind == "tests" and relation.dst == path:
                    detail = f"tests '{path}' ({relation.evidence})"
                    tests.append((relation.src, Weight.RELATED_TEST, detail))
                elif relation.kind == "tests":
                    detail = f"tested by '{path}' ({relation.evidence})"
                    dependencies.append((relation.dst, Weight.NEIGHBOR, detail))
                elif relation.src == path:
                    detail = f"imported by '{path}' ({relation.evidence})"
                    dependencies.append((relation.dst, Weight.NEIGHBOR, detail))
                else:
                    detail = f"imports '{path}' ({relation.evidence})"
                    dependents.append((relation.src, Weight.NEIGHBOR, detail))
            for group in (tests, dependencies, dependents):
                self._dropped |= len(group) > limit
                for other, weight, detail in group[:limit]:
                    found.add(other, weight if anchored else Weight.WEAK_NEIGHBOR, detail)

    def _describe(self, path: str, found: _Candidates) -> InspectedFile:
        record = self.index.file(path)
        matched = found.matched.get(path, [])
        exported = [s for s in self.index.symbols(path) if s.exported and s.kind != "method"]
        symbols = unique_symbols([*matched, *exported])[: max(SYMBOLS_PER_FILE, len(matched))]
        reasons = sorted(found.reasons[path], key=lambda r: -r.weight)
        return InspectedFile(
            path=path,
            score=found.score(path),
            reasons=tuple(reasons),
            facts=tuple(self.index.facts(path)),
            symbols=tuple(symbols),
            analyzer=record.analyzer if record else None,
            blob=record.blob if record else "",
        )


def unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def unique_symbols(symbols: list[SymbolRecord]) -> list[SymbolRecord]:
    return list(dict.fromkeys(symbols))

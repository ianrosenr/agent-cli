"""Bounded repository inspection for planning: from a goal to anchors to compact facts.

``TaskInspector`` never interprets natural language, so the anchors are picked out here,
deterministically: path-like tokens, identifier-like tokens, and a few content words. The planner
gets the resulting facts, never file contents or the whole index.
"""

from __future__ import annotations

import re

from agent_cli.orchestration.domain import FileFacts, RepositoryContext
from agent_cli.repository import Anchors, Inspection

MAX_FILES = 12
MAX_PATHS = 8
MAX_SYMBOLS = 8
MAX_TERMS = 6
MIN_TERM_CHARS = 4
REASONS_PER_FILE = 3
FACTS_PER_FILE = 8
SYMBOLS_PER_FILE = 5

TOKEN = re.compile(r"[\w./-]+")
IDENTIFIER = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
FILE_EXTENSIONS = """
    py pyi md rst txt toml yaml yml json ini cfg js jsx ts tsx go rs java kt rb sh sql html css
"""
COMMON_WORDS = """
    about above after again against also because before being below between both could does
    doing down during each every from further have having here into just more most must need
    only other over same should some such than that their them then there these they this
    those through under until very want were what when where which while with within without
    would your make sure please change changes changing update updates updating support
    existing current currently instead using used uses work works
"""
"""Words too common to be useful lexical anchors."""
EXTENSIONS = frozenset(FILE_EXTENSIONS.split())
STOPWORDS = frozenset(COMMON_WORDS.split())


def goal_anchors(goal: str) -> Anchors:
    paths: list[str] = []
    symbols: list[str] = []
    terms: list[str] = []
    for raw in TOKEN.findall(goal):
        token = raw.strip(".,;:!?()[]{}'\"`-")
        if not token:
            continue
        stem, dot, extension = token.rpartition(".")
        if "/" in token or (dot and stem and extension.casefold() in EXTENSIONS):
            paths.append(token)
        elif IDENTIFIER.fullmatch(token) and is_identifier_like(token):
            symbols.append(token)
        elif token.isalpha() and len(token) >= MIN_TERM_CHARS:
            word = token.casefold()
            if word not in STOPWORDS:
                terms.append(word)
    return Anchors(
        paths=unique(paths)[:MAX_PATHS],
        symbols=unique(symbols)[:MAX_SYMBOLS],
        terms=unique(terms)[:MAX_TERMS],
    )


def is_identifier_like(token: str) -> bool:
    """``snake_case``, ``CamelCase``, ``mixedCase``, or dotted names; not plain words."""
    if "_" in token or "." in token:
        return True
    return any(c.isupper() for c in token[1:]) and any(c.islower() for c in token)


def repository_context(inspection: Inspection) -> RepositoryContext:
    files = tuple(
        FileFacts(
            path=file.path,
            reasons=tuple(reason.detail for reason in file.reasons[:REASONS_PER_FILE]),
            facts=tuple(fact.fact for fact in file.facts[:FACTS_PER_FILE]),
            symbols=tuple(symbol.qualname for symbol in file.symbols[:SYMBOLS_PER_FILE]),
        )
        for file in inspection.files[:MAX_FILES]
    )
    return RepositoryContext(
        files=files,
        languages=inspection.facts().languages,
        unresolved=inspection.unresolved,
        truncated=inspection.truncated or len(inspection.files) > MAX_FILES,
    )


def unique(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))

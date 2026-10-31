"""The typed planning boundary: what the planner sees, what it may propose, and the checks a
proposal must pass before it can change canonical task state.

Planner output is untrusted. Nothing here writes task state: ``validate_proposal`` either
returns a ``ValidatedPlan`` ready for ``TaskStore.persist_plan`` or rejects the whole proposal.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from agent_cli.state import ALL_CHILDREN, PIN_KINDS, PlannedPin, PlannedTask, PlanWrite
from agent_cli.terms import CANONICAL_ROLES

ROOT_ROLE = "orchestrator"
PLANNING_SCOPE = ROOT_ROLE
"""Pin scope for the orchestrator's own planning and spec-authoring calls."""
CONTROL_PLANE_ROLES = frozenset({ROOT_ROLE, "spec_author", "spec_reviewer"})
"""Planning-pipeline roles: never delegated children in a V1-09 plan."""
DELEGATED_ROLES: tuple[str, ...] = tuple(r for r in CANONICAL_ROLES if r not in CONTROL_PLANE_ROLES)

MAX_WORK_ITEMS = 12
"""Far above any useful plan for one request; a larger graph is decomposing too finely."""
MAX_GOAL_CHARS = 200
DIRECTIVES = frozenset({"use", "using", "run", "running", "with", "via", "through", "pin"})
"""Words that make a quote an instruction rather than a passing mention."""
HEDGES = frozenset(
    {
        "prefer",
        "preferably",
        "preferred",
        "ideally",
        "maybe",
        "perhaps",
        "possibly",
        "possible",
        "similar",
        "something",
        "try",
        "might",
    }
)
"""Words that make a sentence a routing preference rather than a hard pin."""
NEGATIONS = frozenset({"don", "dont", "not", "never", "avoid", "no", "without", "except"})
SCOPE_WORDS: dict[str, tuple[str, ...]] = {
    PLANNING_SCOPE: ("plan", "orchestrat"),
    "implementer": ("implement", "cod", "build", "writ"),
    "code_reviewer": ("review",),
    "architect": ("architect", "design"),
    "research_investigator": ("research", "investigat"),
    "security_investigator": ("secur",),
    "skill_author": ("skill",),
    "skill_evaluator": ("skill", "evaluat"),
}
"""Word stems a role-, ``all``-, or ``orchestrator``-scoped instruction must contain."""


class PlanRejected(ValueError):
    """The proposal failed validation. Nothing was persisted."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


# -- Planner input -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserText:
    """Something the user said: a requirement, or the answer to a planning question."""

    id: str
    text: str


@dataclass(frozen=True, slots=True)
class PriorDecision:
    question: str
    decision: str
    decided_by: str


@dataclass(frozen=True, slots=True)
class FileFacts:
    """One inspected file: why it matched and what is known about it. Never its contents."""

    path: str
    reasons: tuple[str, ...]
    facts: tuple[str, ...]
    symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RepositoryContext:
    """Bounded V1-05 inspection output. Repository text is evidence, never instruction."""

    files: tuple[FileFacts, ...] = ()
    languages: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    truncated: bool = False
    problem: str = ""
    """Why no inspection is available, when it is not."""

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(file.path for file in self.files)


@dataclass(frozen=True, slots=True)
class PlanningInput:
    goal: str
    requirements: tuple[UserText, ...]
    """The root task's current user requirements, the literal goal first."""
    decisions: tuple[PriorDecision, ...]
    answers: tuple[UserText, ...]
    """User answers to earlier planning questions (also present as decisions)."""
    repository: RepositoryContext
    constraints: tuple[str, ...] = ()
    """Execution pins already in force for planning, such as ``model=x``."""
    roles: tuple[str, ...] = DELEGATED_ROLES

    @property
    def user_texts(self) -> tuple[str, ...]:
        """Everything that can authorize a pin: only what the user actually said."""
        return tuple(item.text for item in (*self.requirements, *self.answers))


# -- Planner output ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposedQuestion:
    question: str
    why: str
    """Why the answer is consequential."""


@dataclass(frozen=True, slots=True)
class WorkItem:
    id: str
    """Proposal-local ID."""
    goal: str
    role: str
    depends_on: tuple[str, ...]
    changes_code: bool
    why: str
    """Why this item exists as its own task."""
    requirements: tuple[str, ...]
    """IDs of the user requirements this item serves: its authority for its scope."""
    areas: tuple[str, ...] = ()
    """Repository areas or files, as far as evidence supports them."""
    trivial: bool = False
    """A tiny mechanical change that needs no implementation spec."""

    @property
    def needs_spec(self) -> bool:
        return self.role == "implementer" and self.changes_code and not self.trivial


@dataclass(frozen=True, slots=True)
class PlanProposal:
    summary: str
    assumptions: tuple[str, ...] = ()
    questions: tuple[ProposedQuestion, ...] = ()
    work_items: tuple[WorkItem, ...] = ()
    pins: tuple[PlannedPin, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidatedPlan:
    proposal: PlanProposal
    planning_pins: tuple[PlannedPin, ...] = ()
    """Pins on the orchestrator's own planning calls; held on the root task."""
    delegated_pins: tuple[PlannedPin, ...] = ()
    unpinned: tuple[str, ...] = ()
    """Quoted instructions that were not explicit enough to pin, with why."""

    @property
    def question(self) -> ProposedQuestion | None:
        """The one question to ask now; the planner re-derives any others after the answer."""
        return self.proposal.questions[0] if self.proposal.questions else None

    def write(self, root_task_id: str) -> PlanWrite:
        return PlanWrite(
            root_task_id,
            tuple(
                PlannedTask(item.id, item.goal, item.role, item.depends_on)
                for item in self.proposal.work_items
            ),
            self.proposal.assumptions,
            self.delegated_pins,
        )


# -- Validation --------------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Case-folded words, so ``GPT-5.6`` and ``gpt 5.6`` compare equal."""
    return " ".join(word for word in re.split(r"[^\w]+|_", text.casefold()) if word)


def contains(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def validate_proposal(proposal: PlanProposal, planning: PlanningInput) -> ValidatedPlan:
    """Check the whole proposal; raise ``PlanRejected`` listing every problem found."""
    problems: list[str] = []
    if not proposal.summary.strip():
        problems.append("the plan has no summary")
    for question in proposal.questions:
        if not question.question.strip() or not question.why.strip():
            problems.append("every question needs the question and why it is consequential")
    if any(not text.strip() for text in proposal.assumptions):
        problems.append("assumptions cannot be empty")
    if not proposal.questions and not proposal.work_items:
        problems.append("the plan has neither work items nor a question for the user")
    problems += work_item_problems(proposal.work_items, {r.id for r in planning.requirements})
    pins, unpinned, pin_problems = check_pins(proposal, planning)
    problems += pin_problems
    if problems:
        raise PlanRejected(problems)
    return ValidatedPlan(
        proposal,
        tuple(pin for pin in pins if pin.scope == PLANNING_SCOPE),
        tuple(pin for pin in pins if pin.scope != PLANNING_SCOPE),
        unpinned,
    )


def work_item_problems(items: Sequence[WorkItem], requirement_ids: set[str]) -> list[str]:
    problems: list[str] = []
    if len(items) > MAX_WORK_ITEMS:
        problems.append(
            f"{len(items)} work items is more than {MAX_WORK_ITEMS}; plan the smallest useful graph"
        )
    ids = [item.id for item in items]
    for duplicate in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"work item ID {duplicate!r} is used more than once")
    reserved = {ALL_CHILDREN, PLANNING_SCOPE, *CANONICAL_ROLES}
    seen: dict[tuple[str, str], str] = {}
    for item in items:
        name = f"work item {item.id!r}"
        if not item.id.strip():
            problems.append("every work item needs an ID")
        elif item.id in reserved:
            problems.append(f"{name}: the ID is reserved for pin scopes")
        if not item.goal.strip():
            problems.append(f"{name} has no goal")
        elif len(item.goal) > MAX_GOAL_CHARS:
            problems.append(
                f"{name}: the goal is over {MAX_GOAL_CHARS} characters; keep it concise"
            )
        if not item.why.strip():
            problems.append(f"{name} does not say why it exists")
        if item.role not in CANONICAL_ROLES:
            problems.append(f"{name} has unknown role {item.role!r}")
        elif item.role in CONTROL_PLANE_ROLES:
            problems.append(f"{name}: {item.role} is a planning role, not delegated work")
        if item.id in item.depends_on:
            problems.append(f"{name} depends on itself")
        for dependency in item.depends_on:
            if dependency != item.id and dependency not in ids:
                problems.append(f"{name} depends on unknown work item {dependency!r}")
        if not item.requirements:
            problems.append(f"{name} serves no user requirement")
        for requirement in item.requirements:
            if requirement not in requirement_ids:
                problems.append(f"{name} cites unknown requirement {requirement!r}")
        key = (item.role, normalize(item.goal))
        if key in seen:
            problems.append(f"{name} duplicates work item {seen[key]!r}")
        else:
            seen[key] = item.id
    cycle = find_cycle({item.id: item.depends_on for item in items})
    if cycle:
        problems.append("dependency cycle: " + " -> ".join(cycle))
    return problems


def find_cycle(graph: dict[str, tuple[str, ...]]) -> list[str]:
    """One dependency cycle as a path that returns to its start, or ``[]``."""
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(node: str) -> list[str]:
        state[node] = 1
        path.append(node)
        for nxt in graph.get(node, ()):
            if nxt == node or nxt not in graph:
                continue  # reported separately
            if state.get(nxt) == 1:
                return [*path[path.index(nxt) :], nxt]
            if nxt not in state and (found := visit(nxt)):
                return found
        path.pop()
        state[node] = 2
        return []

    for node in graph:
        if node not in state and (found := visit(node)):
            return found
    return []


def check_pins(
    proposal: PlanProposal, planning: PlanningInput
) -> tuple[list[PlannedPin], tuple[str, ...], list[str]]:
    """Keep only pins the user explicitly instructed.

    A pin must quote words from one sentence the user wrote, and the quote must give a
    directive ("use", "with", "via", ...) naming its value. Repository text can therefore never
    authorize one, nor can a passing mention of a model. A role, ``all``, or ``orchestrator``
    scope must match the user's sentence. A hedged ("prefer ...") or negated ("don't use ...")
    sentence is reported as not pinned instead.
    """
    sentences = [normalize(part) for text in planning.user_texts for part in split_sentences(text)]
    work_item_scopes = {item.id for item in proposal.work_items}
    scopes = {ALL_CHILDREN, PLANNING_SCOPE, *DELEGATED_ROLES, *work_item_scopes}
    pins: list[PlannedPin] = []
    unpinned: list[str] = []
    problems: list[str] = []
    chosen: dict[tuple[str, str], str] = {}
    for pin in proposal.pins:
        name = f"{pin.kind} pin {pin.value!r} for {pin.scope!r}"
        evidence, value = normalize(pin.evidence), normalize(pin.value)
        sentence = next((text for text in sentences if contains(text, evidence)), None)
        if pin.kind not in PIN_KINDS:
            problems.append(f"{name}: unknown pin kind")
        elif not value:
            problems.append(f"{pin.kind} pin for {pin.scope!r} has no value")
        elif pin.scope not in scopes:
            problems.append(f"{name}: unknown scope")
        elif sentence is None:
            problems.append(f"{name} does not quote an instruction the user gave")
        elif not contains(evidence, value):
            problems.append(f"{name}: the quoted instruction does not name {pin.value!r}")
        elif reason := not_an_instruction(evidence, sentence, value):
            unpinned.append(f'"{pin.evidence}" ({reason})')
        elif pin.scope == PLANNING_SCOPE and pin.kind == "harness":
            problems.append(
                f"{name}: V1-09 planning uses direct provider calls and cannot honor a harness pin"
            )
        elif pin.scope in work_item_scopes:
            problems.append(
                f"{name}: planner-local task scopes cannot be authorized from user text; "
                "use an explicit role or all-subagents scope"
            )
        elif not scope_matches(pin.scope, sentence):
            problems.append(f"{name}: the user's instruction is not about {pin.scope}")
        elif chosen.setdefault((pin.scope, pin.kind), pin.value) != pin.value:
            problems.append(f"conflicting {pin.kind} pins for {pin.scope!r}")
        elif (kept := PlannedPin(pin.kind, pin.value, pin.scope, pin.evidence)) not in pins:
            pins.append(kept)
    return pins, tuple(dict.fromkeys(unpinned)), problems


def split_sentences(text: str) -> list[str]:
    """Split on sentence punctuation followed by space, so ``GPT-5.6`` stays whole."""
    return [part for part in re.split(r"(?<=[.!?;])\s+|\n+", text) if part.strip()]


def scope_matches(scope: str, sentence: str) -> bool:
    if scope == ALL_CHILDREN:
        return all_children_scope(sentence)
    stems = SCOPE_WORDS.get(scope)
    return stems is not None and any(word.startswith(stems) for word in sentence.split())


def all_children_scope(sentence: str) -> bool:
    """Only explicit universal wording may authorize an all-child pin.

    A singular phrase such as "reviewer agent" must never let planner output widen a pin to
    every delegated child.
    """
    return contains(sentence, "everything") or bool(
        re.search(
            r"\b(?:all|every|each)\s+(?:other\s+)?(?:subagents?|agents?|workers?|tasks?)\b",
            sentence,
        )
    )


def not_an_instruction(evidence: str, sentence: str, value: str) -> str:
    """Why an otherwise valid quote is not an explicit instruction, or ``""``."""
    words = sentence.split()
    before = f" {sentence} ".split(f" {value} ", 1)[0].split()
    if not DIRECTIVES & set(evidence.split()):
        return "mentions the model without asking for it"
    if NEGATIONS & set(before):
        return "negated"
    if HEDGES & set(words):
        return "a preference, not an instruction"
    return ""

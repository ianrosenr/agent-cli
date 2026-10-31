# Orchestration

## Task flow

V1 planning and execution are deliberately separate phases:

```text
user goal
 → V1-09 orchestrator: inspect repo, separate facts from assumptions, decompose
 → V1-09 spec author: persist concise pending implementation specs
 → STOP: plan is ready for review/implementation
 → V1-10 spec reviewer: challenge spec → approved spec
 → V1-10 implementer workflow
 → V1-11 worker/worktree execution
 → V1-12 code reviewer: challenge diff against spec → fixes
 → orchestrator: final diff to user → MR gate
```

Ordinary text at the root prompt is the orchestration surface. Slash commands bypass orchestration and remain control/status/configuration commands. There is no separate orchestration REPL, route selector, `/orchestrate`, `/agent`, or `/chat` mode.

V1-09 ends with canonical planning state: a root orchestration task, a validated child-task graph, role assignments, skill-routing decisions, explicit execution pins, and pending short specs for non-trivial implementer work. It does not edit code, launch coding harnesses, create worktrees, approve specs, or run implementation/review loops.

Planning/spec-author reasoning may use direct provider calls through the existing model, access, and budget seams. Paid planning calls must pass V1-08 budget admission and reservation immediately before invocation. Planning model calls are control-plane reasoning, not coding workers.

## Planning lifecycle

For a new ordinary-language request:

1. Create one root task with role `orchestrator`.
2. Record the literal user goal as a user requirement on that root.
3. Inspect bounded V1-05 repository context before asking repository-answerable questions.
4. Produce a typed plan proposal; model prose never writes TaskStore directly.
5. Validate the entire proposed graph before persisting child tasks.
6. Persist child tasks, roles, dependencies, assumptions, and propagated explicit pins atomically.
7. Route skills separately through V1-06 after roles are fixed.
8. Produce and persist pending short specs for non-trivial implementer tasks.
9. Return a concise plan summary and stop before implementation.

If child-graph persistence fails, none of that proposal's child tasks/dependencies/pins are committed. If later skill/spec generation fails, keep the valid graph, mark the affected work blocked, and make the partial phase explicit rather than deleting canonical state.

## Typed plan boundary

Planner output is untrusted input. Validate a typed proposal before it mutates canonical state.

Each proposed work item has at least:

- a proposal-local ID
- concise goal
- exactly one canonical role
- dependency IDs
- whether it changes code
- why it exists
- optional repository areas/evidence

Validation rejects unknown roles, duplicate IDs, missing dependencies, self-dependencies, cycles, duplicate/equivalent work items, empty goals, and unauthorized scope expansion.

The planner does not choose automatic skill names. It does not choose model/provider/harness values except to preserve an explicit user execution constraint.

Decompose into the smallest useful graph. Do not create tasks merely to fill worker concurrency or one task per file.

## Planning questions

The orchestrator asks the user only about consequential unresolved decisions that repository facts or approved prior decisions cannot answer.

A consequential choice can materially affect public behavior, compatibility, data, security, architecture, cost, destructive operations, or user-visible semantics. When uncertain whether a choice is consequential, treat it as consequential.

Planning questions are canonical state, not transient terminal text. Persist at most one pending question per root task, including the concise question, why it matters, status, answer, and timestamps.

When a root task has a pending planning question, the next ordinary non-slash input answers that question instead of creating a new root request. The answer is persisted and becomes a user decision before planning resumes. Restarting Agent CLI must not silently skip or forget the question.

Safe, reversible implementation-local assumptions may be recorded explicitly. An assumption is never silently promoted into a fact or decision.

## Short implementation specs

Every non-trivial code-changing `implementer` child receives a versioned spec with `approval=pending`.

Specs use this logical section order:

```text
Objective
Affected areas
Required behavior
Unchanged behavior
Constraints
Edge cases
Tests
Success criteria
Out of scope
```

Keep specs concise: enough to remove implementation ambiguity, not a design essay. Do not invent exact files when evidence only identifies an area/module. Consequential unresolved questions prevent the work from becoming implementation-ready.

V1-09 never approves its own specs. V1-10 owns spec review and approval.

## Implementation

`agent_cli.orchestration` implements the V1-09 planning contract:

| Module | Responsibility |
| --- | --- |
| `domain.py` | `PlanningInput`, `PlanProposal`, `WorkItem`, and whole-proposal validation (roles, IDs, dependencies, cycles, duplicates, requirement citations, pin authorization) |
| `inspection.py` | deterministic goal → V1-05 anchors, and bounded `RepositoryContext` facts for the planner |
| `calls.py` | `DirectModelCall`: planning pin or role floor → V1-07 candidates → V1-08 budget filtering → `BudgetGuard` |
| `planner.py` | the `Planner` seam, the provider-backed planner prompt and strict reply parser, and `FakePlanner` |
| `spec_author.py` | `SpecDraft`, draft validation, fixed-order spec rendering, the provider-backed spec author, and `FakeSpecAuthor` |
| `service.py` | `OrchestrationService`: session/root lifecycle, questions, the atomic plan write, skill routing, specs |
| `render.py` | the compact plan/question output shown at the prompt, built with `agent_cli.ui` |

`cli.handle_input` sends every non-slash line to `OrchestrationService.handle`, built on first use so planning stays out of startup. Outside a Git repository there is no task state, so ordinary input explains where to run instead.

Validation details worth knowing:

- Every work item must cite the IDs of the user requirements it serves; that citation is its authority for scope.
- A pin must quote words from one sentence the user wrote (in the request or a planning answer), and the quote must give a directive (`use`, `with`, `via`, `run`, ...) naming the pinned value. Repository text therefore cannot authorize a pin, and neither can a passing mention of a model. A hedged ("prefer ...", "ideally ...") or negated ("don't use ...") sentence is reported under "Not pinned" instead.
- Pin scopes are `all`, a delegated role, a work-item ID, or `orchestrator` (the planning calls themselves). A role, `all`, or `orchestrator` scope must match the user's sentence. Pins apply from least to most specific, so a role or work-item pin beats an `all` pin of the same kind. Role and `all` scopes are copied onto matching children and kept as scoped pins for roles created later, such as a V1-12 code reviewer.
- When a proposal adds an `orchestrator` model or provider pin that the call producing it did not honor, planning is redone once under the pin; spend for both calls is shown in "Planned with". If the pinned model cannot run, planning stops rather than substituting.
- Planner and spec-author replies are parsed strictly: unknown fields (such as skill or model choices) are errors. A spec may name an existing file the index knows, a known directory, or a new file directly inside a known directory; dotted module names and prose areas are always allowed.
- Answers to planning questions are added to the inspection anchors before re-planning. After three questions on one request, planning stops and asks for a restated request. `/cancel` withdraws a pending question and cancels its request, so the next line starts a new one.

`/autopilot` shows the effective `implementation.mode` and where it comes from; `/autopilot on|off|plan-only [global|project]` saves only that key (default: global). `off` means `checkpoint`. V1-10 applies the mode.

## Roles and authority

| Role | Reasoning floor | May | May not |
| --- | --- | --- | --- |
| Orchestrator | high | decompose work, write canonical memory, coordinate, ask the user, escalate | edit code directly |
| Spec author | high | read the repo, write and revise specs | edit code, expand the user's goal |
| Spec reviewer | mid (escalates on risk) | question the spec author, approve or reject specs | rewrite the spec itself |
| Architect | high | analyze architecture and propose bounded design constraints/options | edit code, expand product intent |
| Implementer | low (with a precise spec) | edit files in its own worktree within spec scope, run tests | redesign, infer requirements, broaden scope, do unrelated refactors, silently resolve consequential ambiguity |
| Code reviewer | mid (escalates on risk) | question the implementer, request changes, approve | edit code |
| Research investigator | high | read the repo and approved external sources, report findings | edit code, make design decisions |
| Security investigator | high | assess security-sensitive specs and diffs | edit code |
| Skill author | high | author/revise skill policy content within an explicitly scoped skill task | expand another role's authority or change product scope |
| Skill evaluator | high | evaluate skill behavior/coverage and report findings | silently rewrite skills or expand another role's authority |

Floors are defaults, configurable but not shown in `/startup`. See [ROUTING.md](ROUTING.md#role-floors).

## Orchestrator rules

- Assume nothing about implementation intent.
- Classify planning input as: explicit user requirement, repository fact, approved prior decision, assumption, or unresolved consequential decision. Record it under the appropriate canonical state type.
- Inspect the repository before asking the user anything the code or docs can answer.
- Ask the user only about consequential unresolved decisions.
- Workers send questions to the orchestrator, not to the user or each other. The orchestrator answers from memory, delegates research, or asks the user.

## Explicit user execution pins

An explicit user choice of model, provider, or harness is a task constraint, not a routing hint.

Examples:

```text
Use Claude Sonnet for the implementer.
Have the reviewer use GPT-5.6.
Use model X for all subagents on this task.
```

The orchestrator must:

1. record the request as an explicit user requirement
2. attach the constraint to the affected role/task/subtask
3. preserve it in canonical memory
4. explicitly copy an all-subagents constraint onto each affected child because TaskStore pins do not inherit
5. pass it to routing and later worker launch
6. show the pin in routing/status explanations

A specific model pin overrides automatic capability classification and normal role-floor model selection for that scope.

The pin does not override hard runtime constraints such as paid-budget ceilings, provider authentication/availability, context-window requirements, or harness incompatibility.

If the pinned model cannot run, Agent CLI asks the user instead of silently selecting a different model.

If only a model is pinned, Agent CLI may still choose a compatible provider and harness for that same model. Explicit provider or harness pins are preserved the same way.

Vague preferences such as "use something cheap" are routing preferences, not hard pins.

## Implementation modes


| Mode | Behavior |
| --- | --- |
| **autopilot** (default) | After spec approval, the implementer makes every change the spec authorizes without asking per edit. No scope expansion. |
| **checkpoint** | The implementer proposes each coherent batch (usually one file or one tightly related set of edits) and waits for user approval. |
| **plan-only** | No files change. The agent explains the intended implementation. |

In every mode, anything outside the approved spec stops the implementer and escalates.

## Escalation

An implementer escalates instead of proceeding when:

- the spec is ambiguous on a consequential point
- satisfying the spec requires touching out-of-scope behavior
- tests or checks cannot pass without violating the spec
- it discovers work that needs a skill it was not given

The orchestrator resolves the escalation, revises the spec (which then needs re-review if the change is material), or asks the user.

## Requirement changes mid-task

When the user adds a requirement after work starts:

1. Record the requirement.
2. Determine affected tasks.
3. Update affected specs and record the spec diff.
4. Notify affected workers.
5. Re-review if the change is material. Approval of an affected spec is void until re-reviewed.

## Concurrency

Independent subtasks may run in parallel up to the configured worker limit. Each implementer gets its own worktree and branch. The orchestrator integrates results; workers do not merge each other's branches.

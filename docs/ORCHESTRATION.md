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

## Roles and authority

| Role | Reasoning floor | May | May not |
| --- | --- | --- | --- |
| Orchestrator | high | decompose work, write canonical memory, dispatch and stop workers, ask the user, escalate | edit code directly |
| Spec author | high | read the repo, write and revise specs | edit code, expand the user's goal |
| Spec reviewer | mid (escalates on risk) | question the spec author, approve or reject specs | rewrite the spec itself |
| Implementer | low (with a precise spec) | edit files in its own worktree within spec scope, run tests | redesign, infer requirements, broaden scope, do unrelated refactors, silently resolve consequential ambiguity |
| Code reviewer | mid (escalates on risk) | question the implementer, request changes, approve | edit code |
| Research investigator | high | read the repo and approved external sources, report findings | edit code, make design decisions |
| Security investigator | high | assess security-sensitive specs and diffs | edit code |

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

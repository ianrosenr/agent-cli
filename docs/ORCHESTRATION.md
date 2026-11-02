# Orchestration

## Task flow

The root prompt is one continuous supervisor conversation. Internal actions are deliberately small:

```text
user turn
  -> answer
  -> scout (read-only repository work)
  -> ship  (repository-changing work)
```

The supervisor also estimates `trivial | moderate | complex`. That estimate guides how much
orchestration and model intelligence to spend; it never grants authority.

```text
answer
  -> direct answer, optionally with bounded repository context

trivial scout/ship
  -> one typed worker task
  -> skill + dispatch routing
  -> worker lifecycle

moderate/complex scout/ship
  -> conditional decomposition/planning
  -> independent typed worker tasks
  -> skill + dispatch routing
  -> worker lifecycle
```

V1-09.9 implements the supervisor intake and the first optimization: trivial scout/ship work
bypasses the planner-model call and persists one bounded task directly. Worker execution still
lands in V1-11.

## Root-prompt supervisor

There are no user-facing `/chat`, `/agent`, scout, or ship modes. The semantic supervisor sees
the current turn plus compact recent conversation and emits:

```json
{
  "action": "answer | scout | ship",
  "confident": true,
  "request": "self-contained resolved request",
  "repository": true,
  "complexity": "trivial | moderate | complex"
}
```

- **answer**: ordinary chat or a question answerable from bounded repository evidence.
- **scout**: read-only work requiring tools/fresh evidence: run tests/lint/status, reproduce,
  investigate, audit, or gather facts.
- **ship**: user-authorized repository changes.
- **repository** is context for `answer`, not a separate interaction mode.
- **complexity** is a resource-allocation hint. It does not widen scope.

Greetings use a deterministic answer fast path. Everything else is semantic. Uncertain work intent
fails toward read-only answering. Scout/ship outside a repository explains where repository work
must run.

Conversation memory is transient and separate from canonical task state. The literal user turn is
the authoritative requirement. The supervisor's resolved request becomes the task goal; recent
conversation is derived context only, so "go ahead and do that" can be resolved without turning an
earlier assistant suggestion into user authority.

## Answer path

An answer creates no task state. For a trivial general question the semantic supervisor may return
the final concise answer in the same call, avoiding a second model invocation. Greetings use the
zero-model fast path.

Repository-aware answers still run bounded V1-05 inspection and a downstream answer call because
the supervisor intake turn does not receive repository contents. Moderate/complex general answers
may also use the downstream answer model rather than forcing a small intake model to overreach.

Explicit answer pins, access preference and budget guardrails still apply to downstream answer
calls. An explicit answer pin prevents the inline shortcut so the requested model/provider is
honored. Concise verbosity hides internal supervisor decisions.

## Task brief and conditional decomposition

Scout/ship work crosses the conversational boundary as a typed task brief:

```text
kind: scout | ship
goal: self-contained supervisor restatement
context: bounded conversation needed to resolve references
complexity: trivial | moderate | complex
```

The user's literal request remains separately stored as the authoritative requirement.

For `trivial` work, `OrchestrationService.prepare_single_task` creates one worker task without a
planner call:

- scout -> `research_investigator`
- ship -> `implementer`

It still runs normal validation and skill routing. This is intentionally the cheap path.

For moderate/complex work, the planner is a **conditional decomposer**, not the root gateway.
It inspects repository evidence, asks only consequential questions, and creates the smallest useful
dependency graph. Do not decompose merely to fill concurrency.

## Planning lifecycle for decomposed work

1. Create a root task and store the literal user requirement.
2. Inspect bounded repository context.
3. Ask a planner only when complexity warrants decomposition or unresolved consequential choices.
4. Validate the entire typed proposal before persistence.
5. Persist child tasks/dependencies/assumptions/pins atomically.
6. Route minimum sufficient skills after roles are fixed.
7. Produce short implementation specs only when the task/risk needs them.
8. Hand prepared tasks to authorization/worker lifecycle.

If planning output is malformed, invalid, or unavailable, it is a task failure/blocker; it is not
silently presented as a user question.

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

V1-09 only prepares specs. V1-10 binds write authority to the exact current task/spec: when
implementation authorization is granted, that bound spec becomes approved for execution. Changing
the spec voids active authorization. Result/code review remains a separate later phase.

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

## Roles and worker authority

Roles describe responsibility and reasoning needs; they do not themselves grant technical
permissions. Every launched worker separately receives `research`, `diagnostic`, or
`implementation` authority. Skills never expand that authority.

| Role | Reasoning floor | Responsibility | Must not infer from role alone |
| --- | --- | --- | --- |
| Orchestrator | high | decompose work, write canonical memory, coordinate, ask the user, escalate | edit code directly |
| Spec author | high | read the repo, write and revise specs | edit code, expand the user's goal |
| Spec reviewer | mid (escalates on risk) | question the spec author, approve or reject specs | rewrite the spec itself |
| Architect | high | analyze architecture and propose bounded design constraints/options | edit code, expand product intent |
| Implementer | low (with a precise spec) | implement the bounded task when orchestration grants implementation authority | that write permission exists merely because the role is implementer; redesign, broaden scope, or silently resolve consequential ambiguity |
| Code reviewer | mid (escalates on risk) | question the implementer, request changes, approve | edit code |
| Research investigator | high | investigate and report findings | shell or source-write permission merely because of the role; make design decisions |
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

Implementation mode controls **whether Agent CLI grants implementation authority**. It is not passed
to Claude Code, Codex, or any other harness as a vendor permission mode.

| Mode | Behavior |
| --- | --- |
| **autopilot** (default) | Agent CLI may grant implementation authority automatically when the current task/spec is sufficiently bounded. Moderate work requires a current spec; complex or risk-sensitive work still checkpoints. |
| **checkpoint** | Agent CLI persists a user authorization gate. No source-changing worker launches until the user approves the exact current task/spec. |
| **plan-only** | Agent CLI never grants implementation authority. Planning/spec preparation may continue, but no source-changing worker launches. |

Authorization is durable and scope-bound. The worker runtime independently verifies a current
approved authorization before accepting `modify_source=true`. A changed spec voids previous
authorization, so an earlier "go ahead" cannot authorize a materially changed scope.

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

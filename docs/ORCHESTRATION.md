# Orchestration

## Task flow

```
user goal
 → orchestrator: inspect repo, separate facts from assumptions, decompose
 → spec author: short implementation spec per subtask
 → spec reviewer: challenge spec → approved spec
 → implementer (in worktree): execute spec
 → code reviewer: challenge diff against spec → fixes
 → orchestrator: final diff to user → MR gate
```

The orchestrator assigns each subtask one role and a concise task/scope description. It does not choose skill names. A separate low-cost skill-routing stage derives normalized signals and selects policy context without expanding role authority. See [SKILL_ROUTING.md](SKILL_ROUTING.md). The orchestrator may skip the spec stage for tasks that need no code change. Research investigators can be dispatched before a design is committed.

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
- Classify every input as: explicit user requirement, repository fact, approved prior decision, assumption, or unresolved decision. Record it in memory under that class.
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
4. pass it to routing and worker launch
5. show the pin in routing/status explanations

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

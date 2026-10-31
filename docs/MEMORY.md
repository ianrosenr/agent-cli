# Memory

Shared memory belongs to the orchestration layer, not to individual worker chats. It is the canonical record of a task and must survive process restarts.

## What is canonical

| Record | Contents |
| --- | --- |
| Requirement | text, source (user / derived), timestamp, affected tasks |
| Fact | repository fact with its evidence (file, command output) |
| Assumption | stated assumption, who made it, whether it was confirmed |
| Decision | resolved question, who decided, rationale, superseded-by |
| Planning question | root task, consequential question, why it matters, pending/answered/canceled status, answer, timestamps |
| Task | goal, parent/child and dependency links, status, assigned role, explicit model/provider/harness pins |
| Spec | versioned contract per task; each version keeps its diff from the previous one and its approval state |
| Worker | role, model/provider/harness, worktree, status, last heartbeat |
| Finding | reviewer or investigator finding, severity, linked requirement, resolution |
| Blocker | what is blocked, why, who must act |
| Review | spec/code review result, round number, approver |
| Artifact | diffs, test results, reports, with links to the producing task |

Input classes from [ORCHESTRATION.md](ORCHESTRATION.md#orchestrator-rules) (requirement, fact, approved decision, assumption, unresolved decision) map directly to these records. An assumption is never stored as a fact.

## Rules

- Only the orchestrator writes canonical records. Workers return results; the orchestrator decides what to persist.
- Worker scratch reasoning does not become shared truth automatically.
- Records are append-friendly: superseding a decision or spec version keeps the old one, linked to its replacement.
- Workers receive the approved records relevant to their task, not the conversations that produced them.
- Explicit user execution pins are canonical task constraints and survive process restarts until superseded by the user.
- A pending planning question is canonical state. The next ordinary non-slash input answers it; restart must not cause orchestration to skip it.
- A material requirement change voids approval of affected specs until they are re-reviewed.
- After a restart, the orchestrator rebuilds worker state from memory and the worktrees on disk, then resumes or reports each worker.

## Storage

Canonical task state lives in:

```text
<repo>/.agent-cli/state/tasks.sqlite
```

This SQLite database is durable source-of-truth state and is git-ignored. It carries an explicit schema version and uses deterministic migrations. A migration failure must preserve the existing database; canonical task state is never silently deleted or rebuilt because analyzer/repository-cache versions changed.

Repository-derived indexes are separate rebuildable state. See [REPOSITORY_INSPECTION.md](REPOSITORY_INSPECTION.md).

Paid-spend accounting is also separate from task memory because monthly/daily limits span repositories. Its authoritative canonical ledger lives at `~/.agent-cli/state/spend.sqlite`. Task state may link concise budget events, but it is not the financial source of truth.

V1 uses local stores only; no vector database.

## Implementation

`agent_cli.state` implements this contract (V1-05):

- `TaskStore` is the only way to read or write `tasks.sqlite`. Every change appends an `events` row; superseded requirements, decisions, pins, and spec versions are kept and linked to their replacements.
- Schema migrations live in `state/migrations.py`, tracked by SQLite's `user_version`. All pending migrations run in one transaction. A corrupt, foreign, or newer database, or a failed migration, raises `TaskStateError` and leaves the file untouched.
- Execution pins are stored per task as normalized IDs and convert to the adapter `Selection` type. They are not inherited by subtasks; V1-09 explicitly propagates user-scoped all-child pins while persisting the plan.
- V1-09 adds typed planning-question state and an atomic validated-plan write so malformed planner output cannot leave a half-created child graph. `planning_questions` (schema v3) holds each question with its status, answer, linked user `Decision`, and blocker (`/cancel` marks it canceled and cancels its root task); a partial unique index allows at most one pending question per root task. `TaskStore.persist_plan` writes children, roles, dependencies, assumptions, and user pins in one transaction. `scoped_pins` keeps `all`/role-scoped user pins, each linked to the user requirement quoting the instruction, for children created later.
- `/status` renders the latest session's task tree, roles, pins, role-scoped pins, latest spec approval, and open blockers through `status_view`. With no persisted state it prints `No active task.` and creates nothing.
- The state directory contains a `.gitignore` of `*`, so it stays ignored even in repositories whose own ignore rules do not mention it.

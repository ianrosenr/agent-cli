# Memory

Shared memory belongs to the orchestration layer, not to individual worker chats. It is the canonical record of a task and must survive process restarts.

## What is canonical

| Record | Contents |
| --- | --- |
| Requirement | text, source (user / derived), timestamp, affected tasks |
| Fact | repository fact with its evidence (file, command output) |
| Assumption | stated assumption, who made it, whether it was confirmed |
| Decision | resolved question, who decided, rationale, superseded-by |
| Task | goal, parent/child and dependency links, status, assigned role |
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
- A material requirement change voids approval of affected specs until they are re-reviewed.
- After a restart, the orchestrator rebuilds worker state from memory and the worktrees on disk, then resumes or reports each worker.

## Storage

Project task state lives under `<repo>/.agent-cli/state/` and is git-ignored. V1 uses a local store only; no vector database. The format is decided in V1-05.

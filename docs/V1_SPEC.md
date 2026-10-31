# V1 Specification

The contract for Agent CLI V1. [BUILD_GUIDE.md](BUILD_GUIDE.md) explains the reasoning; this file states what V1 must do.

## Objective

A developer runs Agent CLI inside a Git repository, describes a software-engineering goal, and receives a reviewed, understandable diff while Agent CLI delegates execution through existing coding-agent harnesses and chooses the appropriate model/provider/harness for each subtask.

Reference scenario:

> Add expiration support to our cache layer without changing existing callers.

## Required capabilities

1. **CLI shell.** An interactive Python CLI that accepts natural-language goals and slash commands, including `/startup`.
2. **Configuration.** Layered config (global → project → session → task) with usable defaults. See [CONFIGURATION.md](CONFIGURATION.md).
3. **Guided setup.** `/startup` detects harnesses and configures providers, budgets, and preferences. See [STARTUP_FLOW.md](STARTUP_FLOW.md).
4. **Adapters.** Separate, stable interfaces for providers and existing coding-agent harnesses. Agent CLI is the control plane, not a coding-agent runtime. See [ARCHITECTURE.md](ARCHITECTURE.md).
5. **Shared memory and repository facts.** Persist canonical task state in project-local SQLite plus a reusable, incremental repository fact index for cheap task inspection. Canonical task state is durable; repository indexes/caches are rebuildable. See [MEMORY.md](MEMORY.md) and [REPOSITORY_INSPECTION.md](REPOSITORY_INSPECTION.md).
6. **Skills.** Layered skill selection under a context budget. See [SKILLS.md](SKILLS.md).
7. **Model registry and role floors.** Capability-scored models; each canonical role has a deterministic minimum score on its primary capability. See [ROUTING.md](ROUTING.md).
8. **Budget guardrails.** Hard paid-API limits enforced through conservative pre-call reservation and durable settlement; opaque paid harnesses require an enforceable bound. See [ROUTING.md](ROUTING.md#cost-guardrails).
9. **Spec-driven work.** Orchestrator → spec author → spec reviewer → implementer → code reviewer. See [ORCHESTRATION.md](ORCHESTRATION.md).
10. **Worktree isolation.** Each concurrent implementer works in its own Git worktree.
11. **Adversarial review.** Reviewers can question spec authors and implementers. Review rounds are capped. See [REVIEW_PIPELINE.md](REVIEW_PIPELINE.md).
12. **Provider health and fallback.** Normalized failures, cooldowns, and logged fallback. See [ROUTING.md](ROUTING.md#provider-health-and-fallback).
13. **Model discovery.** Newly seen models become inactive candidates until policy activates them.
14. **Requirement changes mid-task.** Recorded, applied to affected specs, diffed, and re-reviewed when the change is material.
15. **Visibility.** Status and diffs are shown throughout.
16. **MR gate.** No merge request is created until configured review requirements pass.

## Defaults

| Setting | Default |
| --- | --- |
| Monthly paid-API limit | $20.00 |
| Daily paid-API limit | none |
| Per-task limit | $1.00 |
| Per-call limit | $0.30 |
| Implementation mode | autopilot |
| Review before MR | true |
| Access preference | subscription → local → paid API |

## Non-goals for V1

Listed in [ENHANCEMENTS.md](ENHANCEMENTS.md). In short: no custom coding-agent harness/tool loop, broad provider/harness catalog, remote workers, rich UI, benchmark automation, learned routing, vector memory, multi-user features, semantic skill retrieval, auto-merge, or support for every Git host.

## Acceptance

V1 is done when the reference scenario runs end to end (issue V1-15):

- the repo is inspected before the user is asked anything the repo can answer
- planning uses a model at or above the spec-author floor
- implementation uses a cheaper compliant model when the spec permits
- implementation/review execution runs through at least one real external harness adapter
- work happens in a worktree
- budgets are never exceeded
- relevant skills are selected within budget
- task state survives a process restart
- the reviewer challenges the diff and fixes are applied
- a mid-task requirement change is handled
- a simulated rate limit triggers logged fallback
- the user sees a final diff, and MR creation follows the review policy

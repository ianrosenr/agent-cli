# Agent CLI — V1 Build Guide

This document is the front-to-back narrative for building Agent CLI.

It explains what the product is, what V1 should do, what ideas are borrowed from Firstmate, what should not be rebuilt unnecessarily, and how the numbered V1 issues fit together.

---

# 1. Product Goal

Agent CLI is a local-first developer orchestration and routing control plane.

The user should be able to open the CLI inside a repository and describe a software-engineering goal in natural language.

The orchestration agent should then:

1. understand the request
2. inspect repository context
3. determine what is known versus assumed
4. break work into bounded subtasks
5. generate short implementation specs
6. choose appropriate models, providers, and harnesses for each subtask
7. delegate work to isolated subagents
8. maintain shared task memory
9. review specs and code
10. enforce cost limits
11. handle provider limits and fallback
12. show status and diffs throughout
13. stop before merge-request creation unless configured review requirements have passed

The central philosophy is:

> Spend expensive reasoning where decisions matter. Use inexpensive models for well-specified execution.

### Control-plane boundary

Agent CLI does not replace coding-agent harnesses.

It coordinates them.

Existing harnesses should own low-level agent execution: tool loops, streaming, session mechanics, file-edit tools, and harness-native terminal behavior. Agent CLI should own the decisions above that layer: decomposition, role authority, skills, context, model/provider/harness routing, budgets, memory, worktrees, review, and escalation.

Pi, Codex CLI, Claude Code, and OpenCode are adapter targets. Pi is a preferred early integration candidate, but core Agent CLI must remain harness-agnostic.

Do not build a new general-purpose coding-agent runtime unless an external harness cannot satisfy a demonstrated product requirement.

---

# 2. What Firstmate Already Does Well

Firstmate is important prior art.

Agent CLI should not spend V1 effort reinventing mature concepts simply to be different.

Useful ideas to retain:

- one supervisor coordinating many workers
- isolated Git worktrees for concurrent agents
- persistent worker/task state
- worker recovery after process restarts
- multiple harness support
- event-driven supervision
- explicit authority boundaries
- worker questions routed through the supervisor
- lazy skill loading
- worker status and artifact tracking

Agent CLI is not intended to be "Firstmate rewritten in Python."

The product should differentiate itself through model intelligence, economics, specification-driven implementation, and a CLI-first experience.

---

# 3. Agent CLI's Main Differentiators

## 3.1 Automatic model intelligence

Models should not be manually assigned to static hard/medium/easy lists forever.

The system should maintain a model registry containing capability estimates such as:

- reasoning
- coding
- repository engineering
- debugging
- tool use
- architecture
- security reasoning
- latency
- context size
- cost

Eventually these scores should use recognized industry benchmarks such as SWE-bench Verified, Aider Polyglot, Terminal-Bench, and other reputable sources.

V1 only needs the registry and routing interfaces. Full benchmark automation can grow later.

## 3.2 Model, provider, and harness are separate

These are three different decisions.

Model:
the intelligence to use.

Provider:
how that model is accessed.

Harness:
the coding-agent environment that performs the work.

Example:

- task: backend implementation
- model: Model X
- provider: local vLLM
- harness: OpenCode

This separation makes provider fallback and subscription-aware routing possible.

## 3.3 Subscription-first economics

When capability is sufficient, prefer:

1. subscription-backed access
2. local models
3. paid token APIs

Paid APIs remain useful for fallback or high-value reasoning.

Default API limits:

- monthly: $20
- daily: none
- per task: $1
- per call: $0.30

These limits are hard runtime guardrails, not suggestions to an LLM.

## 3.4 Specification-driven implementation

A strong model should remove ambiguity before a cheap model implements.

Typical flow:

User request
→ orchestrator
→ spec author
→ spec reviewer
→ approved short spec
→ implementer
→ code reviewer
→ fixes
→ final diff

A cheap implementer should execute a contract, not invent architecture.

## 3.5 Role-based model floors

Task difficulty alone is not enough.

Some responsibilities require strong reasoning even when the task looks small.

Examples:

- security investigator: high reasoning floor
- research investigator: high reasoning floor
- spec author: high reasoning floor
- orchestrator: high reasoning floor
- backend implementer with precise spec: may use inexpensive model
- ordinary code reviewer: mid tier by default

These thresholds should be built in and configurable, but hidden from normal startup.

## 3.6 Review by challenge, not just commentary

Reviewers should be able to question the agents responsible for changes.

A reviewer can ask:

- Why was this change necessary?
- Which spec requirement authorizes it?
- Could this be simpler?
- What failure modes were considered?
- Why did you touch unrelated behavior?

Spec authors can be challenged the same way before implementation starts.

---

# 4. Core Agent Roles

V1 should support these roles.

## Orchestrator

Owns task decomposition, shared memory, routing, coordination, and escalation.

The orchestrator must assume nothing about implementation intent.

It should distinguish:

- explicit user requirements
- repository facts
- approved prior decisions
- assumptions
- unresolved decisions

It should inspect the repository before asking the user questions that code or documentation can answer.

## Spec Author

Uses stronger reasoning to convert a goal into a short, readable implementation spec.

The spec should include:

- objective
- affected areas/files
- required behavior
- behavior that must not change
- constraints
- edge cases
- tests
- success criteria
- scope boundaries

## Spec Reviewer

Challenges the spec for ambiguity, hidden assumptions, missing tests, security issues, and unnecessary scope.

Default model can be mid tier, with escalation for risk.

## Implementer

Executes the approved spec.

It should not:

- redesign the solution
- infer product requirements
- broaden scope
- perform unrelated refactors
- silently resolve consequential ambiguity

If blocked, it escalates.

## Code Reviewer

Compares code to the approved spec.

Default model is mid tier.

Escalate to stronger reasoning for security-sensitive or high-risk changes.

## Research Investigator

Used when the orchestrator needs facts before committing to a design.

Research tasks should never default to very weak reasoning models.

---

# 5. Implementation Modes

The user can choose how aggressively implementation agents act.

## Autopilot

Default.

Once a spec is approved, the implementer may make all changes explicitly authorized by that spec without asking before each edit.

Autopilot does not allow scope expansion.

## Checkpoint

The implementer proposes each coherent batch of changes and waits for user approval.

A coherent batch is usually one file or one tightly related set of edits, not every individual line.

## Plan-only

No files are changed.

The agent only explains the intended implementation.

---

# 6. Shared Memory

Memory belongs to the orchestration layer, not to isolated worker chats.

Persist canonical state such as:

- requirements
- decisions
- specs
- spec versions
- task relationships
- findings
- blockers
- worker state
- review results
- artifacts

Worker scratch reasoning should not automatically become shared truth.

When a user adds a new requirement after work has started:

1. record the requirement
2. determine which tasks are affected
3. update the relevant spec
4. record the spec diff
5. notify affected workers
6. re-review if the change is material

Workers should normally communicate through the orchestrator.

---

# 7. Roles, Skills, and Context Management

Roles and skills are separate.

- A role defines a worker's responsibility and authority.
- A skill provides policy, knowledge, or methods.
- Adding a skill never expands role authority.

Built-in product skills ship as independent packages under `builtins/skills/<skill-name>/SKILL.md`. Do not encode taxonomy in the filesystem.

Skill selection starts from the assigned role, then adds only skills justified by the task, approved spec, or affected repository surfaces.

Example implementer:

- role: implementer
- baseline skills: software-engineering, testing
- additional skills when relevant: interfaces, security, data, observability
- task-specific skills: framework or technology skills installed by the user/project

No skill is global to every worker.

Runtime overrides may come from:

- ~/.agent-cli/skills
- <repo>/.agent-cli/skills
- additional configured paths

Use a context/token budget. Prefer approved decisions, contracts, interfaces, and targeted source over broad repository or conversation history.

Workers may request a missing skill when newly discovered work crosses an uncovered policy domain. Dynamic skill escalation never changes the worker's role authority.

---

# 8. Provider and Harness Setup

The CLI should include a guided `/startup` flow.

It should detect installed harnesses and guide provider setup.

V1 adapter targets:

- Pi
- Codex CLI
- Claude Code
- OpenCode

Support custom providers through a stable provider adapter.

At minimum, OpenAI-compatible endpoints should be easy to add.

Credentials should not be stored directly in ordinary YAML.

Prefer environment variables or OS credential storage.

---

# 9. Cost and Budget Behavior

Defaults:

```yaml
cost:
  monthly_limit_usd: 20.00
  daily_limit_usd: null
  per_task_limit_usd: 1.00
  per_call_limit_usd: 0.30
```

When a paid call would exceed a hard limit:

1. try a subscription-backed alternative
2. try a local model
3. try a cheaper compliant API model
4. ask the user if no valid path exists

Configuration precedence:

global
→ project
→ session
→ task

The most specific setting wins.

---

# 10. Provider Health and Fallback

Temporary limits are runtime state, not model quality.

Normalize provider failures such as:

- rate limits / 429
- retry-after
- timeouts
- server errors
- auth failure
- quota exhaustion

If an access path is temporarily unavailable:

1. try the same model through another provider
2. otherwise select the closest model that still satisfies role thresholds
3. keep budget limits enforced
4. log the fallback reason

Do not repeatedly hit an endpoint already known to be in cooldown.

---

# 11. Model Discovery

New models should eventually be discovered automatically.

The discovery path is:

new model detected
→ persist as discovered
→ gather metadata
→ find benchmark evidence
→ run local evals if needed
→ estimate capability scores
→ assign confidence
→ activate only when policy allows

V1 only needs the inactive-candidate workflow.

Newly discovered models should not silently become preferred just because they are new.

---

# 12. Git Isolation

Concurrent implementation agents should use Git worktrees.

A worktree is another checkout of the same repository attached to another branch.

This lets several workers edit the same repository independently without overwriting one another.

Do not invent a custom filesystem isolation system unless Git worktrees prove insufficient.

---

# 13. Review Before Merge Requests

Review-before-MR is configurable.

Default:

```yaml
review:
  before_merge_request: true
```

Ordinary code review should use a mid-tier model.

Escalate when risk warrants it, such as:

- authentication
- authorization
- cryptography
- destructive operations
- important migrations
- public API changes
- repeated failures
- unresolved reviewer/implementer disagreement
- significant deviation from spec

Review loops should be bounded. If agents cannot converge, escalate to the orchestrator.

---

# 14. Startup Experience

`/startup` should be simple enough for a new user.

Configure:

- harnesses
- providers
- authentication
- local model endpoints
- monthly/per-task/per-call budgets
- max worker concurrency
- skill directories
- implementation mode
- review preference
- Git/worktree behavior

Do not expose advanced model thresholds during normal startup.

Defaults should make the tool immediately usable.

---

# 15. Build Order

The repository issues are intentionally ordered.

## Phase 1 — Foundation

### V1-01
Docs and agent contracts.

### V1-02
Runnable Python CLI.

### V1-03
Config and `/startup`.

### V1-04
Provider/harness adapter interfaces that wrap existing execution systems.

At this point, the CLI exists but does not yet orchestrate real work.

## Phase 2 — Intelligence foundation

### V1-05
Persistent task state, shared memory, and repository fact index.

### V1-06
Role-aware, low-cost skill discovery, routing, and context budgeting.

### V1-07
Model registry and role thresholds.

### V1-08
Budget guardrails and access preference.

At this point, the system understands how work should be represented and routed.

## Phase 3 — Orchestration

### V1-09
Planner and implementation specs.

### V1-10
Spec review and bounded implementers.

### V1-11
Git worktrees and worker lifecycle.

### V1-12
Adversarial code review.

At this point, the system can perform structured delegated coding work.

## Phase 4 — Resilience and automation

### V1-13
Provider health and rate-limit fallback.

### V1-14
New-model discovery.

## Phase 5 — Usable V1

### V1-15
End-to-end real delegation flow.

The target scenario is:

User asks for a backend change
→ orchestrator inspects repo
→ strong model creates spec
→ spec gets reviewed
→ cheap implementer works in worktree
→ mid-tier reviewer challenges implementation
→ fixes happen
→ user sees final diff
→ MR creation is gated by configured review policy

When V1-15 works reliably, the tool is useful enough for normal development.

---

# 16. What Not to Build Yet

Do not block V1 on:

- dozens of providers
- dozens of harnesses
- a custom coding-agent runtime/tool loop
- remote distributed workers
- sophisticated UI
- perfect benchmark aggregation
- learned personalized routing
- vector-database memory
- enterprise multi-user behavior
- full semantic skill retrieval
- automatic PR merging
- every Git host
- autonomous registry retraining

These belong in enhancement issues.

---

# 17. Definition of V1 Success

V1 is successful when a developer can enter a repository, start Agent CLI, and say something like:

> Add expiration support to our cache layer without changing existing callers.

Agent CLI should be able to:

- inspect the repo
- determine relevant context
- produce a precise implementation spec
- route planning to an appropriately strong model
- route implementation to a cheaper model when safe
- operate in an isolated worktree
- respect configured budgets
- select relevant skills
- retain task state
- review the resulting diff
- challenge the implementer when needed
- handle requirement changes during execution
- recover from provider throttling
- present a final understandable diff

The user should not need to manually choose a model for every subtask.

That is the core promise of Agent CLI V1.

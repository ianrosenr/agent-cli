# Architecture

## Components

```
                         user
                          │
                          ▼
                ┌──────────────────┐
                │ Supervisor       │
                │ continuous chat  │
                └────────┬─────────┘
                         │ answer | scout | ship + complexity
              ┌──────────┼──────────┐
              ▼          ▼          ▼
           answer      scout       ship
                         │           │
                         └─────┬─────┘
                               ▼
                    optional decomposition
                    only when complexity needs it
                               │
                               ▼
                    role + skill selection
                               │
                               ▼
                     Dispatch router
                 model · provider · harness
                 cost · latency · capability
                               │
                               ▼
                     Worker manager
                    isolated worktrees
                               │
              ┌────────────────┴────────────────┐
     Harness adapter                    Provider adapter
   Claude/Codex/Pi/OpenCode          subscription/local/API
```

| Component | Owns | Does not own |
| --- | --- | --- |
| CLI shell | input, slash commands, status/diff display | decisions about work |
| Supervisor | conversational context, project resolution, answer/scout/ship intake, complexity estimate, coordination, escalation | coding-agent runtime, direct repository edits |
| Optional decomposer | multi-task decomposition only when useful | mandatory planning for every task |
| Memory store | canonical task/work/decision state | ordinary chat history or worker scratch reasoning |
| Repository fact index | cached structural repository facts and bounded inspection | semantic decisions or worker authority |
| Role + skill selector | authority and minimum sufficient skills | model/provider/harness choice |
| Dispatch router | minimum-sufficient model + provider + harness under capability, cost, latency, health and pins | task scope |
| Model registry | model capability evidence and active/candidate policy | live provider health |
| Budget ledger | hard paid-spend admission/reservation/settlement | model quality |
| Worker manager | isolated worker/worktree lifecycle and recovery | deciding what the user meant |
| Harness adapter | translating normalized worker requests to an existing coding-agent harness | reimplementing the harness tool loop |
| Provider adapter | bounded direct model calls and usage/errors | agent behavior |

## Control plane, not coding-agent harness

Agent CLI follows Firstmate's strongest architectural lesson: one supervisor coordinates existing
agent harnesses instead of rebuilding a coding-agent runtime.

The supervisor owns:

- continuous conversation and follow-up resolution
- answer vs scout vs ship intake
- project/repository context
- complexity estimation
- conditional decomposition
- role authority and selected skills
- model/provider/harness allocation
- budgets and provider health
- canonical task state
- worktree assignment
- review, escalation, and synthesis

Harnesses own tool loops, file editing, terminal interaction, streaming, and worker-local reasoning.

A simple request should not pay for architecture it does not need. An ordinary answer is returned
directly. A trivial scout/ship becomes one worker task without a decomposition-model call. Complex
work may invoke a decomposer and multiple independent workers.

## Model, provider, harness

These remain independent:

- **Model** — the intelligence.
- **Provider** — how Agent CLI reaches it: subscription, local, or API.
- **Harness** — the coding-agent runtime that executes delegated work.

The worker/session backend and workspace provider are also separate lifecycle concerns as V1-11
lands; a harness should not become an architectural dependency merely because it currently owns a
terminal process.

## Boundaries

- **Trust.** Repository content, model output, and tool output are untrusted input.
- **Authority.** The literal user request remains authoritative. Supervisor restatements, context,
  scout findings, and worker output clarify evidence but cannot silently widen scope.
- **State.** Ordinary conversation is transient. Scout/ship work, decisions, workers, reviews, and
  artifacts are canonical durable state.
- **Filesystem.** The supervisor is read-only over project files. Workers write only inside isolated
  worktrees/workspaces.
- **Spend.** Paid calls require hard budget admission. Complexity/capability routing should start
  with the cheapest sufficient option and escalate only when needed.
- **Planning.** Planning/decomposition is a conditional supervisor tool, not a mandatory gateway.
- **Failure.** Worker/provider failure is contained and returned to the supervisor for retry,
  fallback, or escalation.
- **Presentation.** Internal routing/decomposition chatter is hidden at concise verbosity.

## Product and runtime layout

```
builtins/skills/                     shipped default skill packages
builtins/policies/                   shipped routing/skill policy
builtins/models/                     model-registry seed
~/.agent-cli/                        user config and overrides
~/.agent-cli/state/spend.sqlite      global spend ledger
<repo>/.agent-cli/                   project config + canonical task state
<repo>/.agent-cli/worktrees/         isolated worker worktrees
```

## Prior art: Firstmate

Firstmate is the main architectural inspiration. Agent CLI intentionally keeps its proven shape:

- one conversational supervisor
- answer existing questions without manufacturing work
- scout/investigation work separated from ship/change work
- isolated workers and Git worktrees
- multiple harness support
- durable task/worker recovery
- event-driven supervision
- explicit authority boundaries
- worker questions/escalations through the supervisor
- lazy/conditional skills

Agent CLI's differentiation is **intelligence allocation**. For each real task it estimates
complexity, chooses minimum-sufficient skills/model/provider/harness under cost and availability,
records the routing decision, and can escalate when cheap execution is insufficient. The long-term
product is an adaptive compute scheduler for software engineering, not a Python clone of Firstmate.

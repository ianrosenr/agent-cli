# Architecture

## Components

```
                ┌──────────────────────────────┐
  user ───────► │ CLI shell (REPL, /commands)  │
                └──────────────┬───────────────┘
                               │
                ┌──────────────▼───────────────┐
                │ Orchestrator                 │──► Memory store
                │  planner · coordinator       │──► Role + skill selector
                └──────────────┬───────────────┘
                               │ task + spec + skills
                ┌──────────────▼───────────────┐
                │ Router                       │──► Model registry
                │  floors · budget · health    │──► Budget ledger
                └──────────────┬───────────────┘──► Provider health
                               │ (model, provider, harness)
                ┌──────────────▼───────────────┐
                │ Worker manager               │──► Git worktrees
                └──────────────┬───────────────┘
                               │
              ┌────────────────┴────────────────┐
     Harness adapter                    Provider adapter
   (Claude Code, Codex CLI, Pi, OpenCode)   (subscription, local, API)
```

| Component | Owns | Does not own |
| --- | --- | --- |
| CLI shell | input, slash commands, status and diff display | decisions about work |
| Orchestrator | decomposition, canonical planning state, coordination, escalation, user questions | direct code edits, coding-agent runtime, model choice, spend enforcement |
| Memory store | canonical task state ([MEMORY.md](MEMORY.md)) | worker scratch reasoning |
| Repository fact index | cached structural repository facts and task inspection ([REPOSITORY_INSPECTION.md](REPOSITORY_INSPECTION.md)) | semantic decisions or worker authority |
| Role + skill selector | assigns role authority and minimum sufficient skills ([SKILLS.md](SKILLS.md), [SKILL_ROUTING.md](SKILL_ROUTING.md)) | skill content or model choice |
| Router | choosing model + provider + harness ([ROUTING.md](ROUTING.md)) | task content |
| Model registry | model capability scores, active/candidate policy | live provider health or provider topology |
| Budget ledger | global spend, atomic reservations, settlement, paid-call allow/deny | model capability or harness execution |
| Provider health | cooldowns and normalized failure state | model quality |
| Worker manager | worker lifecycle, worktrees, recovery | what workers should do |
| Harness adapter | translating Agent CLI worker requests to an existing coding-agent harness ([ADAPTERS.md](ADAPTERS.md)) | the coding-agent tool loop/runtime itself |
| Provider adapter | sending requests to a model and reporting usage and errors ([ADAPTERS.md](ADAPTERS.md)) | agent behavior |

## Control plane, not coding-agent harness

Agent CLI owns orchestration. It MUST NOT grow its own general-purpose coding-agent runtime.

Existing harnesses already solve low-level execution concerns such as tool loops, model streaming, harness sessions, terminal interaction, and file-edit tooling. Agent CLI should integrate with those systems through adapters.

Agent CLI owns:

- decomposition and coordination
- role authority and selected skills
- repository/context selection
- model/provider/harness routing
- budgets and provider health
- canonical task state
- worktree assignment
- review and escalation policy

Harnesses own the mechanics of executing a coding agent.

Claude Code, Codex CLI, Pi, and OpenCode are examples of external harnesses. Claude Code and Codex CLI are the first two real integrations; Pi is the next portability test. None is an architectural dependency.

## Model, provider, harness

These are three independent choices:

- **Model** — the intelligence (e.g. a specific model version).
- **Provider** — the access path to that model (subscription, local vLLM, paid API).
- **Harness** — the existing coding-agent environment that executes the work (Pi, Codex CLI, Claude Code, OpenCode).

Keeping them separate allows the same model to be reached through another provider during an outage, and lets routing prefer subscription access.

## Boundaries

- **Trust.** Repository content, model output, and tool output are untrusted input. Credentials come from environment variables or the OS credential store, never from ordinary YAML.
- **State.** Only the orchestration layer writes canonical memory. Untrusted planner/worker model output is validated before persistence; workers return results and do not write shared truth directly.
- **Filesystem.** Each implementer writes only inside its own worktree. The user's checkout is changed only when the user accepts a final diff.
- **Spend.** Paid calls require a conservative upper-bound estimate and atomic ledger reservation before dispatch. An LLM or plain approval cannot override hard ceilings. Opaque paid harnesses are eligible only when their integration exposes an enforceable bound.
- **Planning.** Root natural-language input may use direct provider reasoning for decomposition/spec authoring, but planning never launches a coding harness or edits repository files. Paid planning calls use the same budget guard as every other paid provider call.
- **Failure.** A worker or provider failure is contained to that task and surfaces to the orchestrator. It does not crash the session.
- **Presentation.** `agent_cli.ui` owns terminal rendering through Rich: one console, semantic styles (`agent_cli/ui/styles.py`), and message, table, and home-screen helpers. prompt_toolkit owns input. Services and status views return Rich renderables built with those helpers, never markup strings or raw ANSI; markup is disabled, so user and model text is shown verbatim. Every styled state is also named in words, so output stays readable under `NO_COLOR` and in pipes.

## Product and runtime layout

```
builtins/skills/                     shipped default skill packages
builtins/policies/                   shipped default skill-routing policy
builtins/models/                     shipped model-registry seed
~/.agent-cli/                        user config, skill overrides, model registry
~/.agent-cli/state/spend.sqlite      global canonical paid-spend ledger
<repo>/.agent-cli/                   project config, skill overrides, policies, task state
<repo>/.agent-cli/worktrees/         worker worktrees (git-ignored)
```

Built-ins are product assets. User and project paths override them at runtime without modifying shipped files.

## Prior art: Firstmate

Firstmate already solves supervisor/worker coordination well. Agent CLI keeps these concepts rather than redesigning them:

- one supervisor coordinating many workers
- Git worktrees for concurrent workers
- persistent worker/task state and recovery after restart
- multiple harness support
- event-driven supervision
- explicit authority boundaries
- worker questions routed through the supervisor
- lazy skill loading
- worker status and artifact tracking

Agent CLI is not "Firstmate rewritten in Python." Its differences are model intelligence (registry and role floors), subscription-first economics with hard budgets, spec-driven implementation, adversarial review, and a CLI-first experience. Do not change a borrowed concept unless a V1 requirement needs the change.

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
   (Codex CLI, Claude Code, OpenCode)   (subscription, local, API)
```

| Component | Owns | Does not own |
| --- | --- | --- |
| CLI shell | input, slash commands, status and diff display | decisions about work |
| Orchestrator | decomposition, coordination, escalation, user questions | model choice, spend enforcement |
| Memory store | canonical task state ([MEMORY.md](MEMORY.md)) | worker scratch reasoning |
| Repository fact index | cached structural repository facts and task inspection ([REPOSITORY_INSPECTION.md](REPOSITORY_INSPECTION.md)) | semantic decisions or worker authority |
| Role + skill selector | assigns role authority and minimum sufficient skills ([SKILLS.md](SKILLS.md), [SKILL_ROUTING.md](SKILL_ROUTING.md)) | skill content or model choice |
| Router | choosing model + provider + harness ([ROUTING.md](ROUTING.md)) | task content |
| Model registry | capability scores, tiers, candidate status | live provider health |
| Budget ledger | recorded spend; allow/deny of paid calls | routing preference |
| Provider health | cooldowns and normalized failure state | model quality |
| Worker manager | worker lifecycle, worktrees, recovery | what workers should do |
| Harness adapter | running a coding agent with a prompt, skills, and a working directory | model access |
| Provider adapter | sending requests to a model and reporting usage and errors | agent behavior |

## Model, provider, harness

These are three independent choices:

- **Model** — the intelligence (e.g. a specific model version).
- **Provider** — the access path to that model (subscription, local vLLM, paid API).
- **Harness** — the coding-agent environment that does the work (Codex CLI, Claude Code, OpenCode).

Keeping them separate allows the same model to be reached through another provider during an outage, and lets routing prefer subscription access.

## Boundaries

- **Trust.** Repository content, model output, and tool output are untrusted input. Credentials come from environment variables or the OS credential store, never from ordinary YAML.
- **State.** Only the orchestrator writes canonical memory. Workers return results; the orchestrator decides what becomes shared truth.
- **Filesystem.** Each implementer writes only inside its own worktree. The user's checkout is changed only when the user accepts a final diff.
- **Spend.** The budget ledger is consulted in code before every paid call. An LLM cannot override it.
- **Failure.** A worker or provider failure is contained to that task and surfaces to the orchestrator. It does not crash the session.

## Product and runtime layout

```
builtins/skills/                     shipped default skill packages
builtins/policies/                   shipped default routing policy
~/.agent-cli/                        user config, skill overrides, model registry, budget ledger
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

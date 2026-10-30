# Skills

Users can install many skills. Each worker receives only a curated working set that fits a context budget.

## Layers

| Layer | Purpose | Example (backend worker) |
| --- | --- | --- |
| 1. Core | baseline for the worker's task kind | software-engineering, implementation, testing |
| 2. Role/domain | rules for the area being touched | interfaces, data |
| 3. Task-specific | tools and frameworks named by the task | FastAPI, OAuth, PostgreSQL |

Core skills come from `task_defaults` in the routing policy. No skill is automatically global to every worker.

## Locations

Searched in this order; a later location overrides a same-named skill from an earlier one:

1. `~/.agent-cli/skills/`
2. `<repo>/.agent-cli/skills/`
3. additional paths from `skills.paths` in config

## Skill format

A skill bundle is a directory containing a root `SKILL.md` and optional specialist files, e.g. [.agent-cli/skills/software-engineering/](../.agent-cli/skills/software-engineering/). Every file starts with YAML front matter:

```yaml
---
name: architecture          # kebab-case; matches <name>.md (root SKILL.md exempt)
description: Rules for durable system design and architecture decisions.  # ≤120 chars
version: 1.0.0              # semver
---
```

Authoring rules are in [creating-a-new-skill.md](../.agent-cli/skills/software-engineering/creating-a-new-skill.md). Skill bodies contain policy only; routing lives in the policy file.

## Activation modes

Set per skill in config (`skills.activation`):

| Mode | Behavior |
| --- | --- |
| `always` | included for every worker |
| `auto` (default) | included when the routing policy selects it |
| `explicit` | included only when the user or orchestrator names it |
| `disabled` | never included |

## Selection

[.agent-cli/policies/skill-routing.yaml](../.agent-cli/policies/skill-routing.yaml) drives selection:

1. Start with `task_defaults[task_kind]`.
2. Add skills whose `signals` match the task, the approved spec, or the affected repository surfaces.
3. Add `always` skills; remove `disabled` skills.
4. Fit the set to the context budget (below).
5. Record the selected skills and the reason for each beyond the default (`audit`).

With `dynamic_escalation: true`, a worker that finds work crossing an uncovered policy domain asks the orchestrator for the missing skill. The request and its reason are recorded.

V1 matches on metadata and signals. Semantic ranking is a later enhancement.

## Context budget

Each worker has a token budget for skills (`skills.context_budget_tokens`). When the selected set exceeds it:

1. Keep core skills.
2. Keep skills matched by the approved spec over skills matched only by repository surfaces.
3. Drop task-specific skills with the weakest signal match first.
4. If core skills alone exceed the budget, fail loudly. Do not truncate skill text.

Dropped skills are recorded so the selection can be audited.

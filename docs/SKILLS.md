# Skills

Agent CLI separates **roles** from **skills**.

- A role defines a worker's responsibility and authority.
- A skill provides policy, knowledge, or methods relevant to the assigned work.
- Adding a skill never expands role authority.

## Built-ins and overrides

Built-in product skills ship under:

`builtins/skills/<skill-name>/SKILL.md`

Runtime override locations are searched in this precedence order:

1. built-in skills
2. `~/.agent-cli/skills/`
3. `<repo>/.agent-cli/skills/`
4. additional configured paths

Later locations may replace a same-named skill. Built-in files are never modified by overrides.

## Skill package

One skill is one top-level package:

```text
builtins/skills/security/
└── SKILL.md
```

A skill is not nested under a taxonomy such as `software-engineering/`. Taxonomy belongs to routing metadata, not the filesystem.

Every `SKILL.md` begins with exactly:

```yaml
---
name: security
description: Rules for trust boundaries, input validation, authorization, secrets, and secure execution.
version: 1.0.0
---
```

Metadata rules are defined by the `creating-a-new-skill` skill.

## Selection

`builtins/policies/skill-routing.yaml` is the default routing contract.

Selection uses:

- assigned role
- task
- approved specification
- affected repository surfaces

The flow is:

1. Start with `role_defaults[role]`.
2. Add skills whose signals match the work.
3. Apply configured activation/override policy.
4. Fit the selected skills to the context budget.
5. Record selected skills and selection reasons.

No skill is global to every worker.

With dynamic escalation enabled, a worker may request missing policy when newly discovered work crosses an uncovered skill domain. The orchestrator may add that skill without changing the worker's role authority.

## Context budget

Every token supplied to a worker must define the task, describe relevant system state, constrain an authorized decision, or provide information needed for that decision.

Prefer approved decisions, contracts, interfaces, and targeted source over broad repository or conversation history.

If selected optional skills exceed the configured budget, drop the weakest optional matches first. Never truncate skill text.

## Activation

Configuration may set a skill to:

- `auto` — selected by routing
- `explicit` — selected only when named
- `disabled` — unavailable
- `always` — included regardless of signals

`always` is a user-configurable override, not a built-in assumption.

## Audit

Routing records:

- the final selected skill set
- why non-default skills were selected
- dynamic skill escalations and their reasons

Routing evaluation and worker-behavior evaluation are separate concerns.

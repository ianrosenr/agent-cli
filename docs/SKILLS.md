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

The only V1 metadata keys are `name`, `description`, and `version`. Names are lowercase kebab-case and match the containing directory; descriptions are one plain-text line of at most 120 characters; versions are semantic versions.

Metadata rules are defined by the `creating-a-new-skill` skill.

## Initial built-ins

V1-06 introduces the initial independent built-in packages:

- `software-engineering`
- `testing`
- `specification`
- `review`
- `architecture`
- `research`
- `security`
- `interfaces`
- `data`
- `observability`
- `creating-a-new-skill`
- `skill-evaluation`

Type-safety rules belong to `software-engineering`; there is no separate type-safety skill. OpenTelemetry-specific production policy belongs to `observability`. Each normative rule has one owner.

## Selection

`builtins/policies/skill-routing.yaml` is the default routing contract. The end-to-end classifier and selection flow is documented in [SKILL_ROUTING.md](SKILL_ROUTING.md).

The orchestrator assigns role and subtask scope. Deterministic repository inspection always participates; an injected cheap semantic classifier may add normalized signals when available. Deterministic code maps the resulting signals to skills.

V1-06 works without a production classifier. V1-07 later chooses the concrete classifier model/provider.

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

Before a concrete worker tokenizer is known, V1-06 uses one deterministic, versioned conservative token estimator consistently for discovery, budgeting, telemetry, and `/skills` display.

## Activation

Configuration may set a skill to:

- `auto` — selected by routing
- `explicit` — selected only when named
- `disabled` — unavailable
- `always` — included regardless of signals

`always` is a user-configurable override, not a built-in assumption.

Activation comes from the layered `skills.activation` config. `explicit` gates only signal routing: a role's required defaults still load. Disabling a role's required default is an error rather than a silent weakening of that role's policy.

`/skills` shows the effective catalog (source, version, activation, estimated tokens) and, when task state exists, each task's latest selection with reasons, dropped skills, and signals. It never prints skill bodies.

## Audit

Routing records:

- the final selected skill set
- why non-default skills were selected
- dynamic skill escalations and their reasons

Routing evaluation and worker-behavior evaluation are separate concerns.

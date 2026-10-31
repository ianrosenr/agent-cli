# Routing

This document covers model/provider/harness routing. Skill routing is a separate low-cost stage documented in [SKILL_ROUTING.md](SKILL_ROUTING.md).

The router turns `(role, task, risk)` into `(model, provider, harness)`.

## Explicit user pins

Before automatic model classification, check for explicit user execution constraints attached to the task/subtask.

A specific model pin:

- selects that model directly instead of running normal capability-based model selection
- may intentionally select a model below the normal role floor
- is logged as a user override
- must never silently fall back to a different model

Hard runtime constraints still apply: budget ceilings, provider readiness, context limits, and harness compatibility.

If the requested model cannot be executed, ask the user rather than substituting another model.

A user may explicitly pin a runtime-reachable model that has no active registry score. Treat it as an explicit unscored override: do not invent scores and do not make it automatically eligible for other tasks.

When only the model is pinned, provider and harness selection may still optimize among compatible paths for that same model.

## Selection order


1. **User pin.** If a specific model is pinned for this scope, select it and skip normal capability classification/role-floor filtering for model choice.
2. **Eligible models.** Otherwise, use active registry models whose role-relevant capability score meets the configured role floor and whose runtime constraints fit the task.
3. **Healthy access paths.** For the selected/eligible model, providers not in cooldown.
4. **Budget check.** Drop paid paths that would exceed any hard limit.
5. **Preference.** Prefer subscription → local → paid API. Within a class, prefer the cheapest compatible path.
6. **Harness.** Pick a configured external harness whose reported capabilities support the chosen task/model/provider combination.
7. **No path.** If nothing remains, ask the user. Never silently substitute a different pinned model.

Every decision is logged with the reasons that ruled out other candidates.

Harness capabilities come from adapters. Core routing must not contain vendor-specific Pi, Codex, Claude Code, or OpenCode logic. A harness may constrain model/provider combinations; those constraints are adapter data, not orchestration policy.

## Model registry

The built-in seed lives at:

```text
builtins/models/registry.yaml
```

It describes model capability facts only. Runtime provider/access paths come from provider adapters, so the same canonical model may be available through subscription, local, or API access without duplicating its scores.

Each entry includes:

- canonical model ID and optional aliases
- status: `active` or `candidate`
- capability scores: reasoning, coding, repository engineering, debugging, tool use, architecture, security reasoning
- latency: `fast | medium | slow`
- coarse cost hint: `free | low | medium | high`
- confidence: `low | medium | high`
- concise score/evidence sources

Capability scores are integers from 1 through 5:

```text
1 = weak
2 = usable for simple/bounded work
3 = competent
4 = strong
5 = exceptional
```

Do not infer scores from model names, vendors, size, latency, or price. Do not fabricate real-model scores merely to populate the seed. A small or initially empty real-model seed is valid until evidence-backed entries are available.

The coarse cost category is a routing hint, not exact provider pricing or the V1-08 spend ledger.

Candidate or unknown models are never automatically promoted because a provider reports them.

A provider path is matched to a registry entry when the provider reports the model's canonical ID or one of its aliases (case-insensitively). Paths whose access kind is unknown or absent from `access.preference` are never used.

## Role floors

Named floors map exactly to numeric minimum scores:

```text
low  = 2
mid  = 3
high = 4
```

A floor applies to the role's primary capability; it is not an opaque global model grade.

| Role | Primary capability | Default floor |
| --- | --- | --- |
| Orchestrator | reasoning | high |
| Spec author | reasoning | high |
| Spec reviewer | reasoning | mid |
| Architect | architecture | high |
| Implementer | coding | low |
| Code reviewer | coding | mid |
| Research investigator | reasoning | high |
| Security investigator | security reasoning | high |
| Skill author | reasoning | high |
| Skill evaluator | reasoning | high |

Floors are configurable under `routing.role_floors` and hidden from normal `/startup`.

After floor filtering, V1-07 ranks candidates deterministically by primary capability, a role-relevant secondary capability, confidence, latency, coarse cost, then canonical model ID. Later budget, provider-health, access-path, and harness constraints may remove or reorder candidates in the full routing pipeline.

**Risk escalation** may raise an affected reviewer's floor to `high` for authentication, authorization, cryptography, destructive operations, important migrations, public API changes, repeated failures, unresolved reviewer/implementer disagreement, or significant deviation from spec.

## Skill-classifier model selection

The V1-06 semantic skill classifier is optional. Agent CLI does not require a special "System-1 provider."

Advanced config supports behavior equivalent to:

```yaml
routing:
  classifier:
    mode: auto                 # auto | selected | disabled
    selected_model: null
```

The built-in classifier floor is intentionally low:

```text
primary capability = reasoning
minimum score = 2
```

In `auto` mode, only active models with reasoning >= 2, coarse cost `free` or `low`, and at least one READY provider path are eligible. Rank them by lower cost, lower latency, higher reasoning, higher confidence, then canonical model ID. Apply the configured access preference for provider choice.

If none qualify, V1-06 deterministic routing remains the normal fallback. Do not silently use a medium/high-cost model solely to improve skill routing.

In `selected` mode, use the exact user-selected classifier model if a READY compatible path exists. Do not silently substitute another model. An explicit selected model may be expensive or unscored because the user chose it.

In `disabled` mode, never invoke a semantic classifier.

When no low-cost classifier is available, `/models` or other management surfaces may recommend configuring one, but normal orchestration must continue without it.

## Cost guardrails

Defaults:

```yaml
cost:
  monthly_limit_usd: 20.00
  daily_limit_usd: null      # no daily limit
  per_task_limit_usd: 1.00
  per_call_limit_usd: 0.30
```

These are hard ceilings, not approval thresholds. A model response or plain user approval cannot bypass them.

Paid spend applies to `api` access paths. Subscription and local access consume $0 in the V1 paid-API ledger.

The authoritative spend ledger is global:

```text
~/.agent-cli/state/spend.sqlite
```

Monthly and daily periods use UTC calendar boundaries. The per-task ceiling applies to the root user task and all descendant subtasks combined.

Ledger arithmetic uses integer micro-dollars rather than floating-point dollars:

```text
1 USD = 1,000,000 usd_micros
```

Before a paid call, Agent CLI must have a conservative upper-bound cost estimate. Unknown pricing, unbounded output, or an unenforceable paid opaque-harness execution makes that path budget-ineligible. Do not guess pricing from the model registry's coarse cost category.

Admission and reservation are atomic across processes. Active `reserved` and `dispatched` amounts consume headroom until settled or safely released.

Settlement source precedence is:

```text
provider-reported Usage.cost_usd
→ reliable token usage + the same versioned pricing source
→ reserved conservative upper bound
```

Unknown actual usage is never treated as zero.

For automatic routing, V1-08 consumes all V1-07 model/provider candidate paths rather than choosing the strongest model first. Remove over-budget/unknown-price API paths, then apply `access.preference`. Under the default preference, a compliant subscription model may beat a stronger paid API model. Within API access, prefer lower reserved cost before capability rank.

Candidate ranking does not reserve spend. Reservation happens only immediately before the selected paid invocation/launch boundary.

If no path is budget-compliant, require user action: choose another path/model, reduce the bound/scope, or explicitly change budget configuration. Do not offer a one-off approval that bypasses the ceiling.

Explicit model/provider pins never bypass hard budgets.

External harnesses may make internal calls Agent CLI cannot observe. A paid opaque harness path is hard-budget-safe only when its integration exposes an enforceable upper bound or equivalent budget control that can be reserved before launch. Agent CLI must not claim per-internal-call enforcement otherwise.

## Provider health and fallback

Provider failures are normalized into:

| Kind | Examples | Handling |
| --- | --- | --- |
| `rate_limited` | HTTP 429 | cooldown; honor `retry-after` when present |
| `timeout` | request or stream timeout | bounded retry, then cooldown |
| `server_error` | 5xx | bounded retry, then cooldown |
| `auth_failed` | 401/403 | disable path until reconfigured; tell the user |
| `quota_exhausted` | subscription or credit limit | cooldown until the reset time, if known |

Temporary limits are runtime state. They never change a model's capability scores.

On failure:

1. Try the same model through another provider.
2. Otherwise choose the closest model that still meets the role floor.
3. Keep budget limits enforced.
4. Log the fallback reason.

Do not call an endpoint already known to be in cooldown.

## Model discovery

V1-14 owns persistence and ingestion of newly seen models as candidates.

V1-07 may expose registry APIs needed by discovery, but it does not scan provider catalogs on a schedule, auto-score unknown models, or auto-activate them. A new model never becomes preferred just because it is new.

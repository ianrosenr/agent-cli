# Routing

This document covers model/provider/harness routing. Skill routing is a separate low-cost stage documented in [SKILL_ROUTING.md](SKILL_ROUTING.md).

The router turns `(role, task, risk)` into `(model, provider, harness)`.

## Selection order

1. **Eligible models.** Active registry models whose tier meets the role floor and whose capabilities fit the task (e.g. context size).
2. **Healthy access paths.** For each model, providers not in cooldown.
3. **Budget check.** Drop paid paths that would exceed any hard limit.
4. **Preference.** Prefer subscription → local → paid API. Within a class, prefer the cheapest path that meets the floor.
5. **Harness.** Pick a configured external harness whose reported capabilities support the chosen task/model/provider combination.
6. **No path.** If nothing remains, ask the user. Never silently drop below a role floor.

Every decision is logged with the reasons that ruled out other candidates.

Harness capabilities come from adapters. Core routing must not contain vendor-specific Pi, Codex, Claude Code, or OpenCode logic. A harness may constrain model/provider combinations; those constraints are adapter data, not orchestration policy.

## Model registry

Each model entry holds capability estimates:

- reasoning, coding, repository engineering, debugging, tool use, architecture, security reasoning
- latency, context size, cost
- tier (`low` / `mid` / `high`), derived from scores
- status: `candidate` or `active`
- score sources and confidence

Scores should eventually come from recognized benchmarks (SWE-bench Verified, Aider Polyglot, Terminal-Bench, and similar). V1 needs only the registry and routing interfaces with a hand-maintained seed. Static hard/medium/easy lists are not the long-term design.

## Role floors

| Role | Default floor |
| --- | --- |
| Orchestrator | high |
| Spec author | high |
| Research investigator | high |
| Security investigator | high |
| Spec reviewer | mid |
| Code reviewer | mid |
| Implementer | low, if the spec is precise |

Floors are built in and configurable under `routing.role_floors` ([CONFIGURATION.md](CONFIGURATION.md)). They are not shown during normal `/startup`.

**Risk escalation** raises a reviewer's floor to `high` for: authentication, authorization, cryptography, destructive operations, important migrations, public API changes, repeated failures, unresolved reviewer/implementer disagreement, or significant deviation from spec.

## Cost guardrails

Defaults:

```yaml
cost:
  monthly_limit_usd: 20.00
  daily_limit_usd: null      # no daily limit
  per_task_limit_usd: 1.00
  per_call_limit_usd: 0.30
```

- Limits apply to paid token APIs. Subscription and local access do not count against them.
- The budget ledger checks the estimated cost before every paid call and records actual cost after. This is enforced in code, not by prompting.
- If a paid call would exceed a limit, try in order: subscription alternative → local model → cheaper compliant API model → ask the user.
- A compliant alternative must still meet the role floor.

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

```
new model detected → persist as candidate → gather metadata
 → find benchmark evidence → run local evals if needed
 → estimate scores + confidence → activate only when policy allows
```

V1 implements only the candidate workflow. A new model never becomes preferred just because it is new.

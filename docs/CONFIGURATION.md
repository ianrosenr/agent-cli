# Configuration

## Files and precedence

```
global   ~/.agent-cli/config.yaml
project  <repo>/.agent-cli/config.yaml
session  set in the CLI for the current session
task     set by the orchestrator or user for one task
```

The most specific layer wins: global → project → session → task. A key absent from a layer inherits from the layer above. Defaults apply when no layer sets a key.

Cost limits only tighten. Each layer after global may **lower** a cost limit but may not raise it above, or remove (`null`), the limit in effect from the layers above it: project ≤ global, session ≤ project, task ≤ session. Where a layer does not set a limit, the nearest layer above that does applies, ending at the built-in default. Only global config may raise or remove limits.

Cost limits must also agree with each other, where `null` means no limit:

- per-call ≤ per-task ≤ monthly
- per-call ≤ daily ≤ monthly

Per-task and daily limits are not compared, because one task can span several days. Both rules are enforced in code when configuration is resolved; errors name the layer that introduced the conflict.

Session and task layers are in-memory override mappings with the same schema as the files.

Cost ceilings are hard runtime limits. A plain approval does not bypass them. Monthly/daily accounting is global, per-task accounting applies to the root user task tree, and paid API calls require a conservative pre-call cost bound. Spend accounting itself lives in `~/.agent-cli/state/spend.sqlite`; configuration remains in the ordinary layered config above.

## Defaults

```yaml
cost:
  monthly_limit_usd: 20.00
  daily_limit_usd: null          # no daily limit
  per_task_limit_usd: 1.00
  per_call_limit_usd: 0.30

access:
  preference: [subscription, local, api]

implementation:
  mode: autopilot                # autopilot | checkpoint | plan-only

review:
  before_merge_request: true
  max_rounds: 3                  # then escalate to the orchestrator

workers:
  max_concurrency: 3                # valid range: 1-16; 2-4 recommended for normal local use

git:
  use_worktrees: true
  worktree_dir: .agent-cli/worktrees
  branch_prefix: agent-cli/

skills:
  paths: []                      # added after built-in, user, and project locations
  activation: {}                 # skill name → always | auto | explicit | disabled
  context_budget_tokens: 8000

harnesses: {}                    # persistent user config; runtime detection/readiness is never saved
providers: {}                    # persistent user config; runtime health/readiness is never saved

# Advanced. Not shown in /startup.
routing:
  role_floors:
    orchestrator: high
    spec_author: high
    spec_reviewer: mid
    architect: high
    implementer: low
    code_reviewer: mid
    research_investigator: high
    security_investigator: high
    skill_author: high
    skill_evaluator: high
  classifier:
    mode: auto                  # auto | selected | disabled
    selected_model: null
```

`workers.max_concurrency` is a ceiling, not a target: the orchestrator should launch only as many independent workers as useful, up to the configured limit. V1 accepts 1–16 workers, defaults to 3, and recommends 2–4 for normal local use.
Role floors are capability minimums: `low = 2`, `mid = 3`, and `high = 4` on V1's integer 1–5 score scale. Each canonical role maps to one primary capability as defined in [ROUTING.md](ROUTING.md#role-floors).

Classifier mode is also advanced configuration. `auto` only uses an active low-cost classifier model that meets the built-in classifier floor; if none exists, Agent CLI uses deterministic skill routing. `selected` uses the exact configured model without silent substitution. `disabled` never calls a semantic classifier. No cheap classifier is required for Agent CLI to function.


Relative `skills.paths` entries resolve against the repository root (the working directory outside a repository); `~` expands to the home directory. A configured path that does not exist is an error. `skills.context_budget_tokens` budgets skill text only, not the worker's whole context window.

`review.max_rounds`, `workers.max_concurrency`, and `skills.context_budget_tokens` are initial values. Built-in skills and routing ship with the CLI; user/project config overrides them without modifying built-in files.

## Credentials

Credentials never appear in config YAML. A provider entry refers to a credential by name:

```yaml
providers:
  local-vllm:
    type: openai-compatible
    base_url: http://localhost:8000/v1
    api_key_env: LOCAL_VLLM_API_KEY      # or: api_key_keyring: agent-cli/local-vllm
```

Values come from environment variables or the OS credential store.

Like other sections, provider and harness entries merge by key across layers, so a project can change one field of a provider defined globally (for example `enabled: false`). Required fields such as a provider's `type` must be present once all layers are merged.

## Validation

Config is validated on load. Unknown keys and invalid values are errors that name the file and key. Agent CLI does not silently ignore them. Keys that look like secret values (`api_key`, `token`, `password`, …) are rejected with a pointer to `api_key_env` / `api_key_keyring`.

## Writing

`/startup` writes to the layer the user chooses:

- **global** records every setting the flow asks about, so the file describes the user's full baseline.
- **project** records only settings that differ from what the project would inherit from global config. Setting a project value back to the inherited value removes the project override, so the project keeps following global changes.

At the V1-04 stage, `/startup` preserves provider/harness entries while showing adapter-backed runtime readiness. As real adapters land, the final V1 startup flow will guide harness/provider configuration and authentication using these same persistent fields. Runtime detection, readiness, health, and session state are never written into config.

Keys the current setup flow does not manage (including routing and any provider/harness fields not explicitly edited) are preserved, but the file is rewritten from its values: comments other than the generated header are not kept.

Writes are atomic (temporary file + rename), create parent directories, and are validated against every other layer first. A symlinked config file is written through to its target, and an existing file keeps its permissions.

Saving the **project** layer also git-ignores that project's runtime directories (`.agent-cli/state/` and the worktree directory). Saving global config never modifies the repository you happen to be in.

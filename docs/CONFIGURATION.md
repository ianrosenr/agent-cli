# Configuration

## Files and precedence

```
global   ~/.agent-cli/config.yaml
project  <repo>/.agent-cli/config.yaml
session  set in the CLI for the current session
task     set by the orchestrator or user for one task
```

The most specific layer wins: global → project → session → task. A key absent from a layer inherits from the layer above. Defaults apply when no layer sets a key.

A project or task layer may **lower** a cost limit but may not raise one above the global limit.

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
  max_concurrency: 3

git:
  use_worktrees: true
  worktree_dir: .agent-cli/worktrees
  branch_prefix: agent-cli/

skills:
  paths: []                      # added after the default locations
  activation: {}                 # skill name → always | auto | explicit | disabled
  context_budget_tokens: 8000

harnesses: {}                    # detected/configured by /startup
providers: {}                    # configured by /startup

# Advanced. Not shown in /startup.
routing:
  role_floors:
    orchestrator: high
    spec_author: high
    research_investigator: high
    security_investigator: high
    spec_reviewer: mid
    code_reviewer: mid
    implementer: low
```

`review.max_rounds`, `workers.max_concurrency`, and `skills.context_budget_tokens` are initial values. They may be tuned by the issue that implements each feature.

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

## Validation

Config is validated on load. Unknown keys and invalid values are errors that name the file and key. Agent CLI does not silently ignore them.

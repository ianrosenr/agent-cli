# Harness and provider adapters

Agent CLI is the control plane. It decides what work exists, who owns it, which model, provider, and harness run it, and how results are reviewed. External **harnesses** (Claude Code, Pi, Codex CLI, OpenCode, ...) execute coding agents; **providers** serve models. Adapters translate between Agent CLI's normalized contract and each one's native commands, SDK, RPC, or process. Agent CLI never reimplements a harness's tool loop, streaming, or sessions.

Code lives in `src/agent_cli/adapters/`:

| Module | Contents |
|---|---|
| `domain.py` | normalized, vendor-neutral types: identity, capabilities, readiness, selections, worker request/handle/status/result, usage, model descriptors, `Failure` / `AdapterError` |
| `harness.py` | `HarnessAdapter` interface |
| `provider.py` | `ProviderAdapter` interface |
| `registry.py` | `AdapterRegistry`: adapters looked up by ID |
| `status.py` | human-readable status combining config with live readiness (`/startup`, `/providers`) |

Shared vocabulary used by both config and adapters (`AccessKind`, `ImplementationMode`) lives in `src/agent_cli/terms.py`, so the adapter domain does not depend on the config package. Adapters available to a session are registered in `default_adapters()` in `src/agent_cli/cli.py`.
| `fake.py` | configurable fakes implementing the real interfaces, for tests |

Core code depends only on these types. Adding an adapter means implementing an interface and registering it; orchestration does not change.

## Rules every adapter follows

- **Only `AdapterError` escapes.** Vendor exceptions are translated into an `ErrorKind` (`not_found`, `auth_required`, `auth_failed`, `unavailable`, `rate_limited`, `timeout`, `invalid_request`, `unsupported`, `incompatible`, `provider_error`, `harness_error`, `canceled`). Results record failures as `Failure` (plain data: kind and message), so they can be stored and compared; `AdapterError` wraps a `Failure` when one is raised. Retry, cooldown, and fallback policy belong to V1-13, not adapters.
- **Capabilities are explicit.** A harness lists what it supports (`Capability`) and, optionally, the only models/providers it can run. A listed model or provider is always accepted (so a fixed-model harness runs its model); without a list, the harness must support `model_selection` / `provider_selection`. Optional operations (`send`, `interrupt`, `diff`, `usage`) default to raising `unsupported`.
- **Selections are never substituted.** `WorkerRequest.model`, `.provider`, and `.harness` are `Selection`s: an ID plus whether the user pinned it. Adapters carry them unchanged. A harness that cannot honor one raises `incompatible` (`HarnessAdapter.check`); a provider calls exactly the requested model or fails. Routing decides what to do next, and a pinned selection that nothing can honor goes back to the user.
- **Model, provider, and harness stay separate.** No type encodes one as another.
- **Readiness is runtime state, not configuration.** Detection, version, authentication, and health are reported by `readiness()` and shown next to config; they are never written to config files. `HarnessConfig` / `ProviderConfig` hold only what the user set. A config entry and an adapter refer to the same harness or provider when the config key equals the adapter's `identity().id`.
- **Readiness is fast and never raises.** Setup calls it synchronously, so any process or network check must use a short timeout and report a timeout as `unavailable`. Only `ready` adapters may execute work. `detected` means the adapter was found but readiness has not yet been confirmed, so execution remains blocked. Work requested while not ready fails with the matching normalized error kind.
- **No vendor fields in core types.** Vendor detail stays inside the adapter that needs it.

## Status

The interfaces, fakes, and `/startup` / `/providers` integration exist. Claude Code is the first real harness; Pi remains the second portability test.

### Claude Code harness (V1-10.5)

`claude-code` (`src/agent_cli/adapters/claude_code.py`) wraps the installed Claude Code CLI and deliberately uses the user's Claude subscription login rather than the Anthropic API.

- **Detection/auth.** Runtime always registers the adapter so `/startup` and `/providers` can detect `claude`. Readiness runs bounded `claude --version` and `claude auth status --json` checks. Runtime auth is never written to Agent CLI config.
- **Subscription-only.** Readiness fails closed when Anthropic API/cloud billing override variables are set and tells the user which variable to unset. It then accepts Claude account/OAuth auth and rejects API-key or third-party auth, so this path cannot silently become pay-as-you-go.
- **Execution.** Workers use Claude Code print mode with JSON output, no session persistence, a 25-turn cap, and the exact requested model when one is selected. Task/spec/role/skills context goes over stdin, not argv.
- **Permissions.** `autopilot` uses Claude Code's `auto` permission mode; `plan-only` uses `plan`. Agent CLI `checkpoint` is rejected for now because a headless worker cannot pause on Agent CLI's approval boundary.
- **Lifecycle.** Start/status/result/interruption are normalized. Results retain a concise summary, token usage when Claude reports it, and Git changed-file/diff information. Raw stderr is never surfaced because it may contain task context.
- **Provider pin.** This fixed access path identifies itself as `claude-subscription`. A conflicting provider pin, including the paid `anthropic` API provider, is rejected rather than substituted.
- **Config.** No config is required for normal detection. `harnesses.claude-code.command` can override the executable path and `enabled: false` disables it.

### Anthropic provider (V1-09.5)

The first real provider adapter is `anthropic` (`src/agent_cli/adapters/anthropic.py`), the first-party Claude API through Anthropic's official Python SDK. It is built at startup by `agent_cli.runtime.default_adapters` from the resolved `providers.anthropic` entry (the adapter never reads config files) and rebuilt after `/startup` saves.

- **Scope.** One request shape: an uncached, non-streaming Messages call with a text system prompt and one user text message, sent to the exact requested model. Generation pins `inference_geo="global"`, `service_tier="standard_only"`, and `speed="standard"` so the request matches the standard global price source. Bedrock, Vertex, Foundry, proxies (`base_url`), batch, prompt caching, fast mode, US-only inference, tools, images, and streaming are out of scope.
- **Readiness.** Config problems (wrong `type`, a `base_url`, no or two credential references) are `misconfigured`; a missing secret is `auth_required`. Otherwise one Models API call with a 5-second timeout: success is `ready`, a rejected key is `auth_required`, anything else `unavailable`. Readiness and the model list are cached in memory for 60 seconds (an `unavailable` result for 5), never persisted. The credential is resolved once per adapter; setup rebuilds the adapter after it saves, and hand edits to config take effect on the next start.
- **Models.** `list_models` returns active models with their exact IDs and the API's `max_input_tokens`. Listing a model never adds it to the registry; unscored models are usable only when named.
- **No hidden retries.** The SDK client is built with `max_retries=0` and an explicit first-party base URL, so `ANTHROPIC_BASE_URL` cannot redirect it. Retry and fallback are V1-13's.
- **Errors.** SDK exceptions map to `auth_failed` (401/403), `not_found` (404), `invalid_request` (400/413/422), `rate_limited` (429), `timeout`, `unavailable` (connection), and `provider_error` (5xx, overloaded, anything else). Messages carry the status, Anthropic's error type, and request ID only: never prompts, keys, or response bodies. A reply with no text, or from a model other than the one requested, is a `provider_error`.
- **Usage.** Input and output tokens and the message ID are reported; `cost_usd` stays empty. Usage outside the standard shape (cache tokens, non-global inference, priority tier, fast mode) is reported as unknown, so the budget settles at the reserved bound.

#### Manual smoke test (real API key, real spend)

CI never calls Anthropic. To try the real path, with a small budget:

```sh
export ANTHROPIC_API_KEY=...        # never paste it into Agent CLI or config
cd <a git repository>
uv run agent-cli
> /startup                          # accept "Configure Anthropic using ANTHROPIC_API_KEY"
> /providers                        # anthropic (Anthropic): Ready (Claude API; key from env ANTHROPIC_API_KEY)
> /models                           # Runtime paths: claude-sonnet-5-5  anthropic (api)
> add expiration support to the cache
> /budget                           # two settled reservations (planning, spec authoring)
```

# Harness and provider adapters

Agent CLI is the control plane. It decides what work exists, who owns it, which model, provider, and harness run it, and how results are reviewed. External **harnesses** (Pi, Codex CLI, Claude Code, OpenCode, ...) execute coding agents; **providers** serve models. Adapters translate between Agent CLI's normalized contract and each one's native commands, SDK, RPC, or process. Agent CLI never reimplements a harness's tool loop, streaming, or sessions.

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
- **Readiness is fast and never raises.** Setup calls it synchronously, so any process or network check must use a short timeout and report a timeout as `unavailable`. Work requested while not ready fails with the matching error kind (`Readiness.blocking_error()`: sign-in required → `auth_required`, not detected → `not_found`, and so on).
- **No vendor fields in core types.** Vendor detail stays inside the adapter that needs it.

## Status

The interfaces, fakes, and `/startup` / `/providers` integration exist. No real harness or provider adapter is registered yet; Pi is the preferred first candidate.

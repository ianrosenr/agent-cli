# Agent CLI

A local-first orchestration and routing control plane for coding agents.

Describe a goal in natural language. Agent CLI inspects the repo, plans and reviews the work, selects the model/provider/harness, and delegates execution to existing coding-agent harnesses. It does not implement its own coding-agent tool loop. It adds policy, economics, routing, memory, and review above the harness layer.

> Spend expensive reasoning where decisions matter. Use inexpensive models for well-specified execution.

## Status

Pre-alpha. The repository contains design docs, agent contracts, and a runnable CLI shell whose slash commands are placeholders. See [docs/BUILD_GUIDE.md](docs/BUILD_GUIDE.md#15-build-order) for the build order.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run agent-cli            # or: uv run python -m agent_cli
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

## What V1 targets

- Harness adapters: Pi, Codex CLI, Claude Code, OpenCode
- Providers: subscription access, local OpenAI-compatible endpoints, paid APIs
- Default paid-API budget: $20/month, no daily limit, $1/task, $0.30/call
- Implementation modes: autopilot (default), checkpoint, plan-only
- Review required before merge requests (configurable)

## Docs

- [docs/BUILD_GUIDE.md](docs/BUILD_GUIDE.md) — product narrative and build order
- [docs/V1_SPEC.md](docs/V1_SPEC.md) — V1 contract
- [CONTRIBUTING.md](CONTRIBUTING.md) — branch, PR, and CI policy
- [docs/VERSIONING.md](docs/VERSIONING.md) — semantic version-impact policy
# Agent CLI

A local-first CLI that orchestrates coding agents inside your repository.

Describe a goal in natural language. Agent CLI inspects the repo, has a strong model write a short implementation spec, gets the spec reviewed, hands it to a cheaper implementer in an isolated Git worktree, challenges the result with a reviewer, and shows you the final diff. It enforces budgets and handles provider throttling along the way.

> Spend expensive reasoning where decisions matter. Use inexpensive models for well-specified execution.

## Status

Pre-alpha. The repository currently contains design docs and agent contracts only. No runtime code exists yet. See [docs/BUILD_GUIDE.md](docs/BUILD_GUIDE.md#15-build-order) for the build order.

## What V1 targets

- Harnesses: Codex CLI, Claude Code, OpenCode
- Providers: subscription access, local OpenAI-compatible endpoints, paid APIs
- Default paid-API budget: $20/month, no daily limit, $1/task, $0.30/call
- Implementation modes: autopilot (default), checkpoint, plan-only
- Review required before merge requests (configurable)

## Docs

- [docs/BUILD_GUIDE.md](docs/BUILD_GUIDE.md) — product narrative and build order
- [docs/V1_SPEC.md](docs/V1_SPEC.md) — V1 contract
- [AGENTS.md](AGENTS.md) — start here if you are a coding agent

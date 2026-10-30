# AGENTS.md

Entry point for coding agents working on this repository.

Agent CLI is a local-first developer orchestration CLI written in Python. It turns a natural-language engineering goal into reviewed specs, routes each subtask to an appropriate model/provider/harness, runs workers in isolated Git worktrees, and gates merge requests on review.

## Workflow for every issue

1. Read this file.
2. Read [docs/BUILD_GUIDE.md](docs/BUILD_GUIDE.md) for the product narrative and build order.
3. Read the issue.
4. Read only the specialist docs listed below that the issue touches.
5. Write a bounded plan. Separate repository facts from assumptions.
6. Implement only that issue. Do not build later roadmap items early.
7. Add or update tests for behavior changes.
8. Run applicable checks; never claim a check passed without running it.
9. Show the diff.
10. Record good out-of-scope ideas in [docs/ENHANCEMENTS.md](docs/ENHANCEMENTS.md) or as new issues.

Commits should be small and targeted, one coherent change each.

## Where things live

| Topic | Doc |
| --- | --- |
| V1 scope and acceptance | [docs/V1_SPEC.md](docs/V1_SPEC.md) |
| Components and boundaries | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Roles, authority, task flow, modes | [docs/ORCHESTRATION.md](docs/ORCHESTRATION.md) |
| Model/provider/harness selection, cost, fallback | [docs/ROUTING.md](docs/ROUTING.md) |
| Shared task memory | [docs/MEMORY.md](docs/MEMORY.md) |
| Skills hierarchy and context budget | [docs/SKILLS.md](docs/SKILLS.md) |
| Config files, defaults, precedence | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| `/startup` | [docs/STARTUP_FLOW.md](docs/STARTUP_FLOW.md) |
| Spec and code review | [docs/REVIEW_PIPELINE.md](docs/REVIEW_PIPELINE.md) |
| Deferred work | [docs/ENHANCEMENTS.md](docs/ENHANCEMENTS.md) |

## Two skill directories

- `.agents/skills/` — role skills for coding agents **building this repository** (orchestrator, spec author, implementer, reviewer). Load the one matching your role.
- `.agent-cli/skills/` and `.agent-cli/policies/` — skills and routing policy **shipped as Agent CLI product content**, loaded by the CLI at runtime for its own workers. Treat them as product data; change them only when an issue says so.

## Hard rules

- Firstmate is prior art (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#prior-art-firstmate)). Do not reinvent its proven concepts for novelty.
- Budget limits are enforced in code, never delegated to an LLM's judgment.
- Credentials never go in ordinary YAML config.
- Use Git worktrees for worker isolation; no custom filesystem sandboxing.
- No merge request is created until configured review requirements pass.
- When a doc becomes false because of your change, update it in the same change.

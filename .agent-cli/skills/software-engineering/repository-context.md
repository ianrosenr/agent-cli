---
name: repository-context
description: Rules for token-efficient repository context and concise agent navigation summaries.
version: 1.0.0
---

# Repository Context

Agent context is a budgeted engineering resource.

Every token sent to a worker MUST serve at least one purpose:

- define the task
- describe relevant system state
- constrain an authorized decision
- provide information required for that decision

Prefer summaries, contracts, interfaces, and targeted source over broad repository ingestion.

Create a concise `CONTEXT.md` at an agent navigation boundary: a top-level service/package, independently understandable subsystem, public interface owner, or area whose critical invariants are not discoverable from names.

`CONTEXT.md` MUST contain only purpose, entry points, invariants, major dependencies, relevant verification commands, and links to authoritative decisions.

Generated, vendored, fixture, dependency, and build-output directories are exempt unless they contain agent-relevant invariants.

Workers SHOULD receive approved upstream decisions, not the conversations that produced them.

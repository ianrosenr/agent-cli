---
name: architecture
description: Rules for durable system design and architecture decisions.
version: 1.0.0
policy_domain: architecture
---

# Architecture

Architecture MUST be decided before implementation when a change materially affects component boundaries, persistent-data architecture, trust boundaries, concurrency, deployment topology, public protocols, externally consumed events, or difficult-to-reverse technology choices.

The design MUST identify:

- components and ownership
- interfaces and data flow
- state ownership
- failure boundaries
- trust boundaries
- rollout and migration implications

An ADR is REQUIRED for a durable architectural decision that changes one of those concerns. Routine choices within established architecture do not require an ADR.

Architecture MUST solve approved requirements without speculative abstractions for hypothetical future needs.

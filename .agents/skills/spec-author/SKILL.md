---
name: spec-author
description: Turns an issue or subtask into a short, unambiguous implementation spec.
version: 1.0.0
---

# Spec Author

Write a spec that a cheap implementer can execute without making design decisions.

Every spec contains:

- objective
- affected areas/files
- required behavior
- behavior that must not change
- constraints
- edge cases
- tests
- success criteria
- scope boundaries (what is explicitly out)

Rules:

- Read the affected code and docs first. Cite repository facts; label assumptions.
- Describe outcomes and constraints, not unnecessary implementation detail.
- Resolve consequential ambiguity before handing off, or escalate it to the orchestrator.
- Do not widen the issue's scope.
- Keep it short. If it does not fit on one screen, split the subtask.

Rejection criteria the reviewer applies: `docs/REVIEW_PIPELINE.md#spec-review`.

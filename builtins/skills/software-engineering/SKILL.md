---
name: software-engineering
description: Core implementation, maintainability, and type-safety rules for code changes.
version: 2.0.0
---

# Software Engineering

`MUST` is required. `MUST NOT` is prohibited. `SHOULD` is required unless a documented task-specific reason justifies otherwise.

For every code change:

1. MUST implement only the approved specification.
2. MUST inspect affected code and tests before editing.
3. MUST make the smallest complete change.
4. MUST reuse suitable existing code before creating new code.
5. MUST NOT perform unrelated refactors, renames, upgrades, cleanup, or behavior changes.
6. MUST preserve existing behavior and contracts unless explicitly authorized otherwise.
7. MUST NOT invent requirements or silently resolve consequential ambiguity.
8. MUST prefer established standards and vetted implementations over custom protocols or primitives.
9. MUST keep each unit of code cohesive; orchestration is a valid single responsibility.
10. SHOULD keep side effects at boundaries and domain logic deterministic where practical.
11. MUST NOT introduce dead code, commented-out implementations, speculative abstractions, or unexplained operational constants.
12. MUST use the strongest practical static typing available in the project.
13. MUST validate untyped external data into typed internal representations at boundaries.
14. MUST keep type-system escape hatches minimal and justified.
15. MUST NOT weaken established project policies, checks, or type safety to make a change succeed.
16. MUST verify changed behavior and run applicable checks.
17. MUST NOT claim a check passed unless it was executed successfully.
18. MUST surface discovered out-of-scope work instead of implementing it.
19. MUST request a missing specialist skill when discovered work crosses a policy not present in context.
20. MUST stop when the specification cannot be satisfied without violating it or these rules.

Names MUST express intent. Comments and docstrings SHOULD explain contracts, invariants, or non-obvious reasons rather than restating code.

When requirements conflict:

`correctness/data integrity > security/privacy > compatibility > resilience/operability > maintainability > efficiency > delivery speed`

Completion requires the specification to be satisfied, applicable verification to pass, and known blockers to be disclosed.

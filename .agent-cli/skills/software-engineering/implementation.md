---
name: implementation
description: Rules for cohesive, minimal, maintainable code implementation.
version: 1.0.0
---

# Implementation

Each unit of code MUST have one cohesive responsibility.

A function MAY coordinate multiple operations when orchestration is its single responsibility.

Prefer:

1. existing project code
2. standard library/framework capability
3. existing project dependency
4. official maintained implementation
5. mature maintained third-party implementation
6. custom implementation

Side effects SHOULD remain at boundaries. Domain logic SHOULD be deterministic where practical.

Do not introduce mutable global state, dead code, commented-out implementations, speculative abstractions, or unexplained operational constants.

Names MUST express intent. Comments and docstrings MUST explain contracts, invariants, or non-obvious reasons rather than restating code.

Public or non-obvious interfaces SHOULD be documented concisely. Types and names SHOULD carry information that does not require prose.

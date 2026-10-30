---
name: software-engineering
version: 1.0.0
always_load: true
---

# Software Engineering

`MUST` is required. `MUST NOT` is prohibited. `SHOULD` is required unless a documented task-specific reason justifies otherwise.

For every code change:

1. MUST implement only the approved specification.
2. MUST inspect affected code and tests before editing.
3. MUST make the smallest complete change.
4. MUST reuse suitable existing code before creating new code.
5. MUST NOT perform unrelated changes.
6. MUST preserve existing behavior and contracts unless the specification explicitly changes them.
7. MUST NOT invent requirements or silently resolve consequential ambiguity.
8. MUST prefer established standards and vetted implementations over custom protocols or primitives.
9. MUST NOT weaken security, validation, typing, tests, error handling, or observability to make a change succeed.
10. MUST verify changed behavior and run applicable checks.
11. MUST NOT claim a check passed unless it was executed successfully.
12. MUST surface discovered out-of-scope work instead of implementing it.
13. MUST request a missing specialist skill when discovered work crosses a policy not present in context.
14. MUST stop when the specification cannot be satisfied without violating it or these rules.

Runnable production software MUST use OpenTelemetry for telemetry and applicable OpenTelemetry Semantic Conventions. Libraries MUST instrument through the OpenTelemetry API and MUST NOT configure SDKs or exporters. Telemetry export MUST be deployment-controlled and MUST NOT expose secrets or sensitive data.

When requirements conflict:

`correctness/data integrity > security/privacy > compatibility > resilience/operability > maintainability > efficiency > delivery speed`

Completion requires the specification to be satisfied, applicable verification to pass, and known blockers to be disclosed.

---
name: dependencies
version: 1.0.0
policy_domain: dependencies
---

# Dependencies

Builds and development environments MUST be reproducible from version-controlled manifests and lockfiles where the ecosystem supports them.

Local environments are not authoritative.

Use isolated development environments. Generated environment directories MUST NOT be committed.

Before adding a dependency, verify that existing project or standard-library capability is insufficient and that the dependency's maintenance, security, license, size, and operational cost are acceptable.

Dependencies MUST be pinned or constrained according to ecosystem best practice.

New code MUST use the strongest practical static typing available in the project.

Untyped external data MUST be validated into typed internal representations at boundaries.

Type-system escape hatches MUST be minimal and justified.

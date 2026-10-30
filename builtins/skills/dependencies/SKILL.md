---
name: dependencies
description: Rules for reproducible environments and dependency selection.
version: 2.0.0
---

# Dependencies

Builds and development environments MUST be reproducible from version-controlled manifests and lockfiles where the ecosystem supports them.

Local environments are not authoritative.

Use isolated development environments. Generated environment directories MUST NOT be committed.

Before adding a dependency, verify that existing project or standard-library capability is insufficient and that the dependency's maintenance, security, license, size, and operational cost are acceptable.

Dependencies MUST be pinned or constrained according to ecosystem best practice.

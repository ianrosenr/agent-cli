---
name: delivery
description: Rules for safe configuration, deployment, rollout, recovery, health checks, and CI/CD.
version: 1.0.0
policy_domain: delivery
---

# Delivery

Production changes MUST be deployable without relying on undocumented manual state.

Configuration MUST be externalized where environment-specific values vary.

Rollouts with material operational risk SHOULD support staged exposure or equivalent blast-radius control.

Changes requiring coordinated deployment MUST define ordering and compatibility between versions.

Risky or irreversible releases MUST define recovery behavior before deployment.

Health checks MUST represent the service's ability to perform required work and MUST NOT report healthy solely because the process is running.

CI/CD MUST fail closed on required verification gates. Waivers require explicit human approval and recorded rationale.

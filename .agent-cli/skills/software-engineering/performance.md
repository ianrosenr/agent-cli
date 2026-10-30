---
name: performance
description: Rules for measured performance work, benchmarking, and resource efficiency.
version: 1.0.0
policy_domain: performance
---

# Performance

Do not optimize without an identified requirement, bottleneck, or measured risk.

Performance-sensitive changes MUST define the workload and metric being optimized.

Benchmark before and after when performance is the purpose of the change.

Prefer algorithmic and architectural improvements over micro-optimization.

Resource use MUST be considered across CPU, memory, I/O, network, storage, external-service calls, and cost.

Performance improvements MUST NOT trade away correctness, security, or operability without explicit approval.

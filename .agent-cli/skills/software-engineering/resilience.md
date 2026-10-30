---
name: resilience
description: Rules for timeouts, retries, idempotency, bounded resources, and failure isolation.
version: 1.0.0
---

# Resilience

Cross-process and network operations MUST have explicit time bounds.

Retries MUST target transient failures, be bounded, use backoff, and avoid duplicating unsafe side effects.

Retryable operations SHOULD be idempotent or deduplicated.

Concurrency, queues, buffers, caches, retries, and resource pools MUST be bounded.

Long-running work SHOULD support cancellation where practical.

Failure isolation MUST prevent one dependency or task from exhausting unrelated capacity.

Recovery behavior MUST preserve data integrity.

Graceful degradation is preferred only when it does not hide correctness or security failures.

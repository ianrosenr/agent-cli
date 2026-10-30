---
name: observability
description: Rules for OpenTelemetry-based metrics, traces, logs, and diagnostic coverage.
version: 1.0.0
---

# Observability

Software MUST be observable by construction.

OpenTelemetry is the standard instrumentation model. Use applicable Semantic Conventions before defining custom names or attributes.

Runnable production software owns SDK/exporter configuration. Reusable libraries instrument through the OpenTelemetry API and MUST NOT select or configure exporters.

Prefer:

- metrics for aggregate behavior
- traces for execution across boundaries
- structured logs for diagnostic detail

Instrumentation MUST answer a defined operational question.

Runtime paths affecting requests, jobs, dependencies, state transitions, failures, retries, queues, latency, throughput, or critical outcomes MUST expose sufficient telemetry to detect and diagnose degradation.

Metrics MUST define meaning, unit, type, labels, and expected cardinality.

Telemetry dimensions MUST be bounded. Request IDs, user IDs, arbitrary URLs, free-form errors, and equivalent unbounded values MUST NOT be metric labels.

Telemetry export MUST be deployment-controlled. Prefer OTLP-compatible boundaries to vendor-specific instrumentation.

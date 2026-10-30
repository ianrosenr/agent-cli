---
name: skill-evaluation
version: 1.0.0
policy_domain: skill-evaluation
---

# Skill Evaluation

Skills MUST be versioned. Behavioral skill changes MUST be evaluated before release.

Maintain separate evaluation suites for:

1. routing: whether the orchestrator selected the required skills
2. behavior: whether the worker followed supplied skills

Offline golden cases MUST be realistic and include required behaviors, forbidden behaviors, expected artifacts, risk tags, and scoring criteria.

Critical correctness and safety invariants require 100% pass rate. Other acceptance thresholds MUST be explicit and versioned.

Behavioral evaluation runs MUST record skill version, model/provider version, model settings, repository revision, and dataset version.

Response caching MUST be bypassed for behavioral regression evaluation when possible; stale local caches MUST NOT determine results.

Production feedback MAY create candidate cases automatically but MUST NOT automatically modify skills, prompts, expectations, or the golden set.

Promotion path:

`capture -> redact -> deduplicate -> reproduce -> label -> review -> promote`

Production-derived cases MUST be reproducible, sanitized, labeled with expected behavior, and approved before joining the golden set.

Online signals SHOULD include explicit feedback, corrections, reviewer overrides, reverted changes, failed verification, policy violations, retries, latency, token use, and cost.

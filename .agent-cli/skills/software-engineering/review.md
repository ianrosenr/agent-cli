---
name: review
description: Rules for adversarial implementation review against approved specifications.
version: 1.0.0
policy_domain: review
---

# Review

Review MUST compare the implementation against the approved specification, not the author's stated intent.

Review MUST actively search for:

- unsatisfied requirements
- unauthorized scope
- regressions
- hidden assumptions
- unnecessary complexity
- missing failure handling
- missing verification
- incompatible interface changes
- policy violations

Findings MUST identify the violated requirement or concrete failure mode.

Do not request stylistic churn that does not improve correctness, clarity, maintainability, or project consistency.

Reviewer and implementer disagreement on material risk MUST escalate rather than loop indefinitely.

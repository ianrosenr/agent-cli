---
name: review
description: Rules for adversarial review against an approved contract.
version: 2.0.0
---

# Review

Review MUST compare the artifact against its approved contract, not the author's stated intent.

Review MUST actively search for:

- unsatisfied requirements
- unauthorized scope
- regressions
- hidden assumptions
- unnecessary complexity
- missing risk handling
- missing verification
- policy violations

Findings MUST identify the violated requirement or concrete failure mode.

Do not request stylistic churn that does not improve correctness, clarity, maintainability, or project consistency.

Material reviewer/author disagreement MUST escalate rather than loop indefinitely.

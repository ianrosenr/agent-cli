# Review Pipeline

Review happens twice: on the spec before implementation, and on the diff after. Both are adversarial. Reviewers question the author, not just comment.

## Spec review

The spec reviewer checks the spec for:

- ambiguity a cheap implementer could misread
- hidden assumptions presented as facts
- missing tests or acceptance criteria
- security and data-integrity risks
- unnecessary scope beyond the user's goal

A spec must contain: objective, affected areas/files, required behavior, behavior that must not change, constraints, edge cases, tests, success criteria, and scope boundaries. A spec missing any of these is rejected.

## Code review

The code reviewer compares the diff to the **approved spec**, not to the implementer's stated intent. It looks for unsatisfied requirements, unauthorized scope, regressions, missing failure handling, missing verification, and incompatible interface changes. The full rule set is the product's [review skill](../.agent-cli/skills/software-engineering/review.md).

## Challenge questions

Reviewers can send questions to the author through the orchestrator, for example:

- Why was this change necessary?
- Which spec requirement authorizes it?
- Could this be simpler?
- What failure modes were considered?
- Why did you touch unrelated behavior?

Answers and the resulting findings are recorded in memory.

## Findings

Each finding states the violated requirement or the concrete failure mode, plus a severity: `blocking` or `non-blocking`. Pure style preferences that do not improve correctness, clarity, maintainability, or consistency are not findings.

## Rounds and escalation

```
review → findings → author fixes → re-review   (repeat ≤ review.max_rounds, default 3)
```

Escalate to the orchestrator when:

- the round limit is reached with blocking findings open
- reviewer and author disagree on a material risk
- the fix needs a spec change

Reviewer floors rise from `mid` to `high` for risky changes. See [ROUTING.md](ROUTING.md#role-floors) for the list.

## Merge-request gate

With `review.before_merge_request: true` (default), Agent CLI creates no merge request until:

- the spec was approved
- code review reports no open blocking findings
- applicable checks were run and passed, and their results are recorded

Agent CLI never merges automatically.

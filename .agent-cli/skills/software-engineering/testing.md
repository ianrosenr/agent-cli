---
name: testing
description: Rules for risk-proportional verification, regression tests, and test integrity.
version: 1.0.0
---

# Testing

Behavioral changes MUST have verification proportional to risk.

Bug fixes SHOULD include a regression test that fails without the fix.

Test externally meaningful behavior rather than incidental implementation details.

Use the cheapest layer that proves the requirement:

`unit -> integration -> contract -> end-to-end`

Boundary behavior MUST use integration or contract coverage when unit tests cannot prove the contract.

Applicable success, edge, invalid-input, and failure paths MUST be tested.

Tests MUST NOT be deleted, skipped, weakened, or rewritten solely to hide a regression.

A flaky test MUST be fixed or explicitly quarantined with tracked follow-up. Re-running until green is not verification.

---
name: data
description: Rules for persistent data, migrations, transactions, backfills, and indexes.
version: 1.0.0
policy_domain: data
---

# Data

Persistent-data changes MUST protect correctness across mixed application versions during rollout when rolling deployment is possible.

Schema changes SHOULD be backward-compatible and staged when a single atomic deployment cannot be guaranteed.

Destructive migrations MUST define backup, recovery, and rollback or explicitly document irreversibility.

Backfills MUST be restartable, bounded, observable, and safe to rerun where practical.

Multi-step updates requiring atomicity MUST use transactions or equivalent guarantees.

Indexes MUST be justified by access patterns and write/storage cost.

Data validation MUST occur before invalid state is persisted.

Never rely on application ordering where the datastore must enforce an invariant.

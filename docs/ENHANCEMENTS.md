# Enhancements

Deliberately out of scope for V1. V1 work must not be blocked on these. Each item should become its own issue before work starts.

| Enhancement | Why deferred |
| --- | --- |
| Many providers and harnesses beyond the V1 set | the adapter interfaces come first; breadth can come later |
| Remote or distributed workers | V1 is local-first |
| Rich UI beyond the CLI | the CLI experience comes first |
| Automated benchmark aggregation for the registry | V1 uses a hand-maintained seed |
| Learned or personalized routing | needs usage data V1 does not collect yet |
| Autonomous registry retraining | same; also needs evaluation safeguards |
| Vector-database memory | the structured local store is enough for V1 |
| Semantic skill retrieval | V1 selects by metadata and signals |
| Enterprise / multi-user behavior | single developer first |
| Automatic PR merging | the MR gate stops before merge by design |
| Support for every Git host | target one host first |

## Adding an item

When you find a good idea outside the current issue's scope, add a row here (or open an issue) instead of implementing it. Give a one-line reason it can wait.

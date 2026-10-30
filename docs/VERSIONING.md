# Versioning

Agent CLI follows semantic versioning.

## Default

`[PATCH]` is the default version impact.

The **pull request title is the authoritative version-impact source**.

If the title has no explicit version marker, treat the change as `[PATCH]`.

Do not maintain a second version selection in the PR body. This prevents contradictory metadata such as a `[MINOR]` title with `[PATCH]` checked elsewhere.

## Version impacts

| Marker | Use when |
| --- | --- |
| `[PATCH]` | Default. Bug fixes, internal changes, compatible refinements, routine maintenance. |
| `[MINOR]` | A backward-compatible user-facing capability is added. |
| `[MAJOR]` | A public contract or behavior changes incompatibly. |
| `[NONE]` | The change should explicitly produce no version/release bump. |

Agent CLI-created pull requests should omit a marker or use `[PATCH]` unless the approved task/spec clearly justifies another marker.

Examples:

```text
Fix config validation                  → PATCH
[PATCH] Fix config validation          → PATCH
[MINOR] Add /settings command          → MINOR
[MAJOR] Replace public config contract → MAJOR
[NONE] Update internal planning notes  → NONE
```

## Future release automation

When release automation is implemented, it should:

1. read an explicit marker from the PR title when present
2. otherwise choose `[PATCH]`
3. reject multiple/conflicting title markers
4. never infer `[MINOR]` or `[MAJOR]` merely because a change is large
5. require an explicit reason for `[MAJOR]`

The versioning decision is deterministic policy, not an LLM judgment unless the user/spec explicitly asks for classification.

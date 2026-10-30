# Versioning

Agent CLI follows semantic versioning.

## Default

`[PATCH]` is the default version impact.

If a change does not explicitly request another impact, treat it as `[PATCH]`.

This keeps versioning predictable and avoids asking agents/users to classify routine changes unnecessarily.

## Version impacts

| Marker | Use when |
| --- | --- |
| `[PATCH]` | Default. Bug fixes, internal changes, compatible refinements, routine maintenance. |
| `[MINOR]` | A backward-compatible user-facing capability is added. |
| `[MAJOR]` | A public contract or behavior changes incompatibly. |
| `[NONE]` | The change should explicitly produce no version/release bump. |

Agent CLI-created pull requests should use `[PATCH]` unless the approved task/spec clearly justifies another marker.

## Future release automation

When release automation is implemented, it should:

1. read the explicit version-impact marker when present
2. otherwise choose `[PATCH]`
3. never infer `[MINOR]` or `[MAJOR]` merely because a change is large
4. require an explicit reason for `[MAJOR]`

The versioning decision is deterministic policy, not an LLM judgment unless the user/spec explicitly asks for classification.

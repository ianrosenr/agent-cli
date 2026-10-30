# Contributing

## Branch and merge policy

`main` is the integration branch.

All repository changes should:

1. start on a feature/fix/chore branch
2. open a pull request targeting `main`
3. pass the required CI checks
4. merge through the pull request

Do not push implementation changes directly to `main`.

GitHub repository rules should enforce this policy by requiring a pull request before updates to `main` and requiring the `pytest` status check from the `CI` workflow.

## Required test gate

Every pull request to `main` runs `.github/workflows/ci.yml`.

Once the Python package exists, the required test command is:

```bash
uv run pytest
```

A pull request must not merge while any pytest test is failing.

Before V1-02 bootstraps the Python package, the workflow reports a successful pre-bootstrap state only when neither Python source nor tests exist.

## Version impact

Agent CLI uses semantic versioning.

The default version impact for a merged change is:

`[PATCH]`

Use another impact only when the change clearly requires it:

- `[PATCH]` — default; bug fixes, internal changes, compatible refinements
- `[MINOR]` — backward-compatible new user-facing capability
- `[MAJOR]` — breaking public behavior or compatibility change
- `[NONE]` — explicitly no release/version change

If no explicit version marker is supplied, treat the change as `[PATCH]`.

Agent CLI-generated pull requests should use `[PATCH]` by default.

See [docs/VERSIONING.md](docs/VERSIONING.md).

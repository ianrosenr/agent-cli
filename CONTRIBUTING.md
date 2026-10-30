# Contributing

## Branch and merge policy

`main` is the integration branch.

All repository changes should:

1. start on a feature/fix/chore branch
2. open a pull request targeting `main`
3. pass the required CI checks
4. merge through the pull request

Do not push implementation changes directly to `main`.

GitHub repository rules should enforce this policy by requiring a pull request before updates to `main` and requiring the `ruff`, `mypy`, and `pytest` status checks from the `CI` workflow.

## Required CI gates

Every pull request to `main` runs `.github/workflows/ci.yml`.

Required checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

A pull request must not merge while any required check is failing.

CI installs dependencies from the committed lock file with `uv sync --dev --locked`.

## Version impact

Agent CLI uses semantic versioning.

The default version impact is:

`[PATCH]`

The PR title is authoritative:

- no marker → `[PATCH]`
- `[PATCH]` → patch
- `[MINOR]` → backward-compatible new user-facing capability
- `[MAJOR]` → breaking public behavior or compatibility change
- `[NONE]` → explicitly no release/version change

Do not duplicate the selected version impact in the PR body.

See [docs/VERSIONING.md](docs/VERSIONING.md).

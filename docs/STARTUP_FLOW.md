# `/startup` Flow

`/startup` makes Agent CLI usable for a new user in a few minutes. Every step has a default, so pressing Enter through the whole flow gives a working setup.

## Implementation ownership

The list below describes the final V1 startup experience.

Implementation is staged:

- **V1-03** owns configuration loading, validation, persistence, and the guided setup UI. It exposes harness/provider configuration hooks but does not implement their adapters.
- **V1-04** supplies real harness/provider detection, readiness, authentication status, and adapter-backed provider information to that existing setup UI.

Do not duplicate V1-04 detection logic inside V1-03 just to make the startup screen look complete.

## Steps

1. **Detect harnesses.** Look for supported external harnesses such as Pi, Codex CLI, Claude Code, and OpenCode. Show what was found and its version/capabilities. Offer install hints for missing ones; do not install anything.
2. **Providers and auth.** For each detected harness, offer its subscription login path. Offer to add paid API providers and custom OpenAI-compatible endpoints. Store only credential *references* (env var name or keyring entry). See [CONFIGURATION.md](CONFIGURATION.md#credentials).
3. **Local models.** Offer to add local endpoints (e.g. vLLM, Ollama with an OpenAI-compatible API). Probe the endpoint and list its models.
4. **Budgets.** Confirm monthly ($20), per-task ($1), and per-call ($0.30) limits. No daily limit by default.
5. **Concurrency.** Maximum parallel workers (default 3).
6. **Skill directories.** Show default locations; allow extra paths.
7. **Implementation mode.** autopilot (default), checkpoint, or plan-only. See [ORCHESTRATION.md](ORCHESTRATION.md#implementation-modes).
8. **Review preference.** Require review before merge requests (default yes).
9. **Git/worktrees.** Confirm worktree use and branch prefix. Ensure the worktree and state directories are git-ignored.
10. **Summary.** Show the resulting config and where each value is written (global or project). Write only after the user confirms.

## Rules

- Do not show advanced settings (role floors, registry scores, fallback tuning) during normal startup.
- Re-running `/startup` edits the existing config. It shows current values as defaults.
- Never print or log secret values.
- If no provider is usable at the end, say so plainly and explain what is missing.

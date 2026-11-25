#!/usr/bin/env bash
# Claude Code Stop hook: run the tests; on failure exit 2 so Claude sees the
# failing test and keeps fixing instead of stopping. Fail-fast: first failure only.
# Usage: bash .claude/hooks/test-gate.sh [test command...]  (no args = autodetect)
# Skips when a git work tree has no uncommitted changes. Gives up after 3 blocks
# per session so an unfixable failure can't loop forever.
input=$(cat)
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  [ -n "$(git status --porcelain)" ] || exit 0
fi

if [ $# -gt 0 ]; then
  cmd=("$@")
elif find . -path ./node_modules -prune -o -path ./.venv -prune -o -name 'test_*.py' -print 2>/dev/null | grep -q .; then
  if [ -f pyproject.toml ]; then cmd=(uv run pytest -x -q); else cmd=(python3 -m pytest -x -q); fi
elif [ -f package.json ] && grep -q '"test"' package.json; then
  if [ -f pnpm-lock.yaml ]; then cmd=(pnpm test); else cmd=(npm test); fi
else
  exit 0
fi

sid=$(printf '%s' "$input" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
count="${TMPDIR:-/tmp}/claude-test-gate-${sid:-none}"

out=$("${cmd[@]}" 2>&1)
rc=$?
if [ $rc -eq 0 ]; then
  rm -f "$count"
  exit 0
fi

n=$(( $(cat "$count" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$count"
if [ "$n" -gt 3 ]; then
  echo "test-gate: tests still failing after 3 attempts; letting Claude stop." >&2
  rm -f "$count"
  exit 0
fi
{
  echo "Tests failed (${cmd[*]}, exit $rc). Fix the failing test below before stopping (attempt $n/3):"
  printf '%s\n' "$out" | tail -60
} >&2
exit 2

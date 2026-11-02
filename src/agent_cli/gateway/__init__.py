"""Root-prompt supervisor intake (V1-09.9).

::

    user -> semantic supervisor -> answer
                              -> scout (read-only work)
                              -> ship  (repository changes)

The actions are internal, not user modes. Repository context is attached to an answer when needed.
Scout/ship decisions carry complexity downstream so dispatch can choose the minimum sufficient
skills, model, provider, and harness. See docs/ORCHESTRATION.md.
"""

NO_REPOSITORY = (
    "Agent CLI runs repository work inside a Git repository. Start it from your project's directory."
)
"""Shown when scout/ship work is requested but no repository is open."""

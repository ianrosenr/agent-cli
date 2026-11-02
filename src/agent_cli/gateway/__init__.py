"""The root-prompt gateway (V1-09.9): ordinary text is answered, or planned as work.

::

    root prompt -> IntentRouter -> chat                -> DirectAnswerService
                                -> repository_question -> RepositoryAnswerService (read-only)
                                -> orchestration       -> OrchestrationService

Kept import-light: the shell imports this package at startup, and the services load on first
use. See docs/ORCHESTRATION.md.
"""

NO_REPOSITORY = (
    "Agent CLI plans work inside a Git repository. Start it from your project's directory."
)
"""Shown when text asks for repository changes but no repository is open."""

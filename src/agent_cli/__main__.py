"""Allow ``python -m agent_cli`` to start the same application as ``agent-cli``."""

from agent_cli.cli import main

raise SystemExit(main())

"""Built-in declarative profiles for reusable CLI harnesses."""

from agent_cli.adapters.cli_harness import AuthPattern, CliHarnessProfile

CLAUDE_CODE = CliHarnessProfile(
    id="claude-code",
    display_name="Claude Code",
    default_command="claude",
    provider_id="claude-subscription",
    provider_name="Claude subscription",
    auth_label="Claude subscription auth",
    auth_status_args=("auth", "status", "--json"),
    login_args=("auth", "login"),
    auth_pattern=AuthPattern(
        json_any=(
            {"loggedIn": True, "authMethod": "claude.ai"},
            {
                "loggedIn": True,
                "authMethod": "oauth_token",
                "subscriptionType": "*",
            },
        )
    ),
    env_remove=frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
        }
    ),
    output_protocol="json-result",
    worker_base_args=(
        "--print",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--max-turns",
        "25",
    ),
    worker_plan_args=("--permission-mode", "plan"),
    worker_write_args=("--permission-mode", "auto"),
    install_latest=("npm", "install", "-g", "@anthropic-ai/claude-code@latest"),
    install_exact=("npm", "install", "-g", "@anthropic-ai/claude-code@{version}"),
    update_latest=("{command}", "update"),
    update_exact=("npm", "install", "-g", "@anthropic-ai/claude-code@{version}"),
    direct_model_id="claude-sonnet-5-5",
    direct_model_name="Claude Sonnet 5.5",
    direct_context_limit=1_000_000,
    direct_base_args=(
        "--print",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--permission-mode",
        "plan",
        "--max-turns",
        "1",
    ),
)

CODEX_CLI = CliHarnessProfile(
    id="codex-cli",
    display_name="Codex CLI",
    default_command="codex",
    provider_id="openai-subscription",
    provider_name="OpenAI subscription",
    auth_label="ChatGPT subscription auth",
    auth_status_args=(
        "-c",
        'forced_login_method="chatgpt"',
        "-c",
        'model_provider="openai"',
        "-c",
        'openai_base_url=""',
        "login",
        "status",
    ),
    login_args=(
        "-c",
        'forced_login_method="chatgpt"',
        "-c",
        'model_provider="openai"',
        "-c",
        'openai_base_url=""',
        "login",
    ),
    auth_pattern=AuthPattern(source="stderr", contains="Logged in using ChatGPT"),
    env_remove=frozenset(
        {
            "CODEX_API_KEY",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "CODEX_ACCESS_TOKEN",
            "OPENAI_FEDERATION_RULE_ID",
            "OPENAI_IDENTITY_TOKEN_FILE",
            "OPENAI_WORKLOAD_IDENTITY_CONTEXT",
        }
    ),
    output_protocol="jsonl-agent",
    worker_base_args=(
        "-c",
        'forced_login_method="chatgpt"',
        "-c",
        'model_provider="openai"',
        "-c",
        'openai_base_url=""',
        "exec",
        "--json",
        "--ephemeral",
    ),
    worker_plan_args=("--sandbox", "read-only"),
    worker_write_args=("--sandbox", "workspace-write"),
    worker_stdin_marker="-",
    install_latest=("npm", "install", "-g", "@openai/codex@latest"),
    install_exact=("npm", "install", "-g", "@openai/codex@{version}"),
    update_latest=("npm", "install", "-g", "@openai/codex@latest"),
    update_exact=("npm", "install", "-g", "@openai/codex@{version}"),
    direct_model_id="gpt-6-sol",
    direct_model_name="GPT-6 Sol",
    direct_context_limit=500_000,
    direct_base_args=(
        "-c",
        'forced_login_method="chatgpt"',
        "-c",
        'model_provider="openai"',
        "-c",
        'openai_base_url=""',
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        "read-only",
    ),
    direct_stdin_marker="-",
)

BUILTIN_CLI_PROFILES = {
    CLAUDE_CODE.id: CLAUDE_CODE,
    CODEX_CLI.id: CODEX_CLI,
}

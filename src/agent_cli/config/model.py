"""Typed configuration models. Field names and defaults follow docs/CONFIGURATION.md.

These dataclasses are the single source of truth for the configuration schema: validation,
merging, and YAML key order are all derived from them (see ``agent_cli.config.schema``).
Per-field constraints live in field metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from agent_cli.terms import AccessKind, ImplementationMode

type SkillActivation = Literal["always", "auto", "explicit", "disabled"]
type Tier = Literal["low", "mid", "high"]
type ClassifierSelection = Literal["auto", "selected", "disabled"]
type Verbosity = Literal["concise", "normal", "verbose"]

MAX_WORKER_CONCURRENCY = 16


def constrained(default: Any, **constraints: Any) -> Any:
    """A field with an immutable default and validation constraints."""
    return field(default=default, metadata=constraints)


@dataclass(frozen=True, slots=True)
class CostConfig:
    """Hard limits on paid-API spend. ``None`` means no limit."""

    monthly_limit_usd: float | None = constrained(20.00, minimum=0)
    daily_limit_usd: float | None = constrained(None, minimum=0)
    per_task_limit_usd: float | None = constrained(1.00, minimum=0)
    per_call_limit_usd: float | None = constrained(0.30, minimum=0)


@dataclass(frozen=True, slots=True)
class AccessConfig:
    preference: tuple[AccessKind, ...] = constrained(
        ("subscription", "local", "api"), kind="unique-nonempty"
    )


@dataclass(frozen=True, slots=True)
class ImplementationConfig:
    mode: ImplementationMode = "autopilot"


@dataclass(frozen=True, slots=True)
class ReviewConfig:
    before_merge_request: bool = True
    max_rounds: int = constrained(3, minimum=1)


@dataclass(frozen=True, slots=True)
class WorkersConfig:
    max_concurrency: int = constrained(3, minimum=1, maximum=MAX_WORKER_CONCURRENCY)


@dataclass(frozen=True, slots=True)
class GitConfig:
    use_worktrees: bool = True
    worktree_dir: str = constrained(".agent-cli/worktrees", kind="worktree-dir")
    branch_prefix: str = constrained("agent-cli/", kind="branch-prefix")


@dataclass(frozen=True, slots=True)
class SkillsConfig:
    paths: tuple[str, ...] = constrained((), kind="paths")
    activation: dict[str, SkillActivation] = field(default_factory=dict)
    context_budget_tokens: int = constrained(8000, minimum=0)


@dataclass(frozen=True, slots=True)
class UiConfig:
    verbosity: Verbosity = "concise"


@dataclass(frozen=True, slots=True)
class CliProfileConfig:
    """Declarative behavior for one reusable CLI harness profile."""

    display_name: str = constrained("", kind="required")
    default_command: str = constrained("", kind="required")
    provider_id: str = constrained("", kind="required")
    provider_name: str = constrained("", kind="required")
    auth_label: str = constrained("", kind="required")
    auth_status_args: tuple[str, ...] = constrained((), kind="required")
    login_args: tuple[str, ...] = constrained((), kind="required")
    auth_source: Literal["stdout", "stderr"] = "stdout"
    auth_regexes: tuple[str, ...] = constrained((), kind="required")
    env_remove: tuple[str, ...] = ()
    output_protocol: Literal["json-result", "jsonl-agent"] = "json-result"
    worker_base_args: tuple[str, ...] = ()
    worker_plan_args: tuple[str, ...] = ()
    worker_research_args: tuple[str, ...] | None = None
    worker_diagnostic_args: tuple[str, ...] | None = None
    worker_write_args: tuple[str, ...] = ()
    worker_model_flag: str = constrained("--model", kind="nonempty")
    worker_stdin_marker: str | None = constrained(None, kind="nonempty")
    install_latest: tuple[str, ...] = ()
    install_exact: tuple[str, ...] = ()
    update_latest: tuple[str, ...] = ()
    update_exact: tuple[str, ...] = ()
    direct_model_id: str | None = constrained(None, kind="nonempty")
    direct_model_name: str | None = constrained(None, kind="nonempty")
    direct_context_limit: int | None = constrained(None, minimum=1)
    direct_base_args: tuple[str, ...] = ()
    direct_model_flag: str = constrained("--model", kind="nonempty")
    direct_stdin_marker: str | None = constrained(None, kind="nonempty")


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    """A configured external harness.

    `type` chooses a reusable adapter protocol. `profile` chooses declarative behavior for
    that protocol. Both are required in persisted harness entries. Runtime facts are never
    stored here.
    """

    type: str = constrained("", kind="required")
    profile: str = constrained("", kind="required")
    enabled: bool = True
    command: str | None = None
    version: str = constrained("latest", kind="nonempty")


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """A configured provider. Credentials are references only, never values."""

    type: str = constrained("", kind="required")
    base_url: str | None = None
    api_key_env: str | None = None
    api_key_keyring: str | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class RoleFloors:
    """Minimum capability per canonical role (``low`` = 2, ``mid`` = 3, ``high`` = 4).

    Fields are exactly ``agent_cli.terms.CANONICAL_ROLES``, in that order.
    """

    orchestrator: Tier = "high"
    spec_author: Tier = "high"
    spec_reviewer: Tier = "mid"
    architect: Tier = "high"
    implementer: Tier = "low"
    code_reviewer: Tier = "mid"
    research_investigator: Tier = "high"
    security_investigator: Tier = "high"
    skill_author: Tier = "high"
    skill_evaluator: Tier = "high"


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    """Model selection for root conversational intake."""

    mode: ClassifierSelection = "auto"
    selected_model: str | None = constrained(None, kind="nonempty")


@dataclass(frozen=True, slots=True)
class ClassifierConfig:
    """Which model, if any, classifies skill-routing signals.

    ``selected_model`` may stay stored in any mode; it is only used in ``selected`` mode, which
    requires it.
    """

    mode: ClassifierSelection = "auto"
    selected_model: str | None = constrained(None, kind="nonempty")


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    """Advanced settings. Loaded from config but never shown by ``/startup``."""

    role_floors: RoleFloors = field(default_factory=RoleFloors)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)


@dataclass(frozen=True, slots=True)
class Config:
    """The effective configuration after all layers are merged and validated."""

    cost: CostConfig = field(default_factory=CostConfig)
    access: AccessConfig = field(default_factory=AccessConfig)
    implementation: ImplementationConfig = field(default_factory=ImplementationConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    workers: WorkersConfig = field(default_factory=WorkersConfig)
    git: GitConfig = field(default_factory=GitConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    harness_profiles: dict[str, CliProfileConfig] = field(default_factory=dict)
    harnesses: dict[str, HarnessConfig] = field(default_factory=dict)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    routing: RoutingConfig = field(default_factory=RoutingConfig)

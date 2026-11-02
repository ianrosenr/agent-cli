"""Bounded implementation authorization for ship tasks.

Authorization is separate from role, skills, planning, and harness permissions. This module decides
whether a prepared ship may receive implementation authority; adapters only enforce the resulting
authority.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from rich.console import Group, RenderableType
from rich.text import Text

from agent_cli.config import Config
from agent_cli.state import Approval, SpecVersion, Task, TaskAuthorization
from agent_cli.terms import TaskComplexity
from agent_cli.ui import blocks, muted, styled

type AuthorizationAction = Literal["grant", "checkpoint", "block"]
type AuthorizationAnswer = Literal["approve", "reject", "unclear"]

HIGH_RISK_SIGNALS = frozenset(
    {
        "agent_instruction_change",
        "auth_change",
        "authentication",
        "authorization",
        "credential",
        "cryptography",
        "irreversible_decision",
        "migration",
        "model_selection_policy_change",
        "privilege",
        "routing_policy_change",
        "secret",
        "system_prompt_change",
        "trust_boundary",
    }
)


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    action: AuthorizationAction
    reason: str


def decide_authorization(
    config: Config,
    complexity: TaskComplexity,
    spec: SpecVersion | None,
    risk_signals: Iterable[str] = (),
) -> AuthorizationDecision:
    """Apply proportional write policy without consulting a model."""
    mode = config.implementation.mode
    if mode == "plan-only":
        return AuthorizationDecision(
            "block",
            "plan-only mode never grants implementation authority",
        )
    if mode == "checkpoint":
        return AuthorizationDecision(
            "checkpoint",
            "checkpoint mode requires explicit user approval before source changes",
        )
    risky = sorted(HIGH_RISK_SIGNALS.intersection(risk_signals))
    if risky:
        return AuthorizationDecision(
            "checkpoint",
            "risk-sensitive work requires explicit approval: " + ", ".join(risky),
        )
    if complexity == "complex":
        return AuthorizationDecision(
            "checkpoint",
            "complex work requires an explicit checkpoint even in autopilot",
        )
    if complexity == "moderate" and spec is None:
        return AuthorizationDecision(
            "block",
            "moderate work needs a bounded implementation spec before write authority",
        )
    return AuthorizationDecision(
        "grant",
        "autopilot may grant bounded implementation authority for this prepared task",
    )


APPROVE = {
    "yes",
    "y",
    "approve",
    "approved",
    "go ahead",
    "go ahead with it",
    "proceed",
    "do it",
    "sounds good",
    "ok",
    "okay",
    "yes go ahead",
}
REJECT = {
    "no",
    "n",
    "reject",
    "stop",
    "cancel",
    "not yet",
    "do not",
    "don't",
}


def authorization_answer(text: str) -> AuthorizationAnswer:
    """Interpret a reply only while an explicit authorization gate is pending."""
    value = " ".join(text.casefold().strip().rstrip(".!").split())
    if value in APPROVE:
        return "approve"
    if value in REJECT:
        return "reject"
    return "unclear"


def authorization_scope(task: Task, spec: SpecVersion | None) -> str:
    return spec.body.strip() if spec is not None else task.goal.strip()


def render_authorization_request(
    authorization: TaskAuthorization,
    task: Task,
    spec: SpecVersion | None,
) -> RenderableType:
    scope = authorization_scope(task, spec)
    requested = Group(
        styled("Authorization required", "step"),
        Text(task.goal),
        Text.assemble(("Why: ", "muted"), authorization.reason),
        Text.assemble(
            ("Write authority: ", "muted"),
            ("implementation", "value"),
        ),
    )
    details = Text(scope)
    return blocks(
        requested,
        details,
        muted(
            "Approve or reject this bounded implementation. "
            "The next line is treated as your answer."
        ),
    )


def render_authorization_result(
    authorization: TaskAuthorization,
    message: str,
) -> RenderableType:
    state = "Authorized" if authorization.status is Approval.APPROVED else "Not authorized"
    style = "success" if authorization.status is Approval.APPROVED else "warning"
    return blocks(styled(state, style), Text(message))

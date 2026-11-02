from __future__ import annotations

from dataclasses import replace

from agent_cli.config import Config
from agent_cli.config.model import ImplementationConfig
from agent_cli.orchestration.authorization import (
    authorization_answer,
    decide_authorization,
)
from agent_cli.state import Approval, SpecVersion


def spec() -> SpecVersion:
    return SpecVersion(
        task_id="task-1",
        version=1,
        body="Change only the bounded cache behavior.",
        diff="",
        approval=Approval.PENDING,
        created_at="now",
    )


def config(mode: str) -> Config:
    return replace(Config(), implementation=ImplementationConfig(mode=mode))  # type: ignore[arg-type]


def test_autopilot_grants_trivial_ship_without_spec() -> None:
    decision = decide_authorization(config("autopilot"), "trivial", None)
    assert decision.action == "grant"


def test_autopilot_requires_spec_for_moderate_ship() -> None:
    missing = decide_authorization(config("autopilot"), "moderate", None)
    bounded = decide_authorization(config("autopilot"), "moderate", spec())
    assert missing.action == "block"
    assert bounded.action == "grant"


def test_complex_ship_requires_checkpoint_even_in_autopilot() -> None:
    decision = decide_authorization(config("autopilot"), "complex", spec())
    assert decision.action == "checkpoint"


def test_checkpoint_always_requires_explicit_user_approval() -> None:
    decision = decide_authorization(config("checkpoint"), "trivial", None)
    assert decision.action == "checkpoint"


def test_plan_only_never_grants_implementation_authority() -> None:
    decision = decide_authorization(config("plan-only"), "trivial", None)
    assert decision.action == "block"


def test_pending_authorization_reply_is_contextual_and_strict() -> None:
    assert authorization_answer("go ahead") == "approve"
    assert authorization_answer("No.") == "reject"
    assert authorization_answer("tell me more first") == "unclear"

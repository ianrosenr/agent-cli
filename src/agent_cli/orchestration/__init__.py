"""Supervisor task preparation and conditional decomposition (V1-09).

Trivial scout/ship work becomes one typed task directly. Moderate/complex work may use the planner
to produce a validated role-explicit graph. See docs/ORCHESTRATION.md.
"""

from agent_cli.orchestration.calls import (
    DirectModelCall,
    ModelCall,
    ModelReply,
    PlanningUnavailable,
)
from agent_cli.orchestration.domain import (
    DELEGATED_ROLES,
    FileFacts,
    Handoff,
    PlanningInput,
    PlanProposal,
    PlanRejected,
    PriorDecision,
    ProposedQuestion,
    RepositoryContext,
    TaskBrief,
    UserText,
    ValidatedPlan,
    WorkItem,
    validate_proposal,
)
from agent_cli.orchestration.inspection import goal_anchors
from agent_cli.orchestration.output import ModelOutputError
from agent_cli.orchestration.planner import FakePlanner, Planner, ProviderPlanner, parse_proposal
from agent_cli.orchestration.service import OrchestrationService
from agent_cli.orchestration.spec_author import (
    SECTIONS,
    FakeSpecAuthor,
    ProviderSpecAuthor,
    SpecAuthor,
    SpecContext,
    SpecDraft,
    SpecRequest,
    parse_specs,
    render_spec,
)

__all__ = [
    "DELEGATED_ROLES",
    "SECTIONS",
    "DirectModelCall",
    "FakePlanner",
    "FakeSpecAuthor",
    "FileFacts",
    "Handoff",
    "ModelCall",
    "ModelOutputError",
    "ModelReply",
    "OrchestrationService",
    "PlanProposal",
    "PlanRejected",
    "Planner",
    "PlanningInput",
    "PlanningUnavailable",
    "PriorDecision",
    "ProposedQuestion",
    "ProviderPlanner",
    "ProviderSpecAuthor",
    "RepositoryContext",
    "SpecAuthor",
    "SpecContext",
    "SpecDraft",
    "SpecRequest",
    "TaskBrief",
    "UserText",
    "ValidatedPlan",
    "WorkItem",
    "goal_anchors",
    "parse_proposal",
    "parse_specs",
    "render_spec",
    "validate_proposal",
]

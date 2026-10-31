"""Root-prompt orchestration planning (V1-09).

Ordinary text at the prompt becomes a root ``orchestrator`` task, a validated role-explicit child
graph, V1-06 skill routing, and pending short specs, then planning stops. The planner proposes
typed work; validation and ``TaskStore.persist_plan`` decide what becomes canonical state. See
docs/ORCHESTRATION.md.
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
    PlanningInput,
    PlanProposal,
    PlanRejected,
    PriorDecision,
    ProposedQuestion,
    RepositoryContext,
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
    "UserText",
    "ValidatedPlan",
    "WorkItem",
    "goal_anchors",
    "parse_proposal",
    "parse_specs",
    "render_spec",
    "validate_proposal",
]

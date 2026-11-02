"""Adapters to external harnesses and providers, behind normalized interfaces.

Agent CLI is the control plane; harnesses (Pi, Codex CLI, Claude Code, OpenCode, ...) execute
coding agents and providers serve models. Core code depends only on the types exported here;
vendor-specific code lives in individual adapter implementations.
"""

from agent_cli.adapters.domain import (
    DIAGNOSTIC_AUTHORITY,
    IMPLEMENTATION_AUTHORITY,
    RESEARCH_AUTHORITY,
    AdapterError,
    Capability,
    Changes,
    ErrorKind,
    Failure,
    HarnessCapabilities,
    HarnessIdentity,
    HarnessReadiness,
    ModelDescriptor,
    ProviderIdentity,
    ProviderReadiness,
    ProviderRequest,
    ProviderResponse,
    Readiness,
    ReadinessState,
    Selection,
    Usage,
    WorkerAuthority,
    WorkerHandle,
    WorkerRequest,
    WorkerResult,
    WorkerStatus,
)
from agent_cli.adapters.harness import HarnessAdapter
from agent_cli.adapters.harness_provider import HarnessBackedProviderAdapter
from agent_cli.adapters.provider import ProviderAdapter
from agent_cli.adapters.registry import AdapterRegistry
from agent_cli.adapters.status import adapter_status, providers_view

__all__ = [
    "DIAGNOSTIC_AUTHORITY",
    "IMPLEMENTATION_AUTHORITY",
    "RESEARCH_AUTHORITY",
    "AdapterError",
    "AdapterRegistry",
    "Capability",
    "Changes",
    "ErrorKind",
    "Failure",
    "HarnessAdapter",
    "HarnessBackedProviderAdapter",
    "HarnessCapabilities",
    "HarnessIdentity",
    "HarnessReadiness",
    "ModelDescriptor",
    "ProviderAdapter",
    "ProviderIdentity",
    "ProviderReadiness",
    "ProviderRequest",
    "ProviderResponse",
    "Readiness",
    "ReadinessState",
    "Selection",
    "Usage",
    "WorkerAuthority",
    "WorkerHandle",
    "WorkerRequest",
    "WorkerResult",
    "WorkerStatus",
    "adapter_status",
    "providers_view",
]

"""Normalized domain objects shared by every harness and provider adapter.

These types are the whole vocabulary orchestration may use to talk about external execution.
They carry no vendor-specific fields: an adapter translates between them and its harness's or
provider's native commands, payloads, and identifiers.

Everything here is runtime state discovered or produced while Agent CLI runs. None of it is
configuration and none of it is written to config files (see ``agent_cli.config``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from agent_cli.terms import AccessKind, ImplementationMode

# -- Errors ------------------------------------------------------------------------------------


class ErrorKind(StrEnum):
    """Normalized failure categories, for later health and fallback policy (V1-13)."""

    NOT_FOUND = "not_found"
    AUTH_REQUIRED = "auth_required"
    AUTH_FAILED = "auth_failed"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED = "unsupported"
    """The adapter does not offer the requested optional capability."""
    INCOMPATIBLE = "incompatible"
    """The adapter cannot satisfy the request's model/provider/harness pins."""
    PROVIDER_ERROR = "provider_error"
    HARNESS_ERROR = "harness_error"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class Failure:
    """A normalized failure as plain data, so it can be compared, stored, and serialized."""

    kind: ErrorKind
    message: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}"


class AdapterError(Exception):
    """The only exception an adapter lets escape. Vendor exceptions are translated into it."""

    def __init__(self, kind: ErrorKind, message: str) -> None:
        self.failure = Failure(kind, message)
        super().__init__(str(self.failure))

    @property
    def kind(self) -> ErrorKind:
        return self.failure.kind

    @property
    def message(self) -> str:
        return self.failure.message


# -- Readiness ---------------------------------------------------------------------------------


class ReadinessState(StrEnum):
    """Whether a harness or provider can be used right now."""

    NOT_DETECTED = "not_detected"
    DETECTED = "detected"
    """Found, but readiness (for example authentication) has not been confirmed."""
    READY = "ready"
    AUTH_REQUIRED = "auth_required"
    MISCONFIGURED = "misconfigured"
    UNAVAILABLE = "unavailable"


READINESS_LABELS: dict[ReadinessState, str] = {
    ReadinessState.NOT_DETECTED: "Not detected",
    ReadinessState.DETECTED: "Detected",
    ReadinessState.READY: "Ready",
    ReadinessState.AUTH_REQUIRED: "Sign-in required",
    ReadinessState.MISCONFIGURED: "Misconfigured",
    ReadinessState.UNAVAILABLE: "Unavailable",
}


BLOCKING_ERRORS: dict[ReadinessState, ErrorKind] = {
    ReadinessState.NOT_DETECTED: ErrorKind.NOT_FOUND,
    ReadinessState.AUTH_REQUIRED: ErrorKind.AUTH_REQUIRED,
    ReadinessState.MISCONFIGURED: ErrorKind.INVALID_REQUEST,
    ReadinessState.UNAVAILABLE: ErrorKind.UNAVAILABLE,
}
"""How each not-ready state is reported when work is requested anyway. ``DETECTED`` may run:
readiness is unconfirmed, and the harness or provider reports its own failure if it cannot."""


@dataclass(frozen=True, slots=True)
class Readiness:
    state: ReadinessState
    reason: str = ""
    """Concise human-readable evidence, such as what was found or what to do next."""

    @property
    def ready(self) -> bool:
        return self.state is ReadinessState.READY

    def describe(self) -> str:
        label = READINESS_LABELS[self.state]
        return f"{label} ({self.reason})" if self.reason else label

    def blocking_error(self) -> AdapterError | None:
        """The error to raise when work is requested in this state; ``None`` if it may run."""
        kind = BLOCKING_ERRORS.get(self.state)
        return None if kind is None else AdapterError(kind, self.describe())


@dataclass(frozen=True, slots=True)
class HarnessReadiness(Readiness):
    """Runtime readiness of a harness. Never stored in configuration."""


@dataclass(frozen=True, slots=True)
class ProviderReadiness(Readiness):
    """Runtime readiness and health of a provider. Never stored in configuration."""


# -- Harness capabilities ----------------------------------------------------------------------


class Capability(StrEnum):
    """An optional harness feature. Nothing is assumed: an adapter lists what it supports."""

    START_WORKER = "start_worker"
    FOLLOW_UP = "follow_up"
    INTERRUPT = "interrupt"
    MODEL_SELECTION = "model_selection"
    PROVIDER_SELECTION = "provider_selection"
    WORKING_DIRECTORY = "working_directory"
    ISOLATED_WORKTREE = "isolated_worktree"
    DIFF = "diff"
    USAGE = "usage"
    RESUMABLE_SESSIONS = "resumable_sessions"


@dataclass(frozen=True, slots=True)
class HarnessCapabilities:
    """What a harness can do, and which models and providers it can run.

    ``models`` and ``providers`` are compatibility constraints: ``None`` means the harness
    does not restrict them; a set lists the only IDs it accepts.
    """

    supported: frozenset[Capability] = frozenset()
    models: frozenset[str] | None = None
    providers: frozenset[str] | None = None

    def supports(self, capability: Capability) -> bool:
        return capability in self.supported


@dataclass(frozen=True, slots=True)
class HarnessIdentity:
    id: str
    display_name: str
    version: str | None = None


# -- Workers -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Selection:
    """A chosen model, provider, or harness ID, and whether the user pinned it explicitly.

    Adapters carry selections unchanged and never substitute another. ``pinned`` tells
    routing that, if no adapter can honor it, the user must be asked rather than offered an
    alternative.
    """

    id: str
    pinned: bool = False

    @classmethod
    def pin(cls, id: str) -> Selection:
        return cls(id, pinned=True)


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    """Everything a harness needs to run one unit of work. Holds no conversation history."""

    task_id: str
    role: str
    spec: str
    """The task or spec text, or a reference to it."""
    working_directory: Path
    """The directory or worktree the worker runs in."""
    repository: str | None = None
    """The targeted repository context, such as its root or a short description."""
    skills: tuple[str, ...] = ()
    """Selected skills and policy context, already rendered for the worker."""
    model: Selection | None = None
    provider: Selection | None = None
    """The provider/access path."""
    harness: Selection | None = None
    """The harness routing chose. The adapter receiving the request must be this harness."""
    mode: ImplementationMode | None = None


@dataclass(frozen=True, slots=True)
class WorkerHandle:
    """An opaque reference to a started worker. Core code never interprets ``token``."""

    harness_id: str
    token: str


class WorkerStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    WAITING = "waiting"
    """Paused for input, such as a follow-up message."""
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def finished(self) -> bool:
        return self in (WorkerStatus.COMPLETED, WorkerStatus.FAILED, WorkerStatus.CANCELED)


@dataclass(frozen=True, slots=True)
class Usage:
    """Reported usage. Any field may be missing when the harness or provider does not say."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    """Monetary cost as reported by the harness or provider, never estimated here."""
    reference: str | None = None
    """A native request or session reference, when safe and useful for tracing."""

    @property
    def tokens(self) -> int | None:
        """Total tokens, derived from input and output when not reported directly."""
        if self.total_tokens is not None:
            return self.total_tokens
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class Changes:
    """What a worker changed. Either part may be missing."""

    files: tuple[str, ...] = ()
    diff: str | None = None
    """A unified diff, or a reference to where one can be read."""


@dataclass(frozen=True, slots=True)
class WorkerResult:
    status: WorkerStatus
    summary: str = ""
    artifacts: tuple[str, ...] = ()
    changes: Changes | None = None
    usage: Usage | None = None
    error: Failure | None = None
    """Why the worker failed, when ``status`` is ``FAILED``."""


# -- Providers ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    id: str
    display_name: str
    type: str
    """The provider type, matching ``ProviderConfig.type`` (for example ``openai-compatible``)."""
    access: AccessKind | None = None


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    """A model a provider reports, normalized from the provider's own listing."""

    provider_id: str
    """The access path this descriptor was discovered through."""
    model_id: str
    display_name: str | None = None
    context_limit: int | None = None
    available: bool = True


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """A direct model call made by Agent CLI itself (classification, planning, research)."""

    model_id: str
    prompt: str
    system: str | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    model_id: str
    text: str
    usage: Usage | None = None

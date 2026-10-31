"""Configurable in-memory adapters for tests and for exercising orchestration offline.

They implement the same interfaces as real adapters and follow the same rules: optional
operations they are not configured to support raise ``UNSUPPORTED``, and requests they cannot
run exactly as asked raise ``INCOMPATIBLE``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from agent_cli.adapters.domain import (
    AdapterError,
    Capability,
    Changes,
    ErrorKind,
    HarnessCapabilities,
    HarnessIdentity,
    HarnessReadiness,
    ModelDescriptor,
    ProviderIdentity,
    ProviderReadiness,
    ProviderRequest,
    ProviderResponse,
    ReadinessState,
    Usage,
    WorkerHandle,
    WorkerRequest,
    WorkerResult,
    WorkerStatus,
)
from agent_cli.adapters.harness import HarnessAdapter, unsupported
from agent_cli.adapters.provider import ProviderAdapter

ALL_CAPABILITIES = frozenset(Capability)


@dataclass(slots=True)
class FakeWorker:
    request: WorkerRequest
    outcome: WorkerResult
    status: WorkerStatus
    messages: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FakeHarnessAdapter(HarnessAdapter):
    """A harness whose workers finish with a scripted outcome, or run until ``finish()``.

    Each worker's outcome is ``script(request)`` when ``script`` is set, otherwise ``outcome``,
    so tests can give different subtasks different results.
    """

    harness_identity: HarnessIdentity = field(
        default_factory=lambda: HarnessIdentity("fake", "Fake harness", "1.0")
    )
    harness_readiness: HarnessReadiness = field(
        default_factory=lambda: HarnessReadiness(ReadinessState.READY)
    )
    harness_capabilities: HarnessCapabilities = field(
        default_factory=lambda: HarnessCapabilities(ALL_CAPABILITIES)
    )
    outcome: WorkerResult = field(
        default_factory=lambda: WorkerResult(WorkerStatus.COMPLETED, "Done.")
    )
    script: Callable[[WorkerRequest], WorkerResult] | None = None
    start_error: AdapterError | None = None
    """Raised by ``start``, to simulate a harness failing to launch a worker."""
    auto_finish: bool = True
    """When false, workers stay ``RUNNING`` until ``finish()`` is called."""
    workers: dict[str, FakeWorker] = field(default_factory=dict)

    def identity(self) -> HarnessIdentity:
        return self.harness_identity

    def readiness(self) -> HarnessReadiness:
        return self.harness_readiness

    def capabilities(self) -> HarnessCapabilities:
        return self.harness_capabilities

    def login(self) -> HarnessReadiness:
        self.require(Capability.LOGIN)
        self.harness_readiness = HarnessReadiness(ReadinessState.READY)
        return self.harness_readiness

    def start(self, request: WorkerRequest) -> WorkerHandle:
        self.require(Capability.START_WORKER)
        blocked = self.harness_readiness.blocking_error()
        if blocked is not None:
            raise blocked
        self.check(request)
        if self.start_error is not None:
            raise self.start_error
        token = f"worker-{len(self.workers) + 1}"
        outcome = self.script(request) if self.script is not None else self.outcome
        status = outcome.status if self.auto_finish else WorkerStatus.RUNNING
        self.workers[token] = FakeWorker(request, outcome, status)
        return WorkerHandle(self.harness_identity.id, token)

    def status(self, handle: WorkerHandle) -> WorkerStatus:
        return self.worker(handle).status

    def result(self, handle: WorkerHandle) -> WorkerResult:
        worker = self.worker(handle)
        if worker.status is worker.outcome.status:
            return worker.outcome
        return WorkerResult(worker.status)

    def send(self, handle: WorkerHandle, message: str) -> None:
        self.require(Capability.FOLLOW_UP)
        worker = self.worker(handle)
        if worker.status.finished:
            raise AdapterError(ErrorKind.INVALID_REQUEST, f"worker is {worker.status}")
        worker.messages.append(message)

    def interrupt(self, handle: WorkerHandle) -> None:
        self.require(Capability.INTERRUPT)
        worker = self.worker(handle)
        if not worker.status.finished:
            worker.status = WorkerStatus.CANCELED

    def diff(self, handle: WorkerHandle) -> Changes:
        self.require(Capability.DIFF)
        return self.result(handle).changes or Changes()

    def usage(self, handle: WorkerHandle) -> Usage:
        self.require(Capability.USAGE)
        return self.result(handle).usage or Usage()

    def finish(self, handle: WorkerHandle) -> None:
        """Move a running worker to its scripted outcome."""
        worker = self.worker(handle)
        worker.status = worker.outcome.status

    def require(self, capability: Capability) -> None:
        if not self.harness_capabilities.supports(capability):
            raise unsupported(self.harness_identity, capability)

    def worker(self, handle: WorkerHandle) -> FakeWorker:
        worker = self.workers.get(handle.token)
        if handle.harness_id != self.harness_identity.id or worker is None:
            raise AdapterError(ErrorKind.NOT_FOUND, f"no worker {handle.token!r}")
        return worker


@dataclass(slots=True)
class FakeProviderAdapter(ProviderAdapter):
    """A provider that answers every call with ``reply``, or fails with ``error``."""

    provider_identity: ProviderIdentity = field(
        default_factory=lambda: ProviderIdentity("fake", "Fake provider", "fake", "api")
    )
    provider_readiness: ProviderReadiness = field(
        default_factory=lambda: ProviderReadiness(ReadinessState.READY)
    )
    models: Sequence[ModelDescriptor] = ()
    reply: str = "ok"
    usage: Usage | None = None
    error: AdapterError | None = None
    """Raised by ``invoke``, to simulate a provider failure."""
    requests: list[ProviderRequest] = field(default_factory=list)

    def identity(self) -> ProviderIdentity:
        return self.provider_identity

    def readiness(self) -> ProviderReadiness:
        return self.provider_readiness

    def list_models(self) -> Sequence[ModelDescriptor]:
        if self.error is not None:
            raise self.error
        return [replace(model, provider_id=self.provider_identity.id) for model in self.models]

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        blocked = self.provider_readiness.blocking_error()
        if blocked is not None:
            raise blocked
        if self.error is not None:
            raise self.error
        model = next((m for m in self.models if m.model_id == request.model_id), None)
        if model is None:
            raise AdapterError(ErrorKind.NOT_FOUND, f"unknown model {request.model_id!r}")
        if not model.available:
            raise AdapterError(ErrorKind.UNAVAILABLE, f"model {request.model_id!r} is inactive")
        return ProviderResponse(request.model_id, self.reply, self.usage)

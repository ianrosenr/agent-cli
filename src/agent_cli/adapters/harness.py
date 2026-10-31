"""The interface every harness adapter implements.

A harness adapter translates Agent CLI's normalized worker contract into one existing
coding-agent harness's commands, SDK, RPC, or process. It reports facts and executes requests;
routing, budgets, retries, and fallback are decided elsewhere.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from agent_cli.adapters.domain import (
    AdapterError,
    Capability,
    Changes,
    ErrorKind,
    HarnessCapabilities,
    HarnessIdentity,
    HarnessReadiness,
    Selection,
    Usage,
    WorkerHandle,
    WorkerRequest,
    WorkerResult,
    WorkerStatus,
)


def unsupported(identity: HarnessIdentity, capability: Capability) -> AdapterError:
    return AdapterError(
        ErrorKind.UNSUPPORTED, f"{identity.display_name} does not support {capability}"
    )


def incompatibilities(
    identity: HarnessIdentity, capabilities: HarnessCapabilities, request: WorkerRequest
) -> list[str]:
    """Why this harness cannot run ``request`` as asked; empty when it can.

    A selected model or provider is accepted when the harness lists it, or when the harness
    lets callers choose freely. It is reported when neither holds, never replaced.
    """
    reasons: list[str] = []
    if request.harness is not None and request.harness.id != identity.id:
        reasons.append(f"{describe('harness', request.harness)} is required")
    checks = (
        ("model", request.model, Capability.MODEL_SELECTION, capabilities.models),
        ("provider", request.provider, Capability.PROVIDER_SELECTION, capabilities.providers),
    )
    for name, wanted, capability, accepted in checks:
        if wanted is None:
            continue
        if accepted is not None:
            if wanted.id not in accepted:
                reasons.append(f"cannot run {describe(name, wanted)}")
        elif not capabilities.supports(capability):
            reasons.append(f"cannot select {describe(name, wanted)}")
    return reasons


def describe(name: str, selection: Selection) -> str:
    return f"{'pinned ' if selection.pinned else ''}{name} {selection.id!r}"


class HarnessAdapter(ABC):
    """Runs workers in one external harness.

    Every method raises only ``AdapterError``. Optional operations default to raising
    ``ErrorKind.UNSUPPORTED``; an adapter overrides those it supports and lists them in
    ``capabilities()``.
    """

    @abstractmethod
    def identity(self) -> HarnessIdentity: ...

    @abstractmethod
    def readiness(self) -> HarnessReadiness:
        """Detect the harness and report whether it can run workers now.

        Never raises, and returns promptly: setup screens call it synchronously, so any
        process or network check must be bounded by a short timeout (a timeout is reported as
        ``UNAVAILABLE``).
        """

    @abstractmethod
    def capabilities(self) -> HarnessCapabilities: ...

    @abstractmethod
    def start(self, request: WorkerRequest) -> WorkerHandle:
        """Start a worker. Raises ``INCOMPATIBLE`` rather than substituting a selection.

        Implementations call ``check`` and ``readiness().blocking_error()`` first.
        """

    @abstractmethod
    def status(self, handle: WorkerHandle) -> WorkerStatus: ...

    @abstractmethod
    def result(self, handle: WorkerHandle) -> WorkerResult:
        """The worker's result so far; final once ``status`` is finished."""

    def login(self) -> HarnessReadiness:
        """Launch the harness's native interactive login, then return fresh readiness."""
        raise unsupported(self.identity(), Capability.LOGIN)

    def send(self, handle: WorkerHandle, message: str) -> None:
        """Send a follow-up message to a running or waiting worker."""
        raise unsupported(self.identity(), Capability.FOLLOW_UP)

    def interrupt(self, handle: WorkerHandle) -> None:
        """Cancel the worker."""
        raise unsupported(self.identity(), Capability.INTERRUPT)

    def diff(self, handle: WorkerHandle) -> Changes:
        raise unsupported(self.identity(), Capability.DIFF)

    def usage(self, handle: WorkerHandle) -> Usage:
        raise unsupported(self.identity(), Capability.USAGE)

    def check(self, request: WorkerRequest) -> None:
        """Raise ``INCOMPATIBLE`` when this harness cannot run ``request`` exactly as asked."""
        identity = self.identity()
        reasons = incompatibilities(identity, self.capabilities(), request)
        if reasons:
            raise AdapterError(
                ErrorKind.INCOMPATIBLE, f"{identity.display_name}: " + "; ".join(reasons)
            )

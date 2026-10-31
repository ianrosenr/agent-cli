"""Resolving a provider's credential reference into a secret held only in memory.

Config stores references, never values (see docs/CONFIGURATION.md#credentials)::

    ProviderConfig -> credential reference -> secret value in memory only

A ``Secret`` never shows its value in ``repr``, ``str``, or an exception message. Only the
adapter that sends it to its provider reads ``Secret.reveal()``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from agent_cli.config.model import ProviderConfig

KEYRING_DEFAULT_SERVICE = "agent-cli"
"""The keyring service used when ``api_key_keyring`` names only an entry (no ``service/``)."""

type KeyringLookup = Callable[[str, str], str | None]
"""``(service, username) -> password``, like ``keyring.get_password``."""


class Secret:
    """A credential value. Its text is reachable only through ``reveal()``."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(****)"

    __str__ = __repr__

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("a Secret cannot be pickled")


class CredentialState(StrEnum):
    RESOLVED = "resolved"
    NO_REFERENCE = "no_reference"
    """Neither ``api_key_env`` nor ``api_key_keyring`` is configured."""
    CONFLICTING = "conflicting"
    """Both are configured; which one to use is ambiguous."""
    ABSENT = "absent"
    """The reference is configured, but the secret it names is missing or empty."""


@dataclass(frozen=True, slots=True)
class Credential:
    state: CredentialState
    reference: str
    """Where the secret comes from, safe to show (``env ANTHROPIC_API_KEY``)."""
    secret: Secret | None = field(default=None, repr=False)

    @property
    def resolved(self) -> bool:
        return self.secret is not None

    def problem(self) -> str:
        """What is wrong, in words safe to show; empty when resolved."""
        return {
            CredentialState.RESOLVED: "",
            CredentialState.NO_REFERENCE: "no credential reference: set api_key_env or "
            "api_key_keyring",
            CredentialState.CONFLICTING: "both api_key_env and api_key_keyring are set; "
            "keep exactly one",
            CredentialState.ABSENT: f"{self.reference} is not set",
        }[self.state]


def keyring_entry(reference: str) -> tuple[str, str]:
    """``service/name`` splits at the first ``/``; a bare ``name`` uses the agent-cli service."""
    service, slash, name = reference.partition("/")
    if slash and service and name:
        return service, name
    return KEYRING_DEFAULT_SERVICE, reference


def system_keyring(service: str, username: str) -> str | None:
    """The OS keyring via the ``keyring`` package, imported only when a reference needs it."""
    import keyring
    from keyring.errors import KeyringError

    try:
        return keyring.get_password(service, username)
    except KeyringError:
        return None


@dataclass(frozen=True, slots=True)
class CredentialResolver:
    """Resolves references against the environment and keyring. Tests inject both."""

    environ: Mapping[str, str] = field(default_factory=lambda: os.environ, repr=False)
    keyring: KeyringLookup = field(default=system_keyring, repr=False)

    def resolve(self, provider: ProviderConfig) -> Credential:
        env, entry = provider.api_key_env, provider.api_key_keyring
        if env and entry:
            return Credential(CredentialState.CONFLICTING, f"env {env} and keyring {entry}")
        if env:
            return self.found(f"env {env}", self.environ.get(env))
        if entry:
            reference = f"keyring {entry}"
            try:
                value = self.keyring(*keyring_entry(entry))
            except Exception:
                # A broken keyring backend is reported as a missing secret, never raised.
                value = None
            return self.found(reference, value)
        return Credential(CredentialState.NO_REFERENCE, "none")

    @staticmethod
    def found(reference: str, value: str | None) -> Credential:
        if value is None or not value.strip():
            return Credential(CredentialState.ABSENT, reference)
        return Credential(CredentialState.RESOLVED, reference, Secret(value.strip()))

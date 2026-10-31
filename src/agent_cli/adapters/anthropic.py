"""The first-party Anthropic (Claude API) provider adapter.

V1 supports exactly one request shape: a standard, global, uncached, non-streaming Messages API
call with a text system prompt and one user text message. Bedrock, Vertex, Foundry, proxies,
batch, prompt caching, fast mode, US-only inference, tools, images, and streaming are out of
scope and are never requested.

Every Anthropic SDK exception is translated into ``AdapterError`` here. The SDK's own automatic
retries are disabled (``max_retries=0``): retry, cooldown, and fallback are core policy (V1-13).
Normalized messages never contain prompts, system text, credentials, or response bodies; they may
name a status code, Anthropic's error type, and its request ID.

References (verify against them when changing this file):

- Python SDK: https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python
- Messages and Models API: https://platform.claude.com/docs/en/api/python
- Token counting: https://platform.claude.com/docs/en/build-with-claude/token-counting
- Errors: https://platform.claude.com/docs/en/api/errors
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, TypeGuard

from agent_cli.adapters.credentials import (
    Credential,
    CredentialResolver,
    CredentialState,
    Secret,
)
from agent_cli.adapters.domain import (
    AdapterError,
    ErrorKind,
    ModelDescriptor,
    ProviderIdentity,
    ProviderReadiness,
    ProviderRequest,
    ProviderResponse,
    ReadinessState,
    Usage,
)
from agent_cli.adapters.provider import ProviderAdapter
from agent_cli.config.model import ProviderConfig

PROVIDER_ID = "anthropic"
PROVIDER_TYPE = "anthropic"
IDENTITY = ProviderIdentity(PROVIDER_ID, "Anthropic", PROVIDER_TYPE, "api")

FIRST_PARTY_BASE_URL = "https://api.anthropic.com"
"""Passed explicitly so ``ANTHROPIC_BASE_URL`` or an SDK profile can never redirect requests."""

READINESS_TIMEOUT_S = 5.0
COUNT_TIMEOUT_S = 15.0
INVOKE_TIMEOUT_S = 300.0
CACHE_TTL_S = 60.0
"""How long readiness and the model list are reused in memory, so one screen or one planning
request does not repeat network checks. Nothing is persisted."""
FAILURE_TTL_S = 5.0
"""How long an ``UNAVAILABLE`` result is reused: long enough for one screen, short enough that a
network blip does not hide the provider for a minute."""

REQUEST_INFERENCE_GEO = "global"
REQUEST_SERVICE_TIER = "standard_only"
REQUEST_SPEED = "standard"
STANDARD_GEOS = frozenset({REQUEST_INFERENCE_GEO})
STANDARD_TIERS = frozenset({"standard"})
STANDARD_SPEEDS = frozenset({REQUEST_SPEED})

type AnthropicClient = Any
"""An ``anthropic.Anthropic`` client, or a test double with the same shape. Only
``models.list``, ``messages.create``, and ``messages.count_tokens`` are used."""
type ClientFactory = Callable[[Secret], AnthropicClient]
type Clock = Callable[[], float]


def sdk_client(secret: Secret) -> AnthropicClient:
    """The official SDK client: explicit key and base URL, no automatic retries.

    Imported on use so constructing the adapter stays cheap.
    """
    import anthropic

    return anthropic.Anthropic(
        api_key=secret.reveal(),
        base_url=FIRST_PARTY_BASE_URL,
        max_retries=0,
        timeout=INVOKE_TIMEOUT_S,
    )


@dataclass(slots=True)
class Cached[T]:
    value: T
    at: float


@dataclass(slots=True)
class AnthropicProviderAdapter(ProviderAdapter):
    """Reaches Claude models through the first-party Claude API.

    Built from the resolved ``providers.anthropic`` entry; it never reads configuration itself.
    """

    config: ProviderConfig
    credentials: CredentialResolver = field(default_factory=CredentialResolver, repr=False)
    client_factory: ClientFactory = field(default=sdk_client, repr=False)
    clock: Clock = field(default=time.monotonic, repr=False)
    _client: AnthropicClient | None = field(default=None, repr=False)
    _credential: Credential | None = field(default=None, repr=False)
    _readiness: Cached[ProviderReadiness] | None = None
    _models: Cached[tuple[ModelDescriptor, ...]] | None = None

    def identity(self) -> ProviderIdentity:
        return IDENTITY

    # -- Readiness -----------------------------------------------------------------------------

    def readiness(self) -> ProviderReadiness:
        cached = self._readiness
        if cached is not None:
            ttl = FAILURE_TTL_S if cached.value.state is ReadinessState.UNAVAILABLE else CACHE_TTL_S
            if self.clock() - cached.at < ttl:
                return cached.value
        try:
            readiness = self.check_readiness()
        except Exception as error:  # readiness never raises
            readiness = ProviderReadiness(
                ReadinessState.UNAVAILABLE, f"readiness check failed: {type(error).__name__}"
            )
        self._readiness = Cached(readiness, self.clock())
        return readiness

    def check_readiness(self) -> ProviderReadiness:
        problem = self.misconfiguration()
        if problem is not None:
            return ProviderReadiness(ReadinessState.MISCONFIGURED, problem)
        credential = self.credential()
        if credential.state is not CredentialState.RESOLVED:
            state = (
                ReadinessState.AUTH_REQUIRED
                if credential.state is CredentialState.ABSENT
                else ReadinessState.MISCONFIGURED
            )
            return ProviderReadiness(state, credential.problem())
        try:
            with translated("readiness check"):
                self.client().models.list(limit=1, timeout=READINESS_TIMEOUT_S)
        except AdapterError as error:
            if error.kind in (ErrorKind.AUTH_FAILED, ErrorKind.AUTH_REQUIRED):
                return ProviderReadiness(
                    ReadinessState.AUTH_REQUIRED,
                    f"Anthropic rejected the API key from {credential.reference}",
                )
            return ProviderReadiness(ReadinessState.UNAVAILABLE, error.message)
        return ProviderReadiness(
            ReadinessState.READY, f"Claude API; key from {credential.reference}"
        )

    def misconfiguration(self) -> str | None:
        if not self.config.enabled:
            return "disabled in config"
        if self.config.type != PROVIDER_TYPE:
            return f"type must be {PROVIDER_TYPE!r}, got {self.config.type!r}"
        if self.config.base_url is not None:
            return "a custom base_url is not supported; remove it to use the Claude API"
        return None

    # -- Client --------------------------------------------------------------------------------

    def credential(self) -> Credential:
        """The credential, resolved once per adapter: the keyring is asked at most once per
        session (and again after setup rebuilds the adapter), and readiness and requests always
        use the same key."""
        if self._credential is None:
            self._credential = self.credentials.resolve(self.config)
        return self._credential

    def client(self) -> AnthropicClient:
        """The SDK client, built once the credential resolves. Raises ``AdapterError``."""
        if self._client is not None:
            return self._client
        problem = self.misconfiguration()
        if problem is not None:
            raise AdapterError(ErrorKind.INVALID_REQUEST, f"anthropic is misconfigured: {problem}")
        credential = self.credential()
        if credential.secret is None:
            kind = (
                ErrorKind.AUTH_REQUIRED
                if credential.state is CredentialState.ABSENT
                else ErrorKind.INVALID_REQUEST
            )
            raise AdapterError(kind, f"anthropic: {credential.problem()}")
        try:
            self._client = self.client_factory(credential.secret)
        except Exception as error:
            raise AdapterError(
                ErrorKind.PROVIDER_ERROR,
                f"anthropic: cannot create the SDK client ({type(error).__name__})",
            ) from None
        return self._client

    # -- Models --------------------------------------------------------------------------------

    def list_models(self) -> Sequence[ModelDescriptor]:
        """Active models the Models API lists, with their exact IDs. Listing a model never adds
        it to the model registry."""
        cached = self._models
        if cached is not None and self.clock() - cached.at < CACHE_TTL_S:
            return cached.value
        with translated("listing models"):
            listed = list(self.client().models.list(limit=100, timeout=READINESS_TIMEOUT_S))
        models = tuple(
            ModelDescriptor(
                PROVIDER_ID,
                info.id,
                getattr(info, "display_name", None),
                context_limit(getattr(info, "max_input_tokens", None)),
            )
            for info in listed
            if getattr(info, "lifecycle", "active") in (None, "active")
        )
        self._models = Cached(models, self.clock())
        return models

    # -- Requests ------------------------------------------------------------------------------

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        if request.max_output_tokens is None or request.max_output_tokens < 1:
            raise AdapterError(
                ErrorKind.INVALID_REQUEST, "anthropic requests need a positive max_output_tokens"
            )
        with translated("message"):
            message = self.client().messages.create(
                **message_params(request),
                max_tokens=request.max_output_tokens,
                inference_geo=REQUEST_INFERENCE_GEO,
                service_tier=REQUEST_SERVICE_TIER,
                speed=REQUEST_SPEED,
                timeout=INVOKE_TIMEOUT_S,
            )
        return normalize(request, message)

    def count_input_tokens(self, request: ProviderRequest) -> int:
        """Anthropic's own input-token count for exactly this request. Raises ``AdapterError``."""
        with translated("token count"):
            counted = self.client().messages.count_tokens(
                **message_params(request), speed=REQUEST_SPEED, timeout=COUNT_TIMEOUT_S
            )
        tokens = getattr(counted, "input_tokens", None)
        if not is_count(tokens):
            raise AdapterError(ErrorKind.PROVIDER_ERROR, "anthropic returned no token count")
        return tokens


def message_params(request: ProviderRequest) -> dict[str, Any]:
    """Token-bearing fields shared by Messages and token counting: no tools or caching."""
    params: dict[str, Any] = {
        "model": request.model_id,
        "messages": [{"role": "user", "content": request.prompt}],
    }
    if request.system:
        params["system"] = request.system
    return params


def is_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def context_limit(value: object) -> int | None:
    return value if is_count(value) and value > 0 else None


def normalize(request: ProviderRequest, message: Any) -> ProviderResponse:
    model = getattr(message, "model", None)
    if model != request.model_id:
        raise AdapterError(
            ErrorKind.PROVIDER_ERROR,
            f"anthropic answered with model {model!r} instead of the requested "
            f"{request.model_id!r}",
        )
    texts = [
        block.text
        for block in getattr(message, "content", None) or ()
        if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str)
    ]
    text = "".join(texts)
    if not text.strip():
        stop = getattr(message, "stop_reason", None)
        raise AdapterError(
            ErrorKind.PROVIDER_ERROR, f"anthropic returned no text (stop reason: {stop})"
        )
    return ProviderResponse(model, text, usage(message))


def usage(message: Any) -> Usage:
    """Reported token usage. Usage outside the standard, global, uncached request shape cannot
    be priced by the standard price source, so its token counts are reported as unknown and the
    budget settles at the full reserved bound."""
    reference = request_id(message)
    reported = getattr(message, "usage", None)
    if reported is None:
        return Usage(reference=reference)
    input_tokens = getattr(reported, "input_tokens", None)
    output_tokens = getattr(reported, "output_tokens", None)
    standard = (
        not getattr(reported, "cache_creation_input_tokens", None)
        and not getattr(reported, "cache_read_input_tokens", None)
        and getattr(reported, "inference_geo", None) in STANDARD_GEOS
        and getattr(reported, "service_tier", None) in STANDARD_TIERS
        and getattr(reported, "speed", None) in STANDARD_SPEEDS
    )
    if not standard or not is_count(input_tokens) or not is_count(output_tokens):
        return Usage(reference=reference)
    return Usage(input_tokens, output_tokens, reference=reference)


def request_id(message: Any) -> str | None:
    value = getattr(message, "_request_id", None) or getattr(message, "id", None)
    return value if isinstance(value, str) and value else None


# -- Error translation -------------------------------------------------------------------------


@contextmanager
def translated(action: str) -> Iterator[None]:
    """Translate any exception raised inside into one normalized ``AdapterError``."""
    try:
        yield
    except AdapterError:
        raise
    except Exception as error:
        raise translate(error, action) from None


def translate(error: Exception, action: str) -> AdapterError:
    """Map an Anthropic SDK exception to a normalized ``AdapterError`` with a safe message."""
    import anthropic

    status = getattr(error, "status_code", None)
    detail = ", ".join(
        part
        for part in (
            f"HTTP {status}" if isinstance(status, int) else "",
            safe(getattr(error, "type", None)),
            f"request {safe(getattr(error, 'request_id', None))}"
            if safe(getattr(error, "request_id", None))
            else "",
        )
        if part
    )
    if isinstance(error, anthropic.APITimeoutError):
        kind, what = ErrorKind.TIMEOUT, "timed out"
    elif isinstance(error, anthropic.APIConnectionError):
        kind, what = ErrorKind.UNAVAILABLE, "could not connect"
    elif isinstance(error, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        kind, what = ErrorKind.AUTH_FAILED, "credential rejected"
    elif isinstance(error, anthropic.NotFoundError):
        kind, what = ErrorKind.NOT_FOUND, "model or resource not found"
    elif isinstance(error, anthropic.RateLimitError):
        kind, what = ErrorKind.RATE_LIMITED, "rate limited"
    elif isinstance(
        error,
        (
            anthropic.BadRequestError,
            anthropic.UnprocessableEntityError,
            anthropic.RequestTooLargeError,
        ),
    ):
        kind, what = ErrorKind.INVALID_REQUEST, "request rejected"
    elif isinstance(error, anthropic.APIStatusError):
        kind, what = ErrorKind.PROVIDER_ERROR, "provider error"
    else:
        kind, what = ErrorKind.PROVIDER_ERROR, f"unexpected failure ({type(error).__name__})"
    suffix = f" ({detail})" if detail else ""
    return AdapterError(kind, f"anthropic {action}: {what}{suffix}")


SAFE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def safe(value: object) -> str:
    """An identifier from an error, kept only when it is a short plain token."""
    if isinstance(value, str) and 0 < len(value) <= 80 and set(value) <= SAFE_CHARS:
        return value
    return ""

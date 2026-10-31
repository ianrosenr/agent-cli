"""An in-memory stand-in for the Anthropic SDK client, shaped like ``anthropic.Anthropic``.

Only ``models.list``, ``messages.create``, and ``messages.count_tokens`` exist, matching what the
adapter uses. Nothing here touches the network or a developer's real credentials.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2

from agent_cli.adapters.credentials import CredentialResolver, Secret

KEY = "sk-ant-test-0123456789"
SONNET = "claude-sonnet-5-5"
URL = "https://api.anthropic.com/v1/messages"


def resolver(**environ: str) -> CredentialResolver:
    """Credentials from a fake environment (default: a test key) and an empty keyring."""
    return CredentialResolver(environ or {"ANTHROPIC_API_KEY": KEY}, lambda service, name: None)


def model_info(id: str, lifecycle: str = "active", max_input_tokens: int | None = 1_000_000) -> Any:
    return SimpleNamespace(
        id=id,
        display_name=id.replace("-", " ").title(),
        lifecycle=lifecycle,
        max_input_tokens=max_input_tokens,
    )


def message(
    text: str | None = "hello",
    model: str = SONNET,
    input_tokens: int = 120,
    output_tokens: int = 30,
    **usage: Any,
) -> Any:
    content = [] if text is None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(
        id="msg_01ABC",
        model=model,
        content=content,
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            inference_geo="global",
            service_tier="standard",
            speed="standard",
            **usage,
        ),
    )


def status_error(cls: type[anthropic.APIStatusError], status: int, kind: str) -> Exception:
    """A real SDK status error whose body carries text that must never be shown."""
    response = httpx2.Response(
        status, request=httpx2.Request("POST", URL), headers={"request-id": "req_011XYZ"}
    )
    body = {"error": {"type": kind, "message": f"leaked detail {KEY} prompt text"}}
    return cls(f"Error code: {status} - {body}", response=response, body=body)


def timeout_error() -> Exception:
    return anthropic.APITimeoutError(request=httpx2.Request("POST", URL))


def connection_error() -> Exception:
    return anthropic.APIConnectionError(request=httpx2.Request("POST", URL))


type Reply = Any | Exception | Callable[[dict[str, Any]], Any]


def answer(reply: Reply, kwargs: dict[str, Any]) -> Any:
    if isinstance(reply, Exception):
        raise reply
    return reply(kwargs) if callable(reply) else reply


@dataclass
class FakeModels:
    data: list[Any] = field(default_factory=lambda: [model_info(SONNET)])
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def list(self, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return list(self.data)


@dataclass
class FakeMessages:
    reply: Reply = field(default_factory=message)
    count: int | Exception | Callable[[dict[str, Any]], int] = 100
    created: list[dict[str, Any]] = field(default_factory=list)
    counted: list[dict[str, Any]] = field(default_factory=list)

    def create(self, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        return answer(self.reply, kwargs)

    def count_tokens(self, **kwargs: Any) -> Any:
        self.counted.append(kwargs)
        return SimpleNamespace(input_tokens=answer(self.count, kwargs))


@dataclass
class FakeAnthropic:
    models: FakeModels = field(default_factory=FakeModels)
    messages: FakeMessages = field(default_factory=FakeMessages)
    secrets: list[Secret] = field(default_factory=list)

    def factory(self, secret: Secret) -> FakeAnthropic:
        """A ``ClientFactory`` that records the secret it was built with and returns this."""
        self.secrets.append(secret)
        return self

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Final, Literal

from mcp.server.request_state import (
    AESGCMRequestStateCodec,
    InvalidRequestState,
    RequestStateCodec,
    compact_json,
)
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INVALID_PARAMS,
    ClientCapabilities,
    ElicitRequest,
    ElicitRequestFormParams,
    InputRequest,
    InputRequiredResult,
    InputResponse,
    InputResponseRequestParams,
    Request,
)
from pydantic import TypeAdapter, ValidationError
from pydantic_ai import UserError
from pydantic_core import to_json

from fastapi_mcp_toolsets.protocol import dump_model

__all__ = [
    "APPROVAL_INPUT_KEY",
    "Elicitation",
    "InputRequired",
    "InputSession",
    "InputUnavailableError",
    "RequestStateSealer",
    "approval_input",
    "build_request_state_codec",
    "input_required_result",
    "read_inputs",
    "supports_form_elicitation",
    "supports_url_elicitation",
]

APPROVAL_INPUT_KEY: Final[str] = "approval"

# How far ahead of this clock a state may claim to be sealed, for clock skew between workers.
REQUEST_STATE_FUTURE_SKEW: Final[timedelta] = timedelta(minutes=1)

INPUT_RESPONSE: Final[TypeAdapter[InputResponse]] = TypeAdapter(InputResponse)

# What may change between a request and its retry.
RETRY_FIELDS: Final[frozenset[str]] = frozenset({"meta", "input_responses", "request_state"})


def request_digest(request: Request[Any, Any]) -> str:
    params = None if request.params is None else request.params.model_dump(mode="json", exclude=RETRY_FIELDS)
    # Sorted, so the digest does not depend on the order of the keys.
    canonical = compact_json({"method": request.method, "params": params}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, kw_only=True)
class RequestState:
    # Of the request a retry must repeat.
    digest: str
    # Every answer so far, dumped; the client only resends the last round's.
    responses: dict[str, Any] = field(default_factory=dict)
    # The keys asked for in the last round, the only answers taken from the retry.
    pending: tuple[str, ...] = ()
    # From `ApprovalRequired`, becomes `ctx.tool_call_metadata`.
    metadata: Any = None
    issued_at: float = 0.0


REQUEST_STATE: Final[TypeAdapter[RequestState]] = TypeAdapter(RequestState)


def build_request_state_codec(
    keys: Sequence[str | bytes] | None,
    codec: RequestStateCodec | None,
) -> RequestStateCodec:
    match keys, codec:
        case None, None:
            # Only works with a single worker.
            return AESGCMRequestStateCodec([secrets.token_bytes(32)])
        case keys, None:
            # `keys[0]` seals, any of them unseals.
            return AESGCMRequestStateCodec(keys)
        case None, codec:
            return codec
        case _:
            raise UserError("Pass request_state_keys or request_state_codec, not both")


@dataclass(frozen=True, kw_only=True)
class RequestStateSealer:
    codec: RequestStateCodec = field(repr=False)
    max_age: timedelta

    def seal(self, state: RequestState) -> str:
        return self.codec.seal(to_json(replace(state, issued_at=time.time())))

    def unseal(self, token: str) -> RequestState:
        try:
            state = REQUEST_STATE.validate_json(self.codec.unseal(token))
        except (InvalidRequestState, ValidationError):
            raise MCPError(INVALID_PARAMS, "Invalid request state") from None

        age = timedelta(seconds=time.time() - state.issued_at)

        if age < -REQUEST_STATE_FUTURE_SKEW:
            raise MCPError(INVALID_PARAMS, "Invalid request state")

        if age > self.max_age:
            raise MCPError(INVALID_PARAMS, "Request state expired")

        return state


class InputRequired(Exception):  # noqa: N818
    def __init__(self, key: str, request: InputRequest, *, metadata: Any = None) -> None:
        super().__init__(f"Input {key!r} is required")
        self.key = key
        self.request = request
        self.metadata = metadata


class InputUnavailableError(RuntimeError):
    """The protocol version or the client cannot answer this kind of input request."""


@dataclass(kw_only=True)
class InputSession:
    responses: dict[str, InputResponse] = field(default_factory=dict)
    metadata: Any = None
    # Numbers the requests made without a key, so a re-run asks them in the same order.
    counter: int = 0

    def take(self, request: InputRequest, *, kind: str, key: str | None) -> InputResponse:
        if key is None:
            key = f"{kind}-{self.counter}"

        self.counter += 1

        if (response := self.responses.get(key)) is not None:
            return response

        raise InputRequired(key, request)


@dataclass(frozen=True, kw_only=True)
class Elicitation[T]:
    action: Literal["accept", "decline", "cancel"]
    data: T | None = None

    @property
    def accepted(self) -> bool:
        return self.action == "accept"


def supports_form_elicitation(capabilities: ClientCapabilities) -> bool:
    match capabilities.elicitation:
        case None:
            return False
        case elicitation:
            # An empty capability means form mode.
            return elicitation.form is not None or elicitation.url is None


def supports_url_elicitation(capabilities: ClientCapabilities) -> bool:
    return capabilities.elicitation is not None and capabilities.elicitation.url is not None


def approval_input(tool_name: str, arguments: Mapping[str, Any]) -> ElicitRequest:
    args = to_json(arguments, indent=2).decode()

    return ElicitRequest(
        params=ElicitRequestFormParams(
            message=f"Allow {tool_name} to run with these arguments?\n{args}",
            requested_schema={"type": "object", "properties": {}},
        ),
    )


def read_inputs(request: Request[Any, Any], *, sealer: RequestStateSealer) -> InputSession:
    match request.params:
        case InputResponseRequestParams(request_state=str() as token) as params:
            pass
        case _:
            return InputSession()

    state = sealer.unseal(token)
    if state.digest != request_digest(request):
        raise MCPError(INVALID_PARAMS, "Request state does not match this request")

    responses = {key: INPUT_RESPONSE.validate_python(value) for key, value in state.responses.items()}

    for key in state.pending:
        if (response := (params.input_responses or {}).get(key)) is not None:
            responses[key] = response

    return InputSession(responses=responses, metadata=state.metadata)


def input_required_result(
    error: InputRequired,
    request: Request[Any, Any],
    session: InputSession,
    *,
    sealer: RequestStateSealer,
) -> InputRequiredResult:
    state = RequestState(
        digest=request_digest(request),
        responses={key: dump_model(response) for key, response in session.responses.items()},
        pending=(error.key,),
        metadata=session.metadata if error.metadata is None else error.metadata,
    )

    return InputRequiredResult(
        input_requests={error.key: error.request},
        request_state=sealer.seal(state),
    )

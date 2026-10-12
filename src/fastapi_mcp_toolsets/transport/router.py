from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal, Protocol, cast, runtime_checkable

from fastapi import APIRouter, Depends, FastAPI, Header, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi_injected import DependencyResolutionError, init_inject_scope
from mcp.server.streamable_http import check_accept_headers
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from mcp.shared.inbound import (
    MCP_PROTOCOL_VERSION_HEADER,
    InboundLadderRejection,
    classify_inbound_request,
    find_duplicated_routing_header,
    unsupported_protocol_version_rejection,
)
from mcp_types import (
    HEADER_MISMATCH,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    PARSE_ERROR,
    ClientCapabilities,
    Implementation,
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
)
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError
from pydantic_ai.exceptions import ConcurrencyLimitExceeded
from starlette.types import Lifespan

from fastapi_mcp_toolsets.protocol import MCPProtocolVersion, jsonrpc_error
from fastapi_mcp_toolsets.server import MCPMessageStream, MCPServer

from .responses import DeferredResponse, error_response, rejection_response

__all__ = [
    "JSONRPCMessage",
    "MCPRoute",
    "MCPRouter",
]

logger = logging.getLogger(__name__)

INVALID_BODY_MESSAGE: Final = "Body must be a single JSON-RPC request or notification object"

OPENAPI_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    status.HTTP_200_OK: {
        "description": "The JSON-RPC response, as JSON or as an SSE stream ending with it",
        "content": {
            "application/json": {},
            "text/event-stream": {},
        },
    },
    status.HTTP_202_ACCEPTED: {
        "description": "A notification was accepted",
    },
    status.HTTP_406_NOT_ACCEPTABLE: {
        "description": "The Accept header allows no supported response type",
    },
}


class JSONRPCMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    jsonrpc: Literal["2.0"]
    # Absent for a notification; `null` is not a valid request id.
    id: StrictInt | StrictStr | None = Field(default=None)
    method: str
    params: dict[str, Any] | None = None

    @property
    def is_request(self) -> bool:
        return "id" in self.model_fields_set

    def body(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


def invalid_body_response(errors: Sequence[Any]) -> Response:
    # Malformed JSON is a parse error; anything else is a request the model rejected.
    if any(error.get("type") == "json_invalid" for error in errors):
        return error_response(PARSE_ERROR, "Parse error")

    return error_response(INVALID_REQUEST, INVALID_BODY_MESSAGE, data=jsonable_encoder(errors))


class MCPRoute(APIRoute):
    """Answers what FastAPI would reject with a 422 as a JSON-RPC error instead."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as e:
                return invalid_body_response(e.errors())

        return handle


def parse_protocol_version(version: str | None) -> MCPProtocolVersion | None:
    if version is None:
        return None

    try:
        return MCPProtocolVersion(version)
    except ValueError:
        return None


@dataclass(frozen=True, kw_only=True)
class RequestEnvelope:
    protocol_version: MCPProtocolVersion
    client_capabilities: ClientCapabilities | None = None
    client_info: Implementation | None = None


@runtime_checkable
class MCPExtension(Protocol):
    """Something served next to the endpoint as an `APIRouter`: an authorization server, a protected resource.

    An extension may also declare a `lifespan`, an async context manager taking the app, which runs within the
    router's own; an `APIRouter` extension's `lifespan_context` is merged by `include_router` itself.
    """

    @property
    def router(self) -> APIRouter: ...


def lifespan_of(obj: Any) -> Lifespan[Any] | None:
    """The `lifespan` an object declares, if any."""
    return getattr(obj, "lifespan", None)


class MCPRouter(APIRouter):
    def __init__(
        self,
        server: MCPServer[Any],
        *,
        path: str = "/mcp",
        json_response: bool = False,
        sse_ping_interval: float = 15,
        security: TransportSecuritySettings | None = None,
        lifespan: Lifespan[Any] | None = None,
        dependencies: Sequence[Any] | None = None,
        extensions: Sequence[APIRouter | MCPExtension] = (),
        **kwargs: Any,
    ) -> None:
        self.server = server
        self.json_response = json_response
        self.sse_ping_interval = sse_ping_interval
        self.security = TransportSecurityMiddleware(security)

        self.extensions = [*extensions]
        self.dependencies_ = [*(dependencies or ())]

        kwargs.setdefault("route_class", MCPRoute)

        super().__init__(
            lifespan=self._merge_lifespan(lifespan),
            **kwargs,
        )

        # `dependencies` guard the MCP endpoint alone, not the extensions served alongside it; the inject scope
        # comes first so they already resolve inside it.
        self.post(
            path,
            name="mcp",
            summary="MCP endpoint",
            description=f"Serves the `{server.name}` MCP server over Streamable HTTP.",
            responses=OPENAPI_RESPONSES,
            dependencies=[Depends(init_inject_scope), *self.dependencies_],
        )(self._handle_post)

        for extension in self.extensions:
            self.extend(extension)

    def extend(self, extension: APIRouter | MCPExtension) -> None:
        """Serve an extension's routes next to the endpoint, outside this router's own prefix.

        Discovery documents live at `/.well-known/...` whatever path the endpoint has, so a prefix given to
        this router does not apply to them; one given at `app.include_router(...)` still does.
        """
        router = extension if isinstance(extension, APIRouter) else extension.router
        prefix, self.prefix = self.prefix, ""

        try:
            self.include_router(router)
        finally:
            self.prefix = prefix

    def extension_lifespans(self) -> list[Lifespan[Any]]:
        # An `APIRouter` extension's lifespan is already merged by `include_router`; a dependency's is the one
        # its callable declares, e.g. a bearer whose verifier discovers its keys at startup.
        extensions = (extension for extension in self.extensions if not isinstance(extension, APIRouter))
        callables = (getattr(dependency, "dependency", dependency) for dependency in self.dependencies_)

        return [lifespan for obj in (*extensions, *callables) if (lifespan := lifespan_of(obj)) is not None]

    @asynccontextmanager
    async def server_lifespan(self, app: FastAPI) -> AsyncGenerator[None]:
        # The server first, then the extensions, so the given lifespan can already use them all.
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(self.server.lifespan(app))

            for lifespan in self.extension_lifespans():
                await stack.enter_async_context(lifespan(app))

            yield

    def _merge_lifespan(self, lifespan: Lifespan[Any] | None) -> Lifespan[Any]:
        if lifespan is None:
            return self.server_lifespan

        @asynccontextmanager
        async def merged(app: Any) -> AsyncGenerator[Any]:
            async with self.server_lifespan(app), lifespan(app) as state:
                yield state

        return cast("Lifespan[Any]", merged)

    async def _handle_post(
        self,
        request: Request,
        message: JSONRPCMessage,
        requested: Annotated[str | None, Header(alias=MCP_PROTOCOL_VERSION_HEADER)] = None,
    ) -> Response:
        if (rejected := await self.check_request(request)) is not None:
            return rejected

        if not message.is_request:
            return self.acknowledge_notification(message, requested)

        body = message.body()

        try:
            jsonrpc_request = JSONRPCRequest.model_validate(body)
        except ValidationError:
            return error_response(INVALID_REQUEST, INVALID_BODY_MESSAGE)

        protocol_version = parse_protocol_version(requested)

        if protocol_version is not None and not protocol_version.features.handshake:
            envelope = self.modern_envelope(body, jsonrpc_request, request)
        else:
            envelope = self.handshake_envelope(jsonrpc_request, requested, protocol_version)

        match envelope:
            case Response() as rejected:
                return rejected
            case RequestEnvelope():
                return await self.respond(body, jsonrpc_request, envelope, request)

    async def check_request(self, request: Request) -> Response | None:
        if (rejected := await self.security.validate_request(request, is_post=True)) is not None:
            return rejected

        has_json, has_sse = check_accept_headers(request)

        if not has_json or (not self.json_response and not has_sse):
            return Response(status_code=status.HTTP_406_NOT_ACCEPTABLE)

        return None

    async def respond(
        self,
        body: dict[str, Any],
        message: JSONRPCRequest,
        envelope: RequestEnvelope,
        request: Request,
    ) -> Response:
        async def serve(stream: MCPMessageStream | None) -> JSONRPCResponse | JSONRPCError:
            try:
                async with self.server.build_context(
                    protocol_version=envelope.protocol_version,
                    client_capabilities=envelope.client_capabilities,
                    client_info=envelope.client_info,
                    headers=request.headers,
                    stream=stream,
                ) as ctx:
                    response = await self.server.dispatch(body, ctx)
            except ConcurrencyLimitExceeded as e:
                return jsonrpc_error(INTERNAL_ERROR, str(e), request_id=message.id)
            except DependencyResolutionError as e:
                # What FastAPI would answer with a 422, e.g. a header the deps need is missing.
                return jsonrpc_error(
                    INVALID_REQUEST,
                    "Invalid request",
                    request_id=message.id,
                    data=jsonable_encoder(e.errors),
                )
            except Exception:
                logger.exception("MCP request handler raised")
                response = None

            return response or jsonrpc_error(INTERNAL_ERROR, "Internal server error", request_id=message.id)

        return DeferredResponse(
            serve,
            protocol_version=envelope.protocol_version,
            ping_interval=self.sse_ping_interval,
            json_response=self.json_response,
        )

    def modern_envelope(
        self,
        body: dict[str, Any],
        message: JSONRPCRequest,
        request: Request,
    ) -> RequestEnvelope | Response:
        if (duplicated := find_duplicated_routing_header(request.headers.items())) is not None:
            return error_response(HEADER_MISMATCH, f"{duplicated} header appears more than once", request_id=message.id)

        route = classify_inbound_request(
            body,
            headers=dict(request.headers),
            supported_modern_versions=[protocol.version for protocol in MCPProtocolVersion.modern()],
        )
        if isinstance(route, InboundLadderRejection):
            return rejection_response(route, request_id=message.id)

        try:
            client_capabilities = ClientCapabilities.model_validate(route.client_capabilities)
            client_info = None if route.client_info is None else Implementation.model_validate(route.client_info)
        except ValidationError as e:
            return error_response(INVALID_PARAMS, f"Invalid request envelope: {e}", request_id=message.id)

        return RequestEnvelope(
            protocol_version=MCPProtocolVersion(route.protocol_version),
            client_capabilities=client_capabilities,
            client_info=client_info,
        )

    def handshake_envelope(
        self,
        message: JSONRPCRequest,
        requested: str | None,
        protocol_version: MCPProtocolVersion | None,
    ) -> RequestEnvelope | Response:
        if message.method == "initialize":
            # Settled by the version in its params.
            return RequestEnvelope(protocol_version=MCPProtocolVersion.negotiate_handshake())

        if requested is None:
            return RequestEnvelope(protocol_version=MCPProtocolVersion.assumed())

        if protocol_version is None or not protocol_version.features.handshake:
            supported = ", ".join(protocol.version for protocol in MCPProtocolVersion)
            return error_response(
                INVALID_REQUEST,
                f"Unsupported protocol version: {requested} (supported: {supported})",
                request_id=message.id,
            )

        return RequestEnvelope(protocol_version=protocol_version)

    def acknowledge_notification(self, message: JSONRPCMessage, requested: str | None) -> Response:
        # Not acted on: a `notifications/cancelled` by client-chosen id could cancel another caller's request.
        try:
            JSONRPCNotification.model_validate(message.body())
        except ValidationError:
            return error_response(INVALID_REQUEST, INVALID_BODY_MESSAGE)

        if requested is not None and (
            rejection := unsupported_protocol_version_rejection(
                requested, [protocol.version for protocol in MCPProtocolVersion]
            )
        ):
            return rejection_response(rejection, request_id=None)

        return Response(status_code=status.HTTP_202_ACCEPTED)

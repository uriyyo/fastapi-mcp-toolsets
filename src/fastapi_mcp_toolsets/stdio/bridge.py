from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import anyio
import mcp_types
from anyio import AsyncFile
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from fastapi import status
from httpx2 import AsyncClient, EventSource, HTTPError
from mcp.server.stdio import stdio_server
from mcp.shared.inbound import (
    MCP_METHOD_HEADER,
    MCP_NAME_HEADER,
    MCP_PROTOCOL_VERSION_HEADER,
    NAME_BEARING_METHODS,
    encode_header_value,
    mcp_param_headers,
    x_mcp_header_map,
)
from mcp.shared.message import SessionMessage
from mcp_types import (
    INTERNAL_ERROR,
    PARSE_ERROR,
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    RequestId,
    jsonrpc_message_adapter,
)
from pydantic import ValidationError
from starlette.types import ASGIApp

from fastapi_mcp_toolsets.protocol import MCPProtocolVersion, jsonrpc_error
from fastapi_mcp_toolsets.tasks.wire import TASK_NAME_PARAMS
from fastapi_mcp_toolsets.transport.router import parse_protocol_version

from .asgi import ASGIStreamTransport, lifespan

__all__ = [
    "MCPStdio",
]

logger = logging.getLogger(__name__)

type Forward = Callable[[str | bytes], Awaitable[bool]]
type Reader = MemoryObjectReceiveStream[SessionMessage | Exception]
type Writer = MemoryObjectSendStream[SessionMessage]

ACCEPT: Final = "application/json, text/event-stream"
CANCELLED_METHOD: Final = "notifications/cancelled"
IN_PROCESS_BASE_URL: Final = "http://localhost"


class EndpointError(Exception):
    """The endpoint failed, or answered with something other than JSON-RPC, e.g. a 401 page from a middleware."""


def dump(message: JSONRPCMessage) -> dict[str, Any]:
    return message.model_dump(by_alias=True, exclude_unset=True, mode="json")


def parse(data: str | bytes) -> JSONRPCMessage:
    return jsonrpc_message_adapter.validate_json(data, by_name=False)


def media_type(content_type: str) -> str:
    media, _, _ = content_type.partition(";")
    return media.strip().lower()


@dataclass(kw_only=True)
class HTTPSession:
    client: AsyncClient
    url: str
    headers: Mapping[str, str]
    writer: Writer

    protocol_version: MCPProtocolVersion | None = None
    param_headers: dict[str, dict[tuple[str, ...], str]] = field(default_factory=dict)
    in_flight: dict[RequestId, anyio.CancelScope] = field(default_factory=dict)

    async def write(self, message: JSONRPCMessage) -> None:
        await self.writer.send(SessionMessage(message))

    def cancel(self, request_id: RequestId) -> None:
        # Closing the request's stream is how a stateless client cancels; nothing is posted.
        match self.in_flight.get(request_id):
            case anyio.CancelScope() as scope:
                scope.cancel()

    def request_headers(self, message: JSONRPCRequest | JSONRPCNotification) -> dict[str, str]:
        # The headers the SDK's own HTTP client stamps on a request, derived from the body alone.
        headers = {**self.headers, "accept": ACCEPT}
        params = message.params or {}

        match params.get("_meta"):
            case {mcp_types.PROTOCOL_VERSION_META_KEY: str() as version}:
                # Mirrored as sent, so an unsupported version is rejected as such rather than as a mismatch.
                headers[MCP_PROTOCOL_VERSION_HEADER] = version
                headers[MCP_METHOD_HEADER] = message.method
                headers.update(self.name_headers(message.method, params))
            case _ if self.protocol_version is not None and message.method != "initialize":
                headers[MCP_PROTOCOL_VERSION_HEADER] = self.protocol_version.version

        return headers

    def name_headers(self, method: str, params: Mapping[str, Any]) -> dict[str, str]:
        headers: dict[str, str] = {}

        match {**NAME_BEARING_METHODS, **TASK_NAME_PARAMS}.get(method):
            case str() as key if isinstance(name := params.get(key), str):
                headers[MCP_NAME_HEADER] = encode_header_value(name)

        match method, params.get("name"):
            case "tools/call", str() as name if (header_map := self.param_headers.get(name)) is not None:
                headers.update(mcp_param_headers(header_map, params.get("arguments") or {}))

        return headers

    def absorb(self, request: JSONRPCRequest, response: JSONRPCResponse) -> None:
        match request.method, response.result:
            case "initialize", {"protocolVersion": str() as version}:
                self.protocol_version = parse_protocol_version(version)
            case "tools/list", {"tools": list() as tools}:
                for tool in tools:
                    match tool:
                        case {"name": str() as name}:
                            self.param_headers[name] = x_mcp_header_map(tool.get("inputSchema"))

    async def post(self, message: JSONRPCRequest | JSONRPCNotification) -> None:
        match message:
            case JSONRPCRequest(id=request_id):
                pass
            case _:
                request_id = None

        answered = False

        async def forward(data: str | bytes) -> bool:
            nonlocal answered

            try:
                incoming = parse(data)
            except ValidationError:
                logger.warning("Dropping a message the endpoint sent that is not JSON-RPC: %r", data)
                return False

            match message, incoming:
                case JSONRPCRequest(id=id), JSONRPCResponse(id=incoming_id) if id == incoming_id:
                    answered = True
                    self.absorb(message, incoming)
                case JSONRPCRequest(id=id), JSONRPCError(id=incoming_id) if id == incoming_id:
                    answered = True

            await self.write(incoming)
            return True

        with anyio.CancelScope() as scope:
            if request_id is not None:
                self.in_flight[request_id] = scope

            try:
                await self.relay(message, forward)
            except EndpointError as e:
                answered = True
                await self.fail(request_id, str(e))
            finally:
                if request_id is not None:
                    self.in_flight.pop(request_id, None)

        if request_id is not None and not answered and not scope.cancel_called:
            await self.fail(request_id, "MCP endpoint closed the stream without answering")

    async def relay(self, message: JSONRPCRequest | JSONRPCNotification, forward: Forward) -> None:
        try:
            await self.exchange(message, forward)
        except HTTPError as e:
            raise EndpointError(f"MCP endpoint failed: {e}") from e

    async def exchange(self, message: JSONRPCRequest | JSONRPCNotification, forward: Forward) -> None:
        async with self.client.sse(
            self.url,
            method="POST",
            json=dump(message),
            headers=self.request_headers(message),
            max_event_size=None,
        ) as source:
            response = source.response

            match response.status_code, media_type(response.headers.get("content-type", "")):
                case status.HTTP_202_ACCEPTED, _:
                    return
                case _, "text/event-stream":
                    await self.relay_events(source, forward)
                case status_code, _:
                    body = await response.aread()

                    if not await forward(body):
                        raise EndpointError(
                            f"MCP endpoint answered HTTP {status_code}: {body.decode(errors='replace')[:200]}"
                        )

    async def relay_events(self, source: EventSource, forward: Forward) -> None:
        async for event in source:
            match event.event:
                case "message":
                    await forward(event.data)

    async def fail(self, request_id: RequestId | None, reason: str) -> None:
        # A notification has nothing to answer; the failure is only logged.
        match request_id:
            case None:
                logger.warning(reason)
            case _:
                await self.write(jsonrpc_error(INTERNAL_ERROR, reason, request_id=request_id))

    async def serve(self, reader: Reader) -> None:
        async with anyio.create_task_group() as tg:
            async for item in reader:
                match item:
                    case Exception():
                        await self.write(jsonrpc_error(PARSE_ERROR, "Parse error"))
                    case SessionMessage(
                        message=JSONRPCNotification(method=method, params={"requestId": request_id})
                    ) if method == CANCELLED_METHOD:
                        self.cancel(request_id)
                    case SessionMessage(message=JSONRPCRequest() | JSONRPCNotification() as message):
                        tg.start_soon(self.post, message)
                    case SessionMessage():
                        # The endpoint never asks the client anything, so there is nothing to answer.
                        logger.warning("Dropping a response the client sent; the endpoint makes no requests")


class MCPStdio:
    """Serves an app's MCP endpoint over the process's stdin and stdout.

    Each JSON-RPC message read from stdin is posted to the Streamable HTTP endpoint and whatever the endpoint
    answers, as JSON or as an SSE stream, is written back one message per line. The app is served in-process,
    with its lifespan and every middleware and dependency it mounts.
    """

    def __init__(
        self,
        app: ASGIApp,
        /,
        *,
        path: str = "/mcp",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.app = app
        self.path = path
        self.headers = dict(headers or {})

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[AsyncClient]:
        """A client talking to the app in-process, within its lifespan."""
        state: dict[str, Any] = {}
        transport = ASGIStreamTransport(self.app, state=state)

        async with (
            lifespan(self.app, state),
            AsyncClient(transport=transport, base_url=IN_PROCESS_BASE_URL, timeout=None) as client,
        ):
            yield client

    async def serve(self, stdin: AsyncFile[str] | None = None, stdout: AsyncFile[str] | None = None) -> None:
        # Closing the writer lets the stdout task finish, so leaving the loop ends the process.
        async with (
            self.connect() as client,
            stdio_server(stdin, stdout) as (reader, writer),
            reader,
            writer,
        ):
            await HTTPSession(client=client, url=self.path, headers=self.headers, writer=writer).serve(reader)

    def run(self) -> None:
        anyio.run(self.serve)

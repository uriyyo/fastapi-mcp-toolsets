from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import anyio
from fastapi import Response
from fastapi.responses import JSONResponse
from fastapi.sse import KEEPALIVE_COMMENT, EventSourceResponse, format_sse_event
from mcp.shared.inbound import InboundLadderRejection
from mcp_types import JSONRPCError, JSONRPCNotification, JSONRPCResponse, RequestId
from pydantic_core import to_json
from starlette.types import Receive, Scope, Send

from fastapi_mcp_toolsets.protocol import MCPProtocolVersion, dump, http_status, jsonrpc_error
from fastapi_mcp_toolsets.server import MCPMessageStream

__all__ = [
    "DeferredResponse",
    "Serve",
    "error_response",
    "message_response",
    "rejection_response",
]

type OutgoingMessage = JSONRPCResponse | JSONRPCError | JSONRPCNotification
type Serve = Callable[[MCPMessageStream | None], Awaitable[JSONRPCResponse | JSONRPCError]]

SSE_HEADERS: Final[dict[str, str]] = {
    "cache-control": "no-cache, no-transform",
    "connection": "keep-alive",
    "x-accel-buffering": "no",
}


def message_response(
    message: JSONRPCResponse | JSONRPCError,
    *,
    protocol_version: MCPProtocolVersion,
) -> Response:
    return JSONResponse(
        status_code=http_status(
            message,
            protocol_version=protocol_version,
        ),
        content=dump(message),
    )


def error_response(code: int, message: str, *, request_id: RequestId | None = None, data: Any = None) -> Response:
    return message_response(
        jsonrpc_error(
            code,
            message,
            request_id=request_id,
            data=data,
        ),
        protocol_version=MCPProtocolVersion.latest(),
    )


def rejection_response(rejection: InboundLadderRejection, *, request_id: RequestId | None) -> Response:
    return error_response(rejection.code, rejection.message, request_id=request_id, data=rejection.data)


def sse_event(message: OutgoingMessage) -> bytes:
    return format_sse_event(event="message", data_str=to_json(dump(message)).decode())


@dataclass(frozen=True, kw_only=True)
class Done:
    response: JSONRPCResponse | JSONRPCError


# Runs the handler until the client disconnects, which is how a stateless client cancels.
class DeferredResponse(Response):
    def __init__(
        self,
        serve: Serve,
        *,
        protocol_version: MCPProtocolVersion,
        ping_interval: float,
        json_response: bool,
    ) -> None:
        super().__init__()
        self.serve = serve
        self.protocol_version = protocol_version
        self.ping_interval = ping_interval
        self.json_response = json_response

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        writer, reader = anyio.create_memory_object_stream[OutgoingMessage | Done](0)

        async def run() -> None:
            async with writer:
                await writer.send(Done(response=await self.serve(None if self.json_response else writer)))

        async def watch_disconnect(cancel_scope: anyio.CancelScope) -> None:
            while (await receive())["type"] != "http.disconnect":
                pass

            cancel_scope.cancel()

        async def next_message() -> OutgoingMessage | Done | None:
            # `None` means nothing arrived within the ping interval.
            with anyio.move_on_after(None if self.json_response else self.ping_interval):
                return await reader.receive()

            return None

        async def events(message: OutgoingMessage | Done | None) -> AsyncIterator[bytes]:
            while not isinstance(message, Done):
                if message is None:
                    yield KEEPALIVE_COMMENT
                else:
                    yield sse_event(message)

                message = await next_message()

            yield sse_event(message.response)

        async with reader, anyio.create_task_group() as tg:
            tg.start_soon(run)
            tg.start_soon(watch_disconnect, tg.cancel_scope)

            # Answer with plain JSON unless something has to be sent before the response.
            match message := await next_message():
                case Done(response=response):
                    await message_response(response, protocol_version=self.protocol_version)(scope, receive, send)
                case _:
                    await EventSourceResponse(events(message), headers=SSE_HEADERS)(scope, receive, send)

            tg.cancel_scope.cancel()

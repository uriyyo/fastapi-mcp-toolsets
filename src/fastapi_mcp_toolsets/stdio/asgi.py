from __future__ import annotations

import logging
import math
from collections.abc import AsyncGenerator, AsyncIterator, MutableMapping
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream
from fastapi import status
from httpx2 import AsyncBaseTransport, AsyncByteStream, Request, Response
from starlette.types import ASGIApp, Message

__all__ = [
    "ASGIStreamTransport",
    "lifespan",
]

logger = logging.getLogger(__name__)

ASGI_VERSION: Final = {
    "version": "3.0",
    "spec_version": "2.3",
}


@asynccontextmanager
async def lifespan(app: ASGIApp, state: MutableMapping[str, Any]) -> AsyncGenerator[None]:
    to_app_writer, to_app = anyio.create_memory_object_stream[Message](math.inf)
    from_app_writer, from_app = anyio.create_memory_object_stream[Message](math.inf)

    async def run() -> None:
        async with from_app_writer:
            await app(
                {"type": "lifespan", "asgi": ASGI_VERSION, "state": state},
                to_app.receive,
                from_app_writer.send,
            )

    async def expect(completed: str) -> None:
        match await from_app.receive():
            case {"type": kind} if kind == completed:
                pass
            case {"message": str() as reason} | {"type": str() as reason}:
                raise RuntimeError(reason)

    async with to_app_writer, from_app, anyio.create_task_group() as tg:
        tg.start_soon(run)

        await to_app_writer.send({"type": "lifespan.startup"})
        await expect("lifespan.startup.complete")

        try:
            yield
        finally:
            # Shut down even when leaving on a cancellation, e.g. Ctrl-C.
            with anyio.CancelScope(shield=True):
                await to_app_writer.send({"type": "lifespan.shutdown"})
                await expect("lifespan.shutdown.complete")


@dataclass
class ASGIResponseStream(AsyncByteStream):
    chunks: MemoryObjectReceiveStream[bytes]
    disconnected: anyio.Event

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        # Not reading any further is how the client disconnects, which cancels the app's handler.
        self.disconnected.set()
        self.chunks.close()


@dataclass
class Exchange:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.body_sent = False
        self.started = anyio.Event()
        self.disconnected = anyio.Event()
        self.chunks_writer, self.chunks = anyio.create_memory_object_stream[bytes](math.inf)
        self.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        self.headers: list[tuple[bytes, bytes]] = []

    async def receive(self) -> Message:
        if not self.body_sent:
            self.body_sent = True
            return {"type": "http.request", "body": self.body, "more_body": False}

        await self.disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        match message["type"]:
            case "http.response.start":
                self.status_code = message["status"]
                self.headers = message.get("headers", [])
                self.started.set()
            case "http.response.body":
                # A closed reader is a client that stopped listening; the app sees it as a disconnect.
                with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
                    if chunk := message.get("body", b""):
                        await self.chunks_writer.send(chunk)

                if not message.get("more_body", False):
                    self.chunks_writer.close()

    async def run(self, app: ASGIApp, scope: dict[str, Any]) -> None:
        try:
            with self.chunks_writer:
                await app(scope, self.receive, self.send)
        except Exception:
            logger.exception("ASGI app raised while serving %s %s", scope["method"], scope["path"])
        finally:
            self.started.set()

    def response(self, request: Request) -> Response:
        return Response(
            self.status_code,
            headers=self.headers,
            stream=ASGIResponseStream(self.chunks, self.disconnected),
            request=request,
        )


class ASGIStreamTransport(AsyncBaseTransport):
    def __init__(
        self,
        app: ASGIApp,
        *,
        state: MutableMapping[str, Any] | None = None,
        root_path: str = "",
        client: tuple[str, int] = ("127.0.0.1", 123),
    ) -> None:
        self.app = app
        self.state = {} if state is None else state
        self.root_path = root_path
        self.client = client
        self._task_group: TaskGroup | None = None
        self._astack: AsyncExitStack | None = None

    async def __aenter__(self) -> Self:
        self._task_group = await anyio.create_task_group().__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        tg, self._task_group = self._task_group, None

        if tg is not None:
            tg.cancel_scope.cancel()
            await tg.__aexit__(exc_type, exc_value, traceback)

    def scope(self, request: Request) -> dict[str, Any]:
        return {
            "type": "http",
            "asgi": ASGI_VERSION,
            "http_version": "1.1",
            "method": request.method,
            "headers": [(name.lower(), value) for name, value in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port),
            "client": self.client,
            "root_path": self.root_path,
            # A shallow copy per request, as servers pass the lifespan state along.
            "state": dict(self.state),
        }

    async def handle_async_request(self, request: Request) -> Response:
        match self._task_group, request.stream:
            case None, _:
                raise RuntimeError("ASGIStreamTransport must be entered before sending requests")
            case tg, AsyncByteStream() as stream:
                exchange = Exchange(b"".join([chunk async for chunk in stream]))
            case _:
                raise TypeError("ASGIStreamTransport only sends async requests")

        tg.start_soon(exchange.run, self.app, self.scope(request))
        await exchange.started.wait()

        return exchange.response(request)

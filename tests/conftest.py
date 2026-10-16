from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import mcp_types
import pytest
from fastapi import FastAPI
from httpx2 import AsyncClient
from mcp.shared.inbound import MCP_METHOD_HEADER, MCP_NAME_HEADER, MCP_PROTOCOL_VERSION_HEADER, NAME_BEARING_METHODS

from fastapi_mcp_toolsets.stdio.asgi import ASGIStreamTransport, lifespan

MODERN_VERSION = "2026-07-28"
HANDSHAKE_VERSION = "2025-11-25"
ACCEPT = "application/json, text/event-stream"


def modern_meta(capabilities: Mapping[str, Any] | None = None, **meta: Any) -> dict[str, Any]:
    return {
        mcp_types.PROTOCOL_VERSION_META_KEY: MODERN_VERSION,
        mcp_types.CLIENT_CAPABILITIES_META_KEY: dict(capabilities or {}),
        mcp_types.CLIENT_INFO_META_KEY: {"name": "tests", "version": "0"},
        **meta,
    }


@dataclass
class MCPClient:
    """Posts JSON-RPC to the endpoint the way the SDK's HTTP client would, on either protocol era."""

    http: AsyncClient
    path: str = "/mcp"
    counter: int = 0
    protocol_version: str | None = None

    def next_id(self) -> int:
        self.counter += 1
        return self.counter

    async def post(self, body: dict[str, Any], headers: Mapping[str, str] | None = None) -> Any:
        async with self.http.sse(
            self.path, method="POST", json=body, headers={"accept": ACCEPT, **(headers or {})}
        ) as source:
            response = source.response

            if response.status_code == 202:
                return None

            if response.headers.get("content-type", "").startswith("text/event-stream"):
                return [event.json() async for event in source if event.event == "message"]

            return (await response.aread()) and response.json()

    async def request(self, method: str, params: dict[str, Any] | None = None, **headers: str) -> Any:
        body = {"jsonrpc": "2.0", "id": self.next_id(), "method": method, "params": params or {}}
        return await self.post(body, headers=self.headers_for(method, params or {}) | headers)

    def headers_for(self, method: str, params: dict[str, Any]) -> dict[str, str]:
        meta = params.get("_meta") or {}

        if (version := meta.get(mcp_types.PROTOCOL_VERSION_META_KEY)) is not None:
            headers = {MCP_PROTOCOL_VERSION_HEADER: version, MCP_METHOD_HEADER: method}

            if (key := NAME_BEARING_METHODS.get(method)) is not None and isinstance(name := params.get(key), str):
                headers[MCP_NAME_HEADER] = name

            return headers

        if self.protocol_version is not None and method != "initialize":
            return {MCP_PROTOCOL_VERSION_HEADER: self.protocol_version}

        return {}

    async def initialize(self) -> dict[str, Any]:
        response = await self.request(
            "initialize",
            {"protocolVersion": HANDSHAKE_VERSION, "capabilities": {}, "clientInfo": {"name": "tests", "version": "0"}},
        )
        self.protocol_version = response["result"]["protocolVersion"]
        await self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=self.headers_for("x", {}))

        return response["result"]

    async def call(self, method: str, params: dict[str, Any] | None = None, **headers: str) -> dict[str, Any]:
        """A request's final JSON-RPC message, skipping notifications."""
        match await self.request(method, params, **headers):
            case list() as messages:
                return messages[-1]
            case dict() as message:
                return message
            case other:
                raise AssertionError(f"No response: {other!r}")


@asynccontextmanager
async def serve(app: FastAPI) -> AsyncGenerator[MCPClient]:
    """A client for the app, served in-process within its lifespan."""
    state: dict[str, Any] = {}
    transport = ASGIStreamTransport(app, state=state)

    async with lifespan(app, state), AsyncClient(transport=transport, base_url="http://test", timeout=None) as http:
        yield MCPClient(http=http)


@pytest.fixture
def connect() -> Callable[[FastAPI], AbstractAsyncContextManager[MCPClient]]:
    # A plain fixture: pytest-asyncio tears async fixtures down in another task, which anyio's scopes refuse.
    return serve

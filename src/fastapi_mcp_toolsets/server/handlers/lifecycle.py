from __future__ import annotations

from mcp_types import (
    DiscoverRequest,
    DiscoverResult,
    EmptyResult,
    InitializeRequest,
    InitializeResult,
    PingRequest,
    SetLevelRequest,
)

from fastapi_mcp_toolsets._compat import collect_toolset_instructions
from fastapi_mcp_toolsets.protocol import MCPProtocolVersion
from fastapi_mcp_toolsets.server.context import MCPContext

from .dispatch import DEFAULT_HANDLERS

__all__ = [
    "acknowledge",
    "discover",
    "initialize",
]


async def instructions(ctx: MCPContext) -> str | None:
    # What a model request would carry: the agent's and its capabilities' parts, then the toolsets'.
    parts = [
        *await ctx.instructions(ctx.run_context),
        *await collect_toolset_instructions(ctx.tool_manager.toolset, ctx.run_context),
    ]
    return "\n\n".join(part.content for part in parts) or None


@DEFAULT_HANDLERS.handler
async def discover(_request: DiscoverRequest, ctx: MCPContext) -> DiscoverResult:
    return DiscoverResult(
        supported_versions=[protocol.version for protocol in MCPProtocolVersion.modern()],
        capabilities=ctx.adapter.capabilities(ctx),
        instructions=await instructions(ctx),
    )


@DEFAULT_HANDLERS.handler
async def initialize(_request: InitializeRequest, ctx: MCPContext) -> InitializeResult:
    return InitializeResult(
        protocol_version=ctx.protocol_version.version,
        capabilities=ctx.adapter.capabilities(ctx),
        server_info=ctx.adapter.server_info,
        instructions=await instructions(ctx),
    )


# `logging/setLevel` is accepted, but there is no session to keep it in.
@DEFAULT_HANDLERS.handler
async def acknowledge(_request: PingRequest | SetLevelRequest, _ctx: MCPContext) -> EmptyResult:
    return EmptyResult()

from __future__ import annotations

from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, GetPromptRequest, GetPromptResult, ListPromptsRequest, ListPromptsResult

from fastapi_mcp_toolsets.server.context import MCPContext
from fastapi_mcp_toolsets.server.sources import served_prompts

from .dispatch import DEFAULT_HANDLERS, page

__all__ = [
    "get_prompt",
    "list_prompts",
]


@DEFAULT_HANDLERS.handler
async def list_prompts(request: ListPromptsRequest, ctx: MCPContext) -> ListPromptsResult:
    prompts, next_cursor = page([*served_prompts(ctx.capability).values()], request, ctx)

    return ListPromptsResult(prompts=[prompt.to_mcp() for prompt in prompts], next_cursor=next_cursor)


@DEFAULT_HANDLERS.handler
async def get_prompt(request: GetPromptRequest, ctx: MCPContext) -> GetPromptResult:
    name = request.params.name

    if (prompt := served_prompts(ctx.capability).get(name)) is None:
        raise MCPError(INVALID_PARAMS, f"Unknown prompt: {name}")

    return await prompt.get(request.params.arguments or {}, ctx.run_context, protocol_version=ctx.protocol_version)

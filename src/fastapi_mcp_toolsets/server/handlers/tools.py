from __future__ import annotations

import uuid
from typing import Any

from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import validate_mcp_param_headers
from mcp_types import INVALID_PARAMS, CallToolRequest, ElicitResult, ListToolsRequest, ListToolsResult, Result
from pydantic_ai import ApprovalRequired, ToolCallPart, ToolDefinition, ToolDenied

from fastapi_mcp_toolsets.features import check_header_params, mcp_tool, tool_error, tool_failure, tool_success
from fastapi_mcp_toolsets.server.context import MCPContext
from fastapi_mcp_toolsets.server.inputs import (
    APPROVAL_INPUT_KEY,
    InputRequired,
    approval_input,
    supports_form_elicitation,
)

from .dispatch import DEFAULT_HANDLERS, page
from .tasks import start_task

__all__ = [
    "call_tool",
    "list_tools",
]


@DEFAULT_HANDLERS.handler
async def list_tools(request: ListToolsRequest, ctx: MCPContext) -> ListToolsResult:
    tool_defs, next_cursor = page(ctx.tool_manager.tool_defs, request, ctx)

    for tool_def in tool_defs:
        check_header_params(tool_def)

    return ListToolsResult(
        tools=[mcp_tool(tool_def, protocol_version=ctx.protocol_version) for tool_def in tool_defs],
        next_cursor=next_cursor,
    )


@DEFAULT_HANDLERS.handler
async def call_tool(request: CallToolRequest, ctx: MCPContext) -> Result:
    name = request.params.name
    arguments = request.params.arguments or {}

    if (tool_def := ctx.tool_manager.get_tool_def(name)) is None:
        raise MCPError(INVALID_PARAMS, f"Unknown tool: {name}")

    # Arguments marked `x-mcp-header` must agree with their `Mcp-Param-*` headers.
    if (
        ctx.protocol_version.features.mcp_param_headers
        and ctx.headers is not None
        and (rejection := validate_mcp_param_headers(tool_def.parameters_json_schema, arguments, ctx.headers))
    ):
        raise MCPError(rejection.code, rejection.message, rejection.data)

    # A client that can poll may get a task to poll instead of waiting on the call.
    if (task := await start_task(tool_def, arguments, ctx)) is not None:
        return task

    match ctx.inputs.responses.get(APPROVAL_INPUT_KEY):
        case ElicitResult(action="accept"):
            approved = True
        case ElicitResult(action=action):
            verb = "declined" if action == "decline" else "dismissed"
            return tool_failure(f"The user {verb} the call to {name}.")
        case _:
            approved = False

    call = ToolCallPart(tool_name=name, args=arguments, tool_call_id=uuid.uuid4().hex)

    # Tool failures go in the result, not a JSON-RPC error; retrying is the client's call.
    try:
        output = await ctx.tool_manager.handle_call(
            call,
            approved=approved,
            metadata=ctx.inputs.metadata if approved else None,
            wrap_validation_errors=False,
        )
    except InputRequired:
        raise
    except ApprovalRequired as e:
        return request_approval(tool_def, arguments, e, ctx)
    except Exception as e:  # noqa: BLE001
        return tool_error(name, e)

    match output:
        case ToolDenied(message=message):
            return tool_failure(message)
        case _:
            return tool_success(output, tool_def, protocol_version=ctx.protocol_version)


def request_approval(
    tool_def: ToolDefinition,
    arguments: dict[str, Any],
    error: ApprovalRequired,
    ctx: MCPContext,
) -> Result:
    if not ctx.protocol_version.features.input_required:
        # Would need a server-to-client request, which a stateless server cannot await.
        return tool_failure(f"{tool_def.name} needs the user's approval, which this protocol version cannot ask for.")

    if not supports_form_elicitation(ctx.client_capabilities):
        return tool_failure(f"{tool_def.name} needs the user's approval, and this client cannot ask the user.")

    raise InputRequired(APPROVAL_INPUT_KEY, approval_input(tool_def.name, arguments), metadata=error.metadata)

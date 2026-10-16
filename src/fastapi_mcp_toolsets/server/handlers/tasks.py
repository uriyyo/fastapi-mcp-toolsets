from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Request as StarletteRequest
from fastapi_injected import push_inject_scope
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import MCP_NAME_HEADER, decode_header_value
from mcp_types import HEADER_MISMATCH, METHOD_NOT_FOUND, CallToolRequest, CallToolRequestParams, CallToolResult
from pydantic_ai import ToolDefinition

from fastapi_mcp_toolsets.protocol import MCPProtocolVersion
from fastapi_mcp_toolsets.server.context import MCPContext
from fastapi_mcp_toolsets.server.inputs import INPUT_RESPONSE, InputRequired, InputSession
from fastapi_mcp_toolsets.tasks import (
    TASKS_EXTENSION,
    CancelTaskRequest,
    CancelTaskResult,
    CreateTaskResult,
    GetTaskRequest,
    GetTaskResult,
    TaskBackend,
    TaskCall,
    TaskCompleted,
    TaskFailed,
    TaskInputs,
    TaskMode,
    TaskNeedsInput,
    TaskOutcome,
    TaskSupport,
    UpdateTaskRequest,
    UpdateTaskResult,
)
from fastapi_mcp_toolsets.tasks.wire import TASK_NAME_PARAMS, missing_tasks_capability

from .dispatch import DEFAULT_HANDLERS, handle

if TYPE_CHECKING:
    from fastapi_mcp_toolsets.server.server import MCPServer

__all__ = [
    "cancel_task",
    "get_task",
    "run_task",
    "start_task",
    "update_task",
]


def client_declares_tasks(ctx: MCPContext) -> bool:
    return TASKS_EXTENSION in (ctx.client_capabilities.extensions or {})


def tasks_backend(ctx: MCPContext, request: GetTaskRequest | UpdateTaskRequest | CancelTaskRequest) -> TaskBackend:
    match ctx.server.tasks:
        case None:
            raise MCPError(METHOD_NOT_FOUND, f"Method not supported: {request.method}")
        case backend:
            pass

    if not client_declares_tasks(ctx):
        raise missing_tasks_capability(request.method)

    # The task id is routed on, so the header a proxy read must be the id the body names.
    match (ctx.headers or {}).get(MCP_NAME_HEADER):
        case str() as header if decode_header_value(header) != request.params.task_id:
            param = TASK_NAME_PARAMS[request.method]
            raise MCPError(
                HEADER_MISMATCH, f"{MCP_NAME_HEADER} header does not match the request body's {param!r} parameter"
            )

    return backend


@DEFAULT_HANDLERS.handler
async def get_task(request: GetTaskRequest, ctx: MCPContext) -> GetTaskResult:
    return await tasks_backend(ctx, request).get(request.params.task_id)


@DEFAULT_HANDLERS.handler
async def update_task(request: UpdateTaskRequest, ctx: MCPContext) -> UpdateTaskResult:
    await tasks_backend(ctx, request).update(request.params.task_id, request.params.input_responses)
    return UpdateTaskResult()


@DEFAULT_HANDLERS.handler
async def cancel_task(request: CancelTaskRequest, ctx: MCPContext) -> CancelTaskResult:
    await tasks_backend(ctx, request).cancel(request.params.task_id)
    return CancelTaskResult()


async def start_task(tool_def: ToolDefinition, arguments: dict[str, Any], ctx: MCPContext) -> CreateTaskResult | None:
    """The task a `tools/call` is answered with instead of its result, or `None` to answer inline."""
    match ctx.server.tasks, TaskSupport.of(tool_def):
        case None, _:
            return None
        case _, None | TaskSupport(mode=TaskMode.FORBIDDEN):
            return None
        case backend, TaskSupport(mode=mode) as support:
            pass

    # A task's legs run through this same handler; they are answered inline. Extensions are negotiated per
    # request, which only the modern versions do.
    if ctx.task_id is not None or ctx.protocol_version.features.handshake:
        return None

    match mode, client_declares_tasks(ctx):
        case TaskMode.REQUIRED, False:
            raise missing_tasks_capability(f"Tool {tool_def.name!r}")
        case _, False:
            return None
        case _:
            pass

    call = TaskCall(
        tool=tool_def.name,
        arguments=arguments,
        protocol_version=ctx.protocol_version.version,
        client_capabilities=ctx.client_capabilities,
        client_info=ctx.client_info,
        headers=dict(ctx.headers or {}),
        log_level=ctx.log_level,
        ttl=support.ttl,
        poll_interval=support.poll_interval,
    )

    return await backend.create(call)


def task_request(server: MCPServer[Any], call: TaskCall) -> StarletteRequest:
    # What the call's request looked like, so header and request-bound dependencies resolve as they did.
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(name.lower().encode(), value.encode()) for name, value in call.headers.items()],
        "app": server.app,
    }

    return StarletteRequest(scope)


async def run_task(server: MCPServer[Any], task_id: str, call: TaskCall, inputs: TaskInputs) -> TaskOutcome:
    """One leg of a task: the tool call until its result, or until it needs input the client has not given."""
    request = CallToolRequest(params=CallToolRequestParams(name=call.tool, arguments=call.arguments))

    async with (
        push_inject_scope(request=task_request(server, call)),
        server.build_context(
            protocol_version=MCPProtocolVersion(call.protocol_version),
            client_capabilities=call.client_capabilities,
            client_info=call.client_info,
            headers=call.headers,
        ) as ctx,
    ):
        ctx.task_id = task_id
        ctx.log_level = call.log_level
        ctx.inputs = InputSession(
            responses={key: INPUT_RESPONSE.validate_python(value) for key, value in inputs.responses.items()},
            metadata=inputs.metadata,
        )

        try:
            result = await server._within_run(ctx, lambda: handle(request, ctx))  # noqa: SLF001
        except InputRequired as e:
            return TaskNeedsInput(
                requests={e.key: e.request},
                inputs=TaskInputs(
                    responses=dict(inputs.responses),
                    pending=(e.key,),
                    metadata=ctx.inputs.metadata if e.metadata is None else e.metadata,
                ),
            )
        except MCPError as e:
            return TaskFailed(error=e.error)

    match result:
        case CallToolResult():
            return TaskCompleted(result=result)
        case _:
            raise TypeError(f"A tool call answered with {type(result).__name__}, not a CallToolResult")

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from types import UnionType
from typing import Any, Union, cast, get_args, get_origin, get_type_hints

from mcp.shared.exceptions import MCPError
from mcp_types import METHOD_NOT_FOUND, PaginatedRequest, Request, Result
from pydantic.fields import FieldInfo
from pydantic_ai import UserError
from pydantic_ai.capabilities.abstract import AbstractCapability

from fastapi_mcp_toolsets._typing import AgentDepsT
from fastapi_mcp_toolsets.protocol import page_cursor, paginate
from fastapi_mcp_toolsets.server.context import MCPContext
from fastapi_mcp_toolsets.server.sources import find_capabilities

__all__ = [
    "DEFAULT_HANDLERS",
    "MCPHandler",
    "MCPHandlers",
    "extension_request_types",
    "handle",
    "page",
    "parse_extension_request",
]

type MCPHandler = Callable[[Any, MCPContext], Awaitable[Result]]


def request_types(handler: MCPHandler) -> list[type[Request[Any, Any]]]:
    # Read off the first parameter's annotation, as `functools.singledispatch` does.
    first = next(iter(inspect.signature(handler).parameters), None)
    annotation = get_type_hints(handler).get(first) if first is not None else None
    members = get_args(annotation) if get_origin(annotation) in {Union, UnionType} else (annotation,)

    if not members or not all(inspect.isclass(member) and issubclass(member, Request) for member in members):
        name = getattr(handler, "__name__", repr(handler))
        raise UserError(f"{name} must annotate its first parameter with the request type(s) it handles")

    return [cast("type[Request[Any, Any]]", member) for member in members]


@dataclass
class MCPHandlers(AbstractCapability[AgentDepsT]):
    """MCP method handlers by request type; a later capability's handler wins over an earlier one's."""

    handlers: dict[type[Request[Any, Any]], MCPHandler] = field(default_factory=dict)
    # By method, for the requests the spec's own tables do not parse: an extension's.
    requests: dict[str, type[Request[Any, Any]]] = field(default_factory=dict)

    def handler[H: MCPHandler](self, handler: H, /) -> H:
        for request_type in request_types(handler):
            self.handlers[request_type] = handler

            match request_type.model_fields.get("method"):
                case FieldInfo(default=str() as method):
                    self.requests[method] = request_type

        return handler


# The built-in handlers, which capabilities can override; filled in by the modules next to this one.
DEFAULT_HANDLERS: MCPHandlers[Any] = MCPHandlers()


def extension_request_types(ctx: MCPContext) -> dict[str, type[Request[Any, Any]]]:
    requests = dict(DEFAULT_HANDLERS.requests)

    for found in find_capabilities(ctx.capability, MCPHandlers):
        requests |= found.requests

    return requests


async def handle(request: Request[Any, Any], ctx: MCPContext) -> Result:
    handlers = dict(DEFAULT_HANDLERS.handlers)

    for found in find_capabilities(ctx.capability, MCPHandlers):
        handlers |= found.handlers

    for request_type in type(request).__mro__:
        if (handler := handlers.get(request_type)) is not None:
            return await handler(request, ctx)

    raise MCPError(METHOD_NOT_FOUND, f"Method not supported: {request.method}")


def parse_extension_request(method: str, params: Any, ctx: MCPContext) -> Request[Any, Any] | None:
    """A request of an extension's method, or `None` when nothing serves the method.

    Raises `ValidationError` for params the method rejects, as `parse_client_request` does.
    """
    match extension_request_types(ctx).get(method):
        case None:
            return None
        case request_type:
            return request_type.model_validate({"method": method, "params": params}, by_name=False)


def page[T](
    items: Sequence[T],
    request: PaginatedRequest[Any],
    ctx: MCPContext,
) -> tuple[Sequence[T], str | None]:
    return paginate(
        items,
        cursor=page_cursor(request),
        page_size=ctx.server.page_size,
    )

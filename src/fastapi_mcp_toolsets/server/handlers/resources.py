from __future__ import annotations

from mcp_types import (
    ListResourcesRequest,
    ListResourcesResult,
    ListResourceTemplatesRequest,
    ListResourceTemplatesResult,
    ReadResourceRequest,
    ReadResourceResult,
)

from fastapi_mcp_toolsets.features import find_resource
from fastapi_mcp_toolsets.server.context import MCPContext
from fastapi_mcp_toolsets.server.sources import served_resources

from .dispatch import DEFAULT_HANDLERS, page

__all__ = [
    "list_resource_templates",
    "list_resources",
    "read_resource",
]


@DEFAULT_HANDLERS.handler
async def list_resources(request: ListResourcesRequest, ctx: MCPContext) -> ListResourcesResult:
    resources, next_cursor = page(
        [resource for resource in served_resources(ctx.capability) if not resource.is_template],
        request,
        ctx,
    )

    return ListResourcesResult(resources=[resource.to_resource() for resource in resources], next_cursor=next_cursor)


@DEFAULT_HANDLERS.handler
async def list_resource_templates(
    request: ListResourceTemplatesRequest,
    ctx: MCPContext,
) -> ListResourceTemplatesResult:
    templates, next_cursor = page(
        [resource for resource in served_resources(ctx.capability) if resource.is_template],
        request,
        ctx,
    )

    return ListResourceTemplatesResult(
        resource_templates=[template.to_template() for template in templates],
        next_cursor=next_cursor,
    )


@DEFAULT_HANDLERS.handler
async def read_resource(request: ReadResourceRequest, ctx: MCPContext) -> ReadResourceResult:
    uri = request.params.uri
    resource, arguments = find_resource(served_resources(ctx.capability), uri)

    return ReadResourceResult(contents=await resource.read(uri, arguments, ctx.run_context))

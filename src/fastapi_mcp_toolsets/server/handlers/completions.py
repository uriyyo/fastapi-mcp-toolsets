from __future__ import annotations

from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, CompleteRequest, CompleteResult, PromptReference, ResourceTemplateReference

from fastapi_mcp_toolsets.features import Completer, complete
from fastapi_mcp_toolsets.server.context import MCPContext
from fastapi_mcp_toolsets.server.sources import served_prompts, served_resources

from .dispatch import DEFAULT_HANDLERS

__all__ = [
    "complete_argument",
]


@DEFAULT_HANDLERS.handler
async def complete_argument(request: CompleteRequest, ctx: MCPContext) -> CompleteResult:
    argument = request.params.argument
    context = request.params.context

    completion = await complete(
        find_completer(request.params.ref, argument.name, ctx),
        ctx.run_context,
        argument.value,
        (context.arguments if context is not None else None) or {},
    )

    return CompleteResult(completion=completion)


def find_completer(
    ref: PromptReference | ResourceTemplateReference,
    argument: str,
    ctx: MCPContext,
) -> Completer | None:
    match ref:
        case PromptReference(name=name):
            if (prompt := served_prompts(ctx.capability).get(name)) is None:
                raise MCPError(INVALID_PARAMS, f"Unknown prompt: {name}")

            return prompt.complete.get(argument)
        case ResourceTemplateReference(uri=uri):
            for resource in served_resources(ctx.capability):
                if resource.is_template and resource.uri == uri:
                    return resource.complete.get(argument)

            raise MCPError(INVALID_PARAMS, f"Unknown resource template: {uri}")

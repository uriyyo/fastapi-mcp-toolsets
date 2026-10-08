from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS
from pydantic import ValidationError
from pydantic.json_schema import GenerateJsonSchema
from pydantic_ai import RunContext
from pydantic_ai._function_schema import FunctionSchema, function_schema
from pydantic_ai.tools import DocstringFormat, GenerateToolJsonSchema

__all__ = [
    "FunctionSchema",
    "build_function_schema",
    "call_function",
]


def build_function_schema(
    function: Callable[..., Any],
    *,
    name: str | None,
    docstring_format: DocstringFormat = "auto",
    require_parameter_descriptions: bool = False,
    schema_generator: type[GenerateJsonSchema] = GenerateToolJsonSchema,
) -> FunctionSchema:
    # The same options and defaults as `FunctionToolset.tool`.
    return function_schema(
        function,
        schema_generator,
        tool_name=name,
        docstring_format=docstring_format,
        require_parameter_descriptions=require_parameter_descriptions,
    )


async def call_function(schema: FunctionSchema, arguments: Mapping[str, Any], ctx: RunContext[Any]) -> Any:
    try:
        validated = schema.validator.validate_python(dict(arguments), context=ctx.validation_context)
    except ValidationError as e:
        raise MCPError(INVALID_PARAMS, f"Invalid arguments for {schema.name}: {e}") from None

    return await schema.call(validated, ctx)

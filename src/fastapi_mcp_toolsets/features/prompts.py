from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, overload

from mcp.server.mcpserver.prompts.base import Message
from mcp_types import GetPromptResult, Icon, Prompt, PromptArgument, PromptMessage
from pydantic.json_schema import GenerateJsonSchema
from pydantic_ai import RunContext, UserError
from pydantic_ai.capabilities.abstract import AbstractCapability
from pydantic_ai.tools import DocstringFormat, GenerateToolJsonSchema

from fastapi_mcp_toolsets._typing import AgentDepsT
from fastapi_mcp_toolsets.protocol import MCPProtocolVersion

from .completions import Completer
from .functions import FunctionSchema, build_function_schema, call_function
from .tools import content_block

__all__ = [
    "MCPPrompts",
    "PromptFunction",
]


@dataclass(frozen=True, kw_only=True)
class PromptFunction:
    name: str
    title: str | None = None
    description: str | None = None
    icons: Sequence[Icon] | None = None
    meta: Mapping[str, Any] | None = None
    schema: FunctionSchema = field(repr=False)
    complete: Mapping[str, Completer] = field(default_factory=dict, repr=False)

    def to_mcp(self) -> Prompt:
        properties: dict[str, Any] = self.schema.json_schema.get("properties", {})
        required = set(self.schema.json_schema.get("required", []))

        return Prompt(
            name=self.name,
            title=self.title,
            description=self.description,
            arguments=[
                PromptArgument(name=name, description=schema.get("description"), required=name in required)
                for name, schema in properties.items()
            ]
            or None,
            icons=None if self.icons is None else [*self.icons],
            meta=None if self.meta is None else {**self.meta},
        )

    async def get(
        self,
        arguments: Mapping[str, str],
        ctx: RunContext[Any],
        *,
        protocol_version: MCPProtocolVersion,
    ) -> GetPromptResult:
        output = await call_function(self.schema, arguments, ctx)

        return GetPromptResult(
            description=self.description,
            messages=prompt_messages(output, protocol_version=protocol_version),
        )


@dataclass
class MCPPrompts(AbstractCapability[AgentDepsT]):
    prompts: dict[str, PromptFunction] = field(default_factory=dict)

    @overload
    def prompt[F: Callable[..., Any]](self, function: F, /) -> F: ...

    @overload
    def prompt[F: Callable[..., Any]](
        self,
        /,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        icons: Sequence[Icon] | None = None,
        meta: Mapping[str, Any] | None = None,
        complete: Mapping[str, Completer] | None = None,
        docstring_format: DocstringFormat = "auto",
        require_parameter_descriptions: bool = False,
        schema_generator: type[GenerateJsonSchema] = GenerateToolJsonSchema,
    ) -> Callable[[F], F]: ...

    def prompt(
        self,
        function: Callable[..., Any] | None = None,
        /,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        icons: Sequence[Icon] | None = None,
        meta: Mapping[str, Any] | None = None,
        complete: Mapping[str, Completer] | None = None,
        docstring_format: DocstringFormat = "auto",
        require_parameter_descriptions: bool = False,
        schema_generator: type[GenerateJsonSchema] = GenerateToolJsonSchema,
    ) -> Any:

        def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
            schema = build_function_schema(
                function,
                name=name,
                docstring_format=docstring_format,
                require_parameter_descriptions=require_parameter_descriptions,
                schema_generator=schema_generator,
            )

            if schema.name in self.prompts:
                raise UserError(f"A prompt named {schema.name!r} is already registered")

            if unknown := set(complete or {}) - set(schema.json_schema.get("properties", {})):
                raise UserError(f"{schema.name} has no arguments {sorted(unknown)} to complete")

            self.prompts[schema.name] = PromptFunction(
                name=schema.name,
                title=title,
                description=description or schema.description,
                icons=icons,
                meta=meta,
                schema=schema,
                complete=complete or {},
            )

            return function

        return decorator if function is None else decorator(function)


def prompt_messages(output: Any, *, protocol_version: MCPProtocolVersion) -> list[PromptMessage]:
    # Anything but a message is said by the user.
    match output:
        case PromptMessage():
            return [output]
        case Message(role=role, content=content):
            return [PromptMessage(role=role, content=content)]
        case list() | tuple():
            return [message for item in output for message in prompt_messages(item, protocol_version=protocol_version)]
        case _:
            return [PromptMessage(role="user", content=content_block(output, protocol_version=protocol_version))]

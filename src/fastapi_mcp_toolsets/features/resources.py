from __future__ import annotations

import base64
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver.resources.templates import DEFAULT_RESOURCE_SECURITY, ResourceSecurity
from mcp.shared.exceptions import MCPError
from mcp.shared.uri_template import UriTemplate
from mcp_types import (
    INVALID_PARAMS,
    Annotations,
    BlobResourceContents,
    Icon,
    Resource,
    ResourceTemplate,
    TextResourceContents,
)
from pydantic.json_schema import GenerateJsonSchema
from pydantic_ai import BinaryContent, RunContext, UserError
from pydantic_ai.capabilities.abstract import AbstractCapability
from pydantic_ai.tools import DocstringFormat, GenerateToolJsonSchema
from pydantic_core import to_json

from fastapi_mcp_toolsets._typing import AgentDepsT

from .apps import APP_MIME_TYPE, UI_SCHEME
from .completions import Completer
from .functions import FunctionSchema, build_function_schema, call_function

__all__ = [
    "MCPResources",
    "ResourceFunction",
    "find_resource",
]


type ResourceContents = TextResourceContents | BlobResourceContents


@dataclass(frozen=True, kw_only=True)
class ResourceFunction:
    # A URI, or an RFC 6570 template whose variables are the function's parameters.
    uri: str
    name: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    size: int | None = None
    icons: Sequence[Icon] | None = None
    annotations: Annotations | None = None
    meta: Mapping[str, Any] | None = None
    schema: FunctionSchema = field(repr=False)
    complete: Mapping[str, Completer] = field(default_factory=dict, repr=False)
    template: UriTemplate | None = field(default=None, repr=False)
    security: ResourceSecurity = field(default=DEFAULT_RESOURCE_SECURITY, repr=False)

    @property
    def is_template(self) -> bool:
        return self.template is not None

    def match(self, uri: str) -> dict[str, str | list[str]] | None:
        if self.template is None:
            return {} if uri == self.uri else None

        if (arguments := self.template.match(uri)) is None:
            return None

        # Raised rather than skipped, so a more permissive template cannot match instead.
        if (rejected := self.security.validate(arguments)) is not None:
            raise MCPError(INVALID_PARAMS, f"Invalid value for {rejected!r} in {uri}", {"uri": uri})

        return arguments

    def to_resource(self) -> Resource:
        return Resource(
            uri=self.uri,
            name=self.name,
            title=self.title,
            description=self.description,
            mime_type=self.mime_type,
            size=self.size,
            icons=None if self.icons is None else [*self.icons],
            annotations=self.annotations,
            meta=None if self.meta is None else {**self.meta},
        )

    def to_template(self) -> ResourceTemplate:
        return ResourceTemplate(
            uri_template=self.uri,
            name=self.name,
            title=self.title,
            description=self.description,
            mime_type=self.mime_type,
            icons=None if self.icons is None else [*self.icons],
            annotations=self.annotations,
            meta=None if self.meta is None else {**self.meta},
        )

    async def read(
        self,
        uri: str,
        arguments: Mapping[str, str | list[str]],
        ctx: RunContext[Any],
    ) -> list[ResourceContents]:
        return resource_contents(uri, await call_function(self.schema, arguments, ctx), mime_type=self.mime_type)


def check_template_parameters(template: UriTemplate, schema: FunctionSchema) -> None:
    variables = set(template.variable_names)
    parameters = set(schema.json_schema.get("properties", {}))
    required = set(schema.json_schema.get("required", []))

    if unknown := variables - parameters:
        raise UserError(f"{template} has variables that {schema.name} does not take: {sorted(unknown)}")

    if missing := required - variables:
        raise UserError(f"{schema.name} requires parameters that {template} does not have: {sorted(missing)}")


@dataclass
class MCPResources(AbstractCapability[AgentDepsT]):
    resources: list[ResourceFunction] = field(default_factory=list)

    def resource[F: Callable[..., Any]](
        self,
        uri: str,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        mime_type: str | None = None,
        size: int | None = None,
        icons: Sequence[Icon] | None = None,
        annotations: Annotations | None = None,
        meta: Mapping[str, Any] | None = None,
        complete: Mapping[str, Completer] | None = None,
        security: ResourceSecurity = DEFAULT_RESOURCE_SECURITY,
        docstring_format: DocstringFormat = "auto",
        require_parameter_descriptions: bool = False,
        schema_generator: type[GenerateJsonSchema] = GenerateToolJsonSchema,
    ) -> Callable[[F], F]:

        def decorator(function: F) -> F:
            schema = build_function_schema(
                function,
                name=name,
                docstring_format=docstring_format,
                require_parameter_descriptions=require_parameter_descriptions,
                schema_generator=schema_generator,
            )
            template = UriTemplate.parse(uri) if UriTemplate.is_template(uri) else None

            if template is not None:
                check_template_parameters(template, schema)
            elif complete:
                raise UserError(f"Only a resource template has arguments to complete, not {uri}")

            self.resources.append(
                ResourceFunction(
                    uri=uri,
                    name=schema.name,
                    title=title,
                    description=description or schema.description,
                    mime_type=APP_MIME_TYPE if mime_type is None and uri.startswith(UI_SCHEME) else mime_type,
                    size=size,
                    icons=icons,
                    annotations=annotations,
                    meta=meta,
                    schema=schema,
                    complete=complete or {},
                    template=template,
                    security=security,
                )
            )

            return function

        return decorator


def find_resource(
    resources: Iterable[ResourceFunction],
    uri: str,
) -> tuple[ResourceFunction, dict[str, str | list[str]]]:
    for resource in sorted(resources, key=lambda resource: resource.is_template):
        if (arguments := resource.match(uri)) is not None:
            return resource, arguments

    raise MCPError(INVALID_PARAMS, f"Resource not found: {uri}", {"uri": uri})


def resource_contents(uri: str, output: Any, *, mime_type: str | None) -> list[ResourceContents]:
    match output:
        case TextResourceContents() | BlobResourceContents():
            return [output]
        case list() | tuple():
            return [contents for item in output for contents in resource_contents(uri, item, mime_type=mime_type)]
        case str():
            return [TextResourceContents(uri=uri, mime_type=mime_type or "text/plain", text=output)]
        case bytes():
            blob = base64.b64encode(output).decode()
            return [BlobResourceContents(uri=uri, mime_type=mime_type or "application/octet-stream", blob=blob)]
        case BinaryContent():
            return [BlobResourceContents(uri=uri, mime_type=output.media_type, blob=output.base64)]
        case _:
            text = to_json(output).decode()
            return [TextResourceContents(uri=uri, mime_type=mime_type or "application/json", text=text)]

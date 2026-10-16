from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from mcp.shared.inbound import X_MCP_HEADER_KEY, find_invalid_x_mcp_header
from mcp_types import (
    AudioContent,
    BlobResourceContents,
    CallToolResult,
    ContentBlock,
    EmbeddedResource,
    Icon,
    ImageContent,
    ResourceLink,
    TextContent,
    Tool,
    ToolAnnotations,
)
from pydantic import Field, ValidationError
from pydantic_ai import BinaryContent, CallDeferred, ModelRetry, ToolDefinition, ToolFailed, ToolReturn, UserError
from pydantic_ai.messages import FileUrl
from pydantic_core import to_json, to_jsonable_python

from fastapi_mcp_toolsets.protocol import MCPProtocolVersion

from .apps import AppUI
from .config import ToolConfig

__all__ = [
    "WRAPPED_RESULT_KEY",
    "ToolAnnotation",
    "check_header_params",
    "mcp_header",
    "mcp_tool",
    "tool_error",
    "tool_failure",
    "tool_success",
]

WRAPPED_RESULT_KEY: Final[str] = "result"

MULTIMODAL_KINDS: Final[frozenset[str]] = frozenset(
    {
        "binary",
        "image-url",
        "audio-url",
        "video-url",
        "document-url",
    }
)


@dataclass(frozen=True, kw_only=True)
class ToolAnnotation(ToolConfig):
    title: str | None = None
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None
    idempotent_hint: bool | None = None
    open_world_hint: bool | None = None
    icons: Sequence[Icon] | None = None
    meta: Mapping[str, Any] | None = None

    def to_mcp(self) -> ToolAnnotations | None:
        hints = (self.title, self.read_only_hint, self.destructive_hint, self.idempotent_hint, self.open_world_hint)

        if all(hint is None for hint in hints):
            return None

        return ToolAnnotations(
            title=self.title,
            read_only_hint=self.read_only_hint,
            destructive_hint=self.destructive_hint,
            idempotent_hint=self.idempotent_hint,
            open_world_hint=self.open_world_hint,
        )


def is_multimodal_schema(schema: Any) -> bool:
    match schema:
        case {"properties": {"kind": {"const": str() as kind}}} if kind in MULTIMODAL_KINDS:
            return True
        case dict():
            return any(is_multimodal_schema(value) for value in schema.values())
        case list():
            return any(is_multimodal_schema(value) for value in schema)
        case _:
            return False


def output_schema(tool_def: ToolDefinition, *, protocol_version: MCPProtocolVersion) -> dict[str, Any] | None:
    if not protocol_version.features.structured_content:
        return None

    if not tool_def.return_schema or is_multimodal_schema(tool_def.return_schema):
        return None

    return tool_def.return_schema


def wraps_result(
    return_schema: dict[str, Any] | None,
    *,
    protocol_version: MCPProtocolVersion,
) -> bool:
    match return_schema:
        case {"type": "object"}:
            return False
        case _:
            return bool(return_schema) and not protocol_version.features.any_structured_content


def mcp_tool(
    tool_def: ToolDefinition,
    *,
    protocol_version: MCPProtocolVersion,
) -> Tool:
    schema = output_schema(tool_def, protocol_version=protocol_version)

    if wraps_result(schema, protocol_version=protocol_version):
        schema = {
            "type": "object",
            "properties": {WRAPPED_RESULT_KEY: schema},
            "required": [WRAPPED_RESULT_KEY],
        }

    tool = Tool(
        name=tool_def.name,
        description=tool_def.description or None,
        input_schema=tool_def.parameters_json_schema,
        output_schema=schema,
    )

    match ToolAnnotation.of(tool_def):
        case ToolAnnotation() as annotation:
            tool.title = annotation.title
            tool.icons = None if annotation.icons is None else [*annotation.icons]
            tool.meta = None if annotation.meta is None else {**annotation.meta}
            tool.annotations = annotation.to_mcp()

    match AppUI.of(tool_def):
        case AppUI() as app:
            tool.meta = {**(tool.meta or {}), "ui": app.to_meta()}

    return tool


def mcp_header(name: str) -> Any:
    """Marks a tool argument as mirrored into the `Mcp-Param-<name>` header: `Annotated[str, mcp_header("Tenant")]`."""
    return Field(json_schema_extra={X_MCP_HEADER_KEY: name})


def check_header_params(tool_def: ToolDefinition) -> None:
    # A bad annotation would make clients drop the tool silently; better the developer hears about it.
    if (reason := find_invalid_x_mcp_header(tool_def.parameters_json_schema)) is not None:
        raise UserError(f"Tool {tool_def.name!r} has an invalid header parameter: {reason}")


def structured_content(data: Any, tool_def: ToolDefinition, *, protocol_version: MCPProtocolVersion) -> Any:
    if not protocol_version.features.structured_content:
        return None

    if wraps_result(output_schema(tool_def, protocol_version=protocol_version), protocol_version=protocol_version):
        return {WRAPPED_RESULT_KEY: data}

    if protocol_version.features.any_structured_content or isinstance(data, dict):
        return data

    return None


def is_multimodal(output: Any) -> bool:
    match output:
        case BinaryContent() | FileUrl():
            return True
        case list() | tuple():
            return any(is_multimodal(item) for item in output)
        case _:
            return False


def file_media_type(file: FileUrl) -> str | None:
    try:
        return file.media_type
    except ValueError:
        return None


def binary_block(item: BinaryContent) -> ContentBlock:
    if item.is_image:
        return ImageContent(data=item.base64, mime_type=item.media_type)

    if item.is_audio:
        return AudioContent(data=item.base64, mime_type=item.media_type)

    return EmbeddedResource(
        resource=BlobResourceContents(
            uri=f"binary://{item.identifier}",
            mime_type=item.media_type,
            blob=item.base64,
        ),
    )


def content_block(item: Any, *, protocol_version: MCPProtocolVersion) -> ContentBlock:
    match item:
        case str():
            return TextContent(text=item)
        case BinaryContent():
            return binary_block(item)
        case FileUrl() if not protocol_version.features.resource_links:
            return TextContent(text=item.url)
        case FileUrl():
            return ResourceLink(uri=item.url, name=item.identifier, mime_type=file_media_type(item))
        case _:
            return TextContent(text=to_json(item).decode())


def content_blocks(content: Any, *, protocol_version: MCPProtocolVersion) -> list[ContentBlock]:
    match content:
        case None:
            return []
        case list() | tuple():
            return [content_block(item, protocol_version=protocol_version) for item in content]
        case _:
            return [content_block(content, protocol_version=protocol_version)]


def tool_success(output: Any, tool_def: ToolDefinition, *, protocol_version: MCPProtocolVersion) -> CallToolResult:
    match output:
        # Extra content follows the result; metadata becomes the result's `_meta`.
        case ToolReturn(return_value=return_value, content=content, metadata=metadata):
            result = tool_success(return_value, tool_def, protocol_version=protocol_version)
            result.content = [*result.content, *content_blocks(content, protocol_version=protocol_version)]
            result.meta = dict(metadata) if isinstance(metadata, Mapping) else None
            return result

    if is_multimodal(output):
        # No structured content: `mcp_tool` advertises no output schema for files.
        return CallToolResult(content=content_blocks(output, protocol_version=protocol_version))

    data = to_jsonable_python(output)

    match output:
        case str():
            text = output
        case _:
            text = to_json(data).decode()

    return CallToolResult(
        content=[TextContent(text=text)],
        structured_content=structured_content(data, tool_def, protocol_version=protocol_version),
    )


def tool_failure(message: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(text=message)],
        is_error=True,
    )


def tool_error(tool_name: str, error: Exception) -> CallToolResult:
    match error:
        case ValidationError():
            return tool_failure(f"Invalid arguments for {tool_name}: {error}")
        # Both carry a message meant for the model as it is.
        case ModelRetry() | ToolFailed():
            return tool_failure(str(error))
        case CallDeferred():
            return tool_failure(
                f"{tool_name} defers its result to outside the call, which this server cannot wait for."
            )
        case _:
            return tool_failure(f"{type(error).__name__}: {error}")

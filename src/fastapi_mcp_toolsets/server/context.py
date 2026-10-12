from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast, get_args

from anyio.abc import ObjectSendStream
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INVALID_PARAMS,
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    ClientCapabilities,
    CreateMessageRequest,
    CreateMessageRequestParams,
    CreateMessageResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    GetPromptRequest,
    GetPromptRequestParams,
    GetPromptResult,
    Implementation,
    InputRequiredResult,
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCResponse,
    ListRootsRequest,
    ListRootsResult,
    LoggingLevel,
    LoggingMessageNotification,
    LoggingMessageNotificationParams,
    ModelPreferences,
    ProgressNotification,
    ProgressNotificationParams,
    ProgressToken,
    ReadResourceRequest,
    ReadResourceRequestParams,
    ReadResourceResult,
    Request,
    Result,
    Root,
    SamplingMessage,
    ServerNotification,
    TextContent,
)
from pydantic import TypeAdapter, ValidationError
from pydantic_ai import InstructionPart, RunContext
from pydantic_ai.capabilities.abstract import AbstractCapability
from pydantic_ai.tool_manager import ToolManager

from fastapi_mcp_toolsets._compat import ElicitationJsonSchema, validate_elicitation_schema
from fastapi_mcp_toolsets.protocol import MCPProtocolVersion, jsonrpc_notification

from .inputs import Elicitation, InputSession, InputUnavailableError, supports_form_elicitation

if TYPE_CHECKING:
    from .server import MCPServer

__all__ = [
    "InstructionsResolver",
    "MCPContext",
    "MCPDeps",
    "MCPMessageStream",
]

type MCPMessageStream = ObjectSendStream[JSONRPCResponse | JSONRPCError | JSONRPCNotification]
type InstructionsResolver[DepsT] = Callable[[RunContext[DepsT]], Awaitable[list[InstructionPart]]]

LOGGING_LEVELS: Final[tuple[LoggingLevel, ...]] = get_args(LoggingLevel)


@dataclass(kw_only=True)
class MCPContext:
    server: MCPServer[Any]
    run_context: RunContext[Any]
    capability: AbstractCapability[Any]
    tool_manager: ToolManager[Any]
    # The agent's and its capabilities' instructions; toolset instructions are collected separately.
    instructions: InstructionsResolver[Any]

    protocol_version: MCPProtocolVersion = field(default_factory=MCPProtocolVersion.latest)
    client_capabilities: ClientCapabilities = field(default_factory=ClientCapabilities)
    client_info: Implementation | None = None
    log_level: LoggingLevel | None = None

    headers: Mapping[str, str] | None = None

    # Only an SSE response has one; without it, notifications are dropped.
    stream: MCPMessageStream | None = None

    progress_token: ProgressToken | None = None

    inputs: InputSession = field(default_factory=InputSession)

    @property
    def can_send_notifications(self) -> bool:
        return self.stream is not None

    async def send_notification(self, notification: ServerNotification) -> None:
        if self.stream is not None:
            await self.stream.send(jsonrpc_notification(notification))

    async def log(self, level: LoggingLevel, data: Any, *, logger: str | None = None) -> None:
        if self.log_level is None or LOGGING_LEVELS.index(level) < LOGGING_LEVELS.index(self.log_level):
            return

        await self.send_notification(
            LoggingMessageNotification(
                params=LoggingMessageNotificationParams(level=level, logger=logger, data=data),
            )
        )

    async def report_progress(self, progress: float, total: float | None = None, message: str | None = None) -> None:
        if self.progress_token is None:
            return

        await self.send_notification(
            ProgressNotification(
                params=ProgressNotificationParams(
                    progress_token=self.progress_token,
                    progress=progress,
                    total=total,
                    message=message,
                ),
            )
        )

    def require_input(self, kind: str, *, supported: bool) -> None:
        if not self.protocol_version.features.input_required:
            raise InputUnavailableError(f"Protocol {self.protocol_version.version} cannot ask the client for {kind}")

        if not supported:
            raise InputUnavailableError(f"The client cannot answer {kind} requests")

    async def elicit[T](self, message: str, schema: type[T], *, key: str | None = None) -> Elicitation[T]:
        self.require_input(
            "elicitation",
            supported=supports_form_elicitation(self.client_capabilities),
        )

        adapter = TypeAdapter(schema)
        # As the SDK renders it: optional fields left out of `required`, and only primitives allowed.
        requested_schema = adapter.json_schema(schema_generator=ElicitationJsonSchema)
        validate_elicitation_schema(requested_schema)

        request = ElicitRequest(
            params=ElicitRequestFormParams(message=message, requested_schema=requested_schema),
        )

        match self.inputs.take(request, kind="elicit", key=key):
            case ElicitResult(action="accept", content=content):
                try:
                    return Elicitation(action="accept", data=adapter.validate_python(content or {}))
                except ValidationError as e:
                    # Not a `ValidationError`, which would read as invalid tool arguments.
                    raise MCPError(INVALID_PARAMS, f"Invalid answer to {message!r}: {e}") from None
            case ElicitResult(action=action):
                return Elicitation(action=action)
            case response:
                raise MCPError(INVALID_PARAMS, f"Expected an elicitation result, got {type(response).__name__}")

    async def sample(
        self,
        messages: str | Sequence[SamplingMessage],
        *,
        max_tokens: int,
        system_prompt: str | None = None,
        temperature: float | None = None,
        model_preferences: ModelPreferences | None = None,
        key: str | None = None,
    ) -> CreateMessageResult:
        self.require_input(
            "sampling",
            supported=self.client_capabilities.sampling is not None,
        )

        if isinstance(messages, str):
            messages = [SamplingMessage(role="user", content=TextContent(text=messages))]

        request = CreateMessageRequest(
            params=CreateMessageRequestParams(
                messages=[*messages],
                max_tokens=max_tokens,
                system_prompt=system_prompt,
                temperature=temperature,
                model_preferences=model_preferences,
            ),
        )

        match self.inputs.take(request, kind="sample", key=key):
            case CreateMessageResult() as result:
                return result
            case response:
                raise MCPError(INVALID_PARAMS, f"Expected a sampling result, got {type(response).__name__}")

    async def list_roots(self, *, key: str | None = None) -> list[Root]:
        self.require_input(
            "roots",
            supported=self.client_capabilities.roots is not None,
        )

        match self.inputs.take(ListRootsRequest(), kind="roots", key=key):
            case ListRootsResult(roots=roots):
                return roots
            case response:
                raise MCPError(INVALID_PARAMS, f"Expected a roots result, got {type(response).__name__}")

    # Using the server from a route, with the same hooks and handlers as over MCP.

    async def handle(self, request: Request[Any, Any]) -> Result:
        return await self.server.handle(request, self)

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        params = CallToolRequestParams(name=name, arguments=dict(arguments or {}))
        return cast("CallToolResult | InputRequiredResult", await self.handle(CallToolRequest(params=params)))

    async def read_resource(self, uri: str) -> ReadResourceResult:
        params = ReadResourceRequestParams(uri=uri)
        return cast("ReadResourceResult", await self.handle(ReadResourceRequest(params=params)))

    async def get_prompt(self, name: str, arguments: Mapping[str, str] | None = None) -> GetPromptResult:
        params = GetPromptRequestParams(name=name, arguments=dict(arguments or {}))
        return cast("GetPromptResult", await self.handle(GetPromptRequest(params=params)))


@dataclass(kw_only=True)
class MCPDeps:
    mcp: MCPContext = field(init=False, repr=False, compare=False)

    def bind_mcp_context(self, ctx: MCPContext) -> None:
        # works for frozen subclasses too.
        object.__setattr__(self, "mcp", ctx)

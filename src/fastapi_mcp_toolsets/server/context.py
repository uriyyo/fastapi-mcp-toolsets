from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, cast, get_args

from anyio.abc import ObjectSendStream
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    AudioContent,
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    ClientCapabilities,
    CreateMessageRequest,
    CreateMessageRequestParams,
    CreateMessageResult,
    CreateMessageResultWithTools,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitRequestURLParams,
    ElicitResult,
    GetPromptRequest,
    GetPromptRequestParams,
    GetPromptResult,
    ImageContent,
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
    Tool,
    ToolChoice,
    ToolResultContent,
    ToolUseContent,
)
from pydantic import TypeAdapter, ValidationError
from pydantic_ai import InstructionPart, RunContext, UserError
from pydantic_ai.capabilities.abstract import AbstractCapability
from pydantic_ai.tool_manager import ToolManager

from fastapi_mcp_toolsets._compat import ElicitationJsonSchema, validate_elicitation_schema
from fastapi_mcp_toolsets.features import mcp_tool
from fastapi_mcp_toolsets.protocol import MCPProtocolVersion, jsonrpc_notification
from fastapi_mcp_toolsets.tasks.backend import TaskBackend

from .inputs import (
    Elicitation,
    InputSession,
    InputUnavailableError,
    supports_form_elicitation,
    supports_url_elicitation,
)

if TYPE_CHECKING:
    from .server import MCPServer

__all__ = [
    "InstructionsResolver",
    "MCPContext",
    "MCPDeps",
    "MCPMessageStream",
]

type ToolChoiceMode = Literal["auto", "required", "none"]
type IncludeContext = Literal["none", "thisServer", "allServers"]
type SamplingBlock = TextContent | ImageContent | AudioContent | ToolUseContent | ToolResultContent


def as_blocks(content: SamplingBlock | Sequence[SamplingBlock]) -> list[SamplingBlock]:
    return [*content] if isinstance(content, Sequence) else [content]


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

    # Set while a task runs the request, instead of the request answering inline.
    task_id: str | None = None

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

    async def elicit_url(self, message: str, url: str, *, key: str | None = None) -> Elicitation[None]:
        """Send the user to a URL, e.g. to sign in or pay; answered once they are done there."""
        self.require_input(
            "URL elicitation",
            supported=supports_url_elicitation(self.client_capabilities),
        )

        # No `elicitationId`: the key of the input request identifies it, and the answer comes with the retry.
        request = ElicitRequest(params=ElicitRequestURLParams(message=message, url=url))

        match self.inputs.take(request, kind="elicit-url", key=key):
            case ElicitResult(action=action):
                return Elicitation(action=action)
            case response:
                raise MCPError(INVALID_PARAMS, f"Expected an elicitation result, got {type(response).__name__}")

    async def task_status(self, message: str) -> None:
        """Tell the client what the task is up to, in `statusMessage` while it polls; nothing outside a task."""
        match self.task_id, self.server.tasks:
            case str() as task_id, TaskBackend() as backend:
                await backend.set_status(task_id, message)

    async def sample(
        self,
        messages: str | Sequence[SamplingMessage],
        *,
        max_tokens: int,
        system_prompt: str | None = None,
        temperature: float | None = None,
        model_preferences: ModelPreferences | None = None,
        tools: Sequence[Tool] | None = None,
        tool_choice: ToolChoiceMode | None = None,
        include_context: IncludeContext | None = None,
        stop_sequences: Sequence[str] | None = None,
        key: str | None = None,
    ) -> CreateMessageResult | CreateMessageResultWithTools:
        """Ask the client's model for a completion; with `tools`, it may answer with a `ToolUseContent` to run."""
        sampling = self.client_capabilities.sampling
        self.require_input("sampling", supported=sampling is not None)

        if tools is not None:
            self.require_input("sampling with tools", supported=sampling is not None and sampling.tools is not None)

        if isinstance(messages, str):
            messages = [SamplingMessage(role="user", content=TextContent(text=messages))]

        request = CreateMessageRequest(
            params=CreateMessageRequestParams(
                messages=[*messages],
                max_tokens=max_tokens,
                system_prompt=system_prompt,
                temperature=temperature,
                model_preferences=model_preferences,
                tools=None if tools is None else [*tools],
                tool_choice=None if tool_choice is None else ToolChoice(mode=tool_choice),
                include_context=include_context,
                stop_sequences=None if stop_sequences is None else [*stop_sequences],
            ),
        )

        match self.inputs.take(request, kind="sample", key=key):
            case CreateMessageResult() | CreateMessageResultWithTools() as result:
                return result
            case response:
                raise MCPError(INVALID_PARAMS, f"Expected a sampling result, got {type(response).__name__}")

    async def sample_with_tools(
        self,
        messages: str | Sequence[SamplingMessage],
        *,
        max_tokens: int,
        tool_names: Sequence[str] | None = None,
        max_rounds: int = 10,
        key: str | None = None,
        **options: Any,
    ) -> CreateMessageResultWithTools:
        """Sample with this server's tools on offer, running each tool the model uses, until it answers in text.

        `tool_names` picks tools from the ones this request serves, all of them by default. Each round is one
        retry of the request, replayed from the start, so what runs before the loop must be repeatable.
        """
        offered = {tool_def.name: tool_def for tool_def in self.tool_manager.tool_defs}

        if tool_names is not None and (unknown := set(tool_names) - offered.keys()):
            raise UserError(f"Unknown tools to sample with: {sorted(unknown)}")

        tools = [
            mcp_tool(tool_def, protocol_version=self.protocol_version)
            for name, tool_def in offered.items()
            if tool_names is None or name in tool_names
        ]
        if isinstance(messages, str):
            messages = [SamplingMessage(role="user", content=TextContent(text=messages))]

        history = [*messages]

        for round_number in range(max_rounds):
            round_key = None if key is None else f"{key}-{round_number}"
            result = await self.sample(history, max_tokens=max_tokens, tools=tools, key=round_key, **options)

            if not isinstance(result, CreateMessageResultWithTools):
                return CreateMessageResultWithTools(
                    role=result.role, content=result.content, model=result.model, stop_reason=result.stop_reason
                )

            uses = [block for block in as_blocks(result.content) if isinstance(block, ToolUseContent)]

            if not uses:
                return result

            results = [await self.use_tool(use) for use in uses]
            history += [
                SamplingMessage(role="assistant", content=result.content),
                SamplingMessage(role="user", content=results),
            ]

        raise MCPError(INTERNAL_ERROR, f"The model kept calling tools for {max_rounds} rounds")

    async def use_tool(self, use: ToolUseContent) -> ToolResultContent:
        # Through the handler, so hooks, approvals and header checks apply as for a client's call.
        match await self.call_tool(use.name, use.input):
            case CallToolResult(content=content, structured_content=structured, is_error=is_error):
                return ToolResultContent(
                    tool_use_id=use.id,
                    content=content,
                    structured_content=structured,
                    is_error=is_error or None,
                )
            case other:
                raise MCPError(INTERNAL_ERROR, f"Tool {use.name!r} answered with {type(other).__name__} while sampling")

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

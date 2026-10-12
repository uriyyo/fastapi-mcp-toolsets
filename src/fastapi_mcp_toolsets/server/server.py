from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum
from types import TracebackType
from typing import Annotated, Any, Generic, Self, cast, overload

from fastapi import Depends, FastAPI
from fastapi import Request as StarletteRequest
from fastapi_injected import init_inject_scope, push_inject_scope, resolve
from mcp.server.mcpserver.resources.templates import DEFAULT_RESOURCE_SECURITY, ResourceSecurity
from mcp.server.request_state import RequestStateCodec
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSONRPC_VERSION,
    LOG_LEVEL_META_KEY,
    METHOD_NOT_FOUND,
    Annotations,
    ClientCapabilities,
    CompletionsCapability,
    Icon,
    Implementation,
    JSONRPCError,
    JSONRPCRequest,
    LoggingCapability,
    LoggingLevel,
    PromptsCapability,
    Request,
    RequestParams,
    ResourcesCapability,
    Result,
    ServerCapabilities,
    ToolsCapability,
)
from mcp_types.methods import parse_client_request
from pydantic import ValidationError
from pydantic.json_schema import GenerateJsonSchema
from pydantic_ai import (
    Agent,
    InstructionPart,
    RunContext,
    RunUsage,
    Tool,
    UserError,
)
from pydantic_ai.agent import AgentInstructions
from pydantic_ai.capabilities import AgentCapability
from pydantic_ai.capabilities.abstract import AbstractCapability, leaf_capabilities, select_workspace
from pydantic_ai.concurrency import AnyConcurrencyLimit, get_concurrency_context, normalize_to_limiter
from pydantic_ai.models import Model
from pydantic_ai.models.test import TestModel
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tool_manager import ToolManager
from pydantic_ai.tools import (
    ArgsValidatorFunc,
    DocstringFormat,
    GenerateToolJsonSchema,
    SystemPromptFunc,
    ToolFuncContext,
    ToolFuncEither,
    ToolFuncPlain,
    ToolParams,
    ToolPrepareFunc,
)
from pydantic_ai.toolsets import AgentToolset, ToolsetFunc
from typing_extensions import TypeForm

from fastapi_mcp_toolsets._compat import GraphAgentState, resolve_sourced_instructions
from fastapi_mcp_toolsets._typing import AgentDepsT
from fastapi_mcp_toolsets.features import Completer, MCPPrompts, MCPResources
from fastapi_mcp_toolsets.protocol import MCPProtocolVersion, MCPResponse, as_request_id, jsonrpc_error, jsonrpc_result

from .context import InstructionsResolver, MCPContext, MCPDeps, MCPMessageStream
from .handlers import MCPHandler, MCPHandlers, handle
from .inputs import InputRequired, RequestStateSealer, build_request_state_codec, input_required_result, read_inputs
from .sources import served_prompts, served_resources

__all__ = [
    "MCPServer",
    "ToolManagerScope",
]


DEFAULT_SERVER_NAME = "fastapi-mcp-toolsets"
DEFAULT_SERVER_VERSION = "0.1.0"
DEFAULT_REQUEST_STATE_MAX_AGE = timedelta(minutes=15)


class ToolManagerScope(StrEnum):
    # Capabilities, toolsets and tools are prepared for each request, with its deps.
    REQUEST = "request"
    # Prepared once for the app's lifespan, without deps, and shared by every request.
    APP = "app"


@dataclass(frozen=True, kw_only=True)
class PreparedRun(Generic[AgentDepsT]):  # noqa: UP046 - type parameters can't have defaults before 3.13
    capability: AbstractCapability[AgentDepsT]
    run_context: RunContext[AgentDepsT]
    tool_manager: ToolManager[AgentDepsT]
    instructions: InstructionsResolver[AgentDepsT]


class MCPServer(Generic[AgentDepsT]):  # noqa: UP046 - type parameters can't have defaults before 3.13
    def __init__(
        self,
        *,
        agent: Agent[AgentDepsT, Any] | None = None,
        tools: Sequence[Tool[AgentDepsT] | ToolFuncEither[AgentDepsT, ...]] = (),
        toolsets: Sequence[AgentToolset[AgentDepsT]] = (),
        capabilities: Sequence[AgentCapability[AgentDepsT]] = (),
        instructions: AgentInstructions[AgentDepsT] = None,
        deps_type: TypeForm[AgentDepsT] | None = None,
        retries: int | None = None,
        tool_timeout: float | None = None,
        validation_context: Any | Callable[[RunContext[AgentDepsT]], Any] = None,
        max_concurrency: AnyConcurrencyLimit = None,
        tool_manager_scope: ToolManagerScope = ToolManagerScope.REQUEST,
        page_size: int | None = None,
        request_state_keys: Sequence[str | bytes] | None = None,
        request_state_codec: RequestStateCodec | None = None,
        request_state_max_age: timedelta = DEFAULT_REQUEST_STATE_MAX_AGE,
        name: str | None = None,
        version: str = DEFAULT_SERVER_VERSION,
        title: str | None = None,
        description: str | None = None,
        website_url: str | None = None,
        icons: Sequence[Icon] | None = None,
    ) -> None:
        if agent is None:
            # No model request is ever made; `TestModel` only satisfies the run context.
            agent = Agent[AgentDepsT, Any](
                TestModel(),
                tools=tools,
                toolsets=toolsets,
                capabilities=capabilities,
                instructions=instructions,
                deps_type=cast("TypeForm[AgentDepsT]", deps_type if deps_type is not None else type(None)),
                retries=retries,
                tool_timeout=tool_timeout,
                validation_context=validation_context,
            )
        elif (
            tools
            or toolsets
            or capabilities
            or instructions is not None
            or deps_type is not None
            or retries is not None
            or tool_timeout is not None
            or validation_context is not None
        ):
            raise UserError("Register tools, toolsets, capabilities and instructions on the agent, not alongside it")

        self.agent = agent
        self.tool_manager_scope = tool_manager_scope
        self.page_size = page_size
        self.server_info = Implementation(
            name=name or self.agent.name or DEFAULT_SERVER_NAME,
            version=version,
            title=title,
            description=description,
            website_url=website_url,
            icons=list(icons) if icons is not None else None,
        )

        self._concurrency_limiter = normalize_to_limiter(max_concurrency, name=self.server_info.name)
        self._request_state_sealer = RequestStateSealer(
            codec=build_request_state_codec(request_state_keys, request_state_codec),
            max_age=request_state_max_age,
        )
        self._exit_stack: AsyncExitStack | None = None
        self._shared_run: PreparedRun[AgentDepsT] | None = None

        # Layered on the agent's capabilities per run, so they also serve an agent built elsewhere.
        self._mcp_resources = MCPResources[AgentDepsT]()
        self._mcp_prompts = MCPPrompts[AgentDepsT]()
        # Last, so the server's own handlers win over the agent's capabilities'.
        self._mcp_handlers = MCPHandlers[AgentDepsT]()
        self._mcp_capabilities: list[AbstractCapability[AgentDepsT]] = [
            self._mcp_resources,
            self._mcp_prompts,
            self._mcp_handlers,
        ]

    @overload
    def tool(self, func: ToolFuncContext[AgentDepsT, ToolParams], /) -> ToolFuncContext[AgentDepsT, ToolParams]: ...

    @overload
    def tool(
        self,
        /,
        *,
        name: str | None = None,
        description: str | None = None,
        retries: int | None = None,
        prepare: ToolPrepareFunc[AgentDepsT] | None = None,
        args_validator: ArgsValidatorFunc[AgentDepsT, ToolParams] | None = None,
        docstring_format: DocstringFormat | None = None,
        require_parameter_descriptions: bool | None = None,
        schema_generator: type[GenerateJsonSchema] | None = None,
        strict: bool | None = None,
        sequential: bool | None = None,
        requires_approval: bool | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float | None = None,
        defer_loading: bool | None = None,
        include_return_schema: bool | None = None,
    ) -> Callable[[ToolFuncContext[AgentDepsT, ToolParams]], ToolFuncContext[AgentDepsT, ToolParams]]: ...

    def tool(self, func: Any = None, /, **options: Any) -> Any:
        return self.agent.tool(func, **options)

    @overload
    def tool_plain(self, func: ToolFuncPlain[ToolParams], /) -> ToolFuncPlain[ToolParams]: ...

    @overload
    def tool_plain(
        self,
        /,
        *,
        name: str | None = None,
        description: str | None = None,
        retries: int | None = None,
        prepare: ToolPrepareFunc[AgentDepsT] | None = None,
        args_validator: ArgsValidatorFunc[AgentDepsT, ToolParams] | None = None,
        docstring_format: DocstringFormat | None = None,
        require_parameter_descriptions: bool | None = None,
        schema_generator: type[GenerateJsonSchema] | None = None,
        strict: bool | None = None,
        sequential: bool | None = None,
        requires_approval: bool | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float | None = None,
        defer_loading: bool | None = None,
        include_return_schema: bool | None = None,
    ) -> Callable[[ToolFuncPlain[ToolParams]], ToolFuncPlain[ToolParams]]: ...

    def tool_plain(self, func: Any = None, /, **options: Any) -> Any:
        return self.agent.tool_plain(func, **options)

    @overload
    def toolset(self, func: ToolsetFunc[AgentDepsT], /) -> ToolsetFunc[AgentDepsT]: ...

    @overload
    def toolset(
        self,
        /,
        *,
        per_run_step: bool = True,
        id: str | None = None,
    ) -> Callable[[ToolsetFunc[AgentDepsT]], ToolsetFunc[AgentDepsT]]: ...

    def toolset(self, func: Any = None, /, **options: Any) -> Any:
        return self.agent.toolset(func, **options)

    def resource[F: Callable[..., Any]](
        self,
        uri: str,
        /,
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
        """Serve a function's return value at `uri`; a `{var}` in it makes a template."""
        return self._mcp_resources.resource(
            uri,
            name=name,
            title=title,
            description=description,
            mime_type=mime_type,
            size=size,
            icons=icons,
            annotations=annotations,
            meta=meta,
            complete=complete,
            security=security,
            docstring_format=docstring_format,
            require_parameter_descriptions=require_parameter_descriptions,
            schema_generator=schema_generator,
        )

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

    def prompt(self, function: Any = None, /, **options: Any) -> Any:
        """Serve a function's return value as a prompt; its parameters are the prompt's arguments."""
        return self._mcp_prompts.prompt(function, **options)

    def instructions[F: SystemPromptFunc[Any]](self, function: F, /) -> F:
        return cast("F", self.agent.instructions(cast("Any", function)))

    def handler[H: MCPHandler](self, handler: H, /) -> H:
        """Handle the MCP request type(s) the first parameter is annotated with, over the built-in handler."""
        return self._mcp_handlers.handler(handler)

    @property
    def name(self) -> str:
        return self.server_info.name

    @property
    def version(self) -> str:
        return self.server_info.version

    @asynccontextmanager
    async def lifespan(self, app: FastAPI | None = None) -> AsyncGenerator[Self]:
        async with push_inject_scope(app=app), self:
            yield self

    async def __aenter__(self) -> Self:
        # Entered once per app, not per request: keeps e.g. `MCPToolset` connections open.
        async with AsyncExitStack() as stack:
            # The agent's model is never called, so it is not entered (nor inferred) along with the toolsets.
            with self.agent.override(model=TestModel()):
                await stack.enter_async_context(self.agent)

            if self.tool_manager_scope is ToolManagerScope.APP:
                # Deps from the scope `lifespan` binds to the app, released when that scope exits.
                run_context = self._build_run_context(await self._build_app_deps())
                self._shared_run = await stack.enter_async_context(self._prepare_run(run_context))

            self._exit_stack = stack.pop_all()

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        stack, self._exit_stack, self._shared_run = self._exit_stack, None, None

        if stack is None:
            return None

        return await stack.__aexit__(exc_type, exc_value, traceback)

    @property
    def _has_deps(self) -> bool:
        # `Agent`'s default, and the server's for no deps.
        return self.agent.deps_type is not object and self.agent.deps_type is not type(None)

    async def _build_deps(self) -> AgentDepsT:
        # Per request, in its inject scope: headers, the body and request-bound deps resolve.
        if not self._has_deps:
            return cast("AgentDepsT", None)

        return await resolve(self.agent.deps_type)

    async def _build_app_deps(self) -> AgentDepsT:
        # Once per app, in the scope `lifespan` binds: a dependency that needs a request fails at startup.
        if not self._has_deps:
            return cast("AgentDepsT", None)

        return await resolve(self.agent.deps_type)

    def _build_run_context(self, deps: AgentDepsT) -> RunContext[AgentDepsT]:
        model = self.agent.model

        return RunContext(
            deps=deps,
            agent=self.agent,
            model=model if isinstance(model, Model) else TestModel(),
            usage=RunUsage(),
            run_id=uuid.uuid4().hex,
        )

    def _install_capability(
        self,
        capability: AbstractCapability[AgentDepsT],
        run_context: RunContext[AgentDepsT],
        capabilities: dict[str, AbstractCapability[AgentDepsT]],
    ) -> None:
        # What hook dispatch and capability-owned toolsets read.
        run_context.root_capability = capability
        run_context.capabilities = capabilities
        run_context._run_capabilities_by_id = {  # noqa: SLF001
            cap.id: cap for cap in leaf_capabilities(capability) if cap.id is not None
        }
        capability._prepare_run_context(run_context)  # noqa: SLF001

    @asynccontextmanager
    async def _prepare_run(self, run_context: RunContext[AgentDepsT]) -> AsyncGenerator[PreparedRun[AgentDepsT]]:
        # The agent's own run setup, as in `Agent.iter`, with the server's capabilities as the run layer.
        base_capability, base_is_override = self.agent._base_run_capability()  # noqa: SLF001
        resolved = await self.agent._resolve_run_capabilities(  # noqa: SLF001
            run_context,
            base_capability=base_capability,
            extra_capabilities=self._mcp_capabilities,
            instrumentation_cap=None,
            # With no model to load them, their tools would silently stay hidden.
            inject_deferred_loader=False,
            base_is_override=base_is_override,
        )
        capability = resolved.run_capability
        self._install_capability(capability, run_context, resolved.capabilities)

        if deferred := [cap_id for cap_id, cap in resolved.capabilities.items() if cap.defer_loading is True]:
            raise UserError(f"Capabilities with defer_loading are not supported by this server: {deferred}")

        if workspace := select_workspace(capability, run_context, ref=None, run_layer=resolved.run_layer):
            run_context.workspace = capability._prepare_workspace(  # noqa: SLF001
                run_context, workspace, explicit=False
            )

        # `prepare_tools` runs inside any wrapper toolset, and no output toolset is added.
        toolset = self.agent._get_toolset(  # noqa: SLF001
            output_toolset=None,
            cap_toolsets=resolved.toolsets,
            run_capability=capability,
        )
        sourced_instructions = self.agent._get_instructions(cap_instructions=resolved.instructions)  # noqa: SLF001

        async def instructions(ctx: RunContext[AgentDepsT]) -> list[InstructionPart]:
            return await resolve_sourced_instructions(sourced_instructions, ctx)

        # Calls go through a `ToolManager`, so capability tool hooks run as in an agent run.
        async with await toolset.for_run(run_context) as prepared:
            tool_manager = ToolManager[AgentDepsT](
                prepared,
                root_capability=capability,
                default_max_retries=self.agent._resolve_tool_retries(),  # noqa: SLF001
            )

            yield PreparedRun(
                capability=capability,
                run_context=run_context,
                tool_manager=await tool_manager.for_run_step(run_context),
                instructions=instructions,
            )

    def _share_run(
        self, shared: PreparedRun[AgentDepsT], run_context: RunContext[AgentDepsT]
    ) -> PreparedRun[AgentDepsT]:
        # A fresh context, so retries and per-run state are not shared between requests.
        self._install_capability(shared.capability, run_context, shared.run_context.capabilities)
        run_context.workspace = shared.run_context.workspace

        tool_manager = replace(
            shared.tool_manager,
            ctx=run_context,
            failed_tools=set(),
            succeeded_tools=set(),
            availability_refused=set(),
        )
        run_context.tool_manager = tool_manager

        return replace(shared, run_context=run_context, tool_manager=tool_manager)

    @asynccontextmanager
    async def _request_run(self) -> AsyncGenerator[PreparedRun[AgentDepsT]]:
        run_context = self._build_run_context(await self._build_deps())

        match self.agent.validation_context:
            case Callable() as build_validation_context:
                run_context.validation_context = build_validation_context(run_context)
            case validation_context:
                run_context.validation_context = validation_context

        match self.tool_manager_scope, self._shared_run:
            case ToolManagerScope.REQUEST, _:
                async with self._prepare_run(run_context) as run:
                    yield run
            case ToolManagerScope.APP, None:
                raise UserError("The server must be entered first, e.g. by including its MCPRouter in the app")
            case ToolManagerScope.APP, shared:
                yield self._share_run(shared, run_context)

    @asynccontextmanager
    async def build_context(
        self,
        *,
        protocol_version: MCPProtocolVersion | None = None,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        headers: Mapping[str, str] | None = None,
        stream: MCPMessageStream | None = None,
    ) -> AsyncGenerator[MCPContext]:
        async with get_concurrency_context(self._concurrency_limiter, f"mcp:{self.name}"), self._request_run() as run:
            ctx = MCPContext(
                server=self,
                run_context=run.run_context,
                capability=run.capability,
                tool_manager=run.tool_manager,
                instructions=run.instructions,
                protocol_version=protocol_version or MCPProtocolVersion.latest(),
                client_capabilities=client_capabilities or ClientCapabilities(),
                client_info=client_info,
                headers=headers,
                stream=stream,
            )

            match run.run_context.deps:
                case MCPDeps() as deps:
                    deps.bind_mcp_context(ctx)

            yield ctx

    def _build_run_result(self, output: Any, ctx: MCPContext) -> AgentRunResult[Any]:
        # Run hooks expect an agent run result; there are no model messages to put in it.
        match ctx.run_context.run_id:
            case str() as run_id:
                state = GraphAgentState(usage=ctx.run_context.usage, run_id=run_id)
            case _:
                state = GraphAgentState(usage=ctx.run_context.usage)

        result = AgentRunResult(output=output, _state=state)

        # As `AgentRun.result` attaches it.
        result.__dict__["_workspace"] = ctx.run_context.workspace

        return result

    async def _within_run(self, ctx: MCPContext, body: Callable[[], Awaitable[Any]]) -> Any:
        capability = ctx.capability
        run_context = ctx.run_context

        async def handler() -> AgentRunResult[Any]:
            await capability.before_run(run_context)

            try:
                result = self._build_run_result(await body(), ctx)
            except Exception as e:  # noqa: BLE001
                # A recovering capability may return a bare output.
                match await capability.on_run_error(run_context, error=e):
                    case AgentRunResult() as recovered:
                        result = recovered
                    case recovered:
                        result = self._build_run_result(recovered, ctx)

            return await capability.after_run(run_context, result=result)

        match await capability.wrap_run(run_context, handler=handler):
            case AgentRunResult(output=output):
                return output
            case output:
                return output

    async def dispatch(self, body: dict[str, Any], ctx: MCPContext) -> MCPResponse:
        return await self._within_run(ctx, lambda: self._handle_message(body, ctx))

    async def handle(self, request: Request[Any, Any], ctx: MCPContext) -> Result:
        """Handle one MCP request in a run of its own, with the same hooks and handlers as the endpoint."""
        return await self._within_run(ctx, lambda: self._handle_request(request, ctx))

    # A yield dependency, so not a context manager: FastAPI drives the generator itself.
    async def context(
        self,
        request: StarletteRequest,
        _scope: Annotated[None, Depends(init_inject_scope)] = None,
    ) -> AsyncGenerator[MCPContext]:
        """A FastAPI dependency: the server's context for the request, to call its tools from a route."""
        async with self.build_context(headers=request.headers) as ctx:
            yield ctx

    async def _handle_message(self, body: dict[str, Any], ctx: MCPContext) -> MCPResponse:
        if "id" not in body:
            # Notifications need no reply, and with no session nothing to act on.
            return None

        try:
            message = JSONRPCRequest.model_validate(body)
        except ValidationError as e:
            return jsonrpc_error(
                INVALID_REQUEST,
                f"Invalid request: {e}",
                request_id=as_request_id(body.get("id")),
            )

        if message.method == "initialize" and ctx.protocol_version.features.handshake:
            ctx.protocol_version = MCPProtocolVersion.negotiate_handshake((message.params or {}).get("protocolVersion"))

        try:
            # A method the version lacks is a `KeyError`.
            request = parse_client_request(message.method, ctx.protocol_version.version, message.params)
        except KeyError:
            return jsonrpc_error(
                METHOD_NOT_FOUND,
                f"Unknown method: {message.method}",
                request_id=message.id,
            )
        except ValidationError as e:
            return jsonrpc_error(INVALID_PARAMS, f"Invalid params: {e}", request_id=message.id)

        match request.params:
            case RequestParams(meta={"progress_token": progress_token}):
                ctx.progress_token = progress_token

        try:
            ctx.log_level = self._request_log_level(request, ctx)
            result = await self._handle_request(request, ctx)
        except MCPError as e:
            return JSONRPCError(jsonrpc=JSONRPC_VERSION, id=message.id, error=e.error)

        return jsonrpc_result(
            result,
            method=message.method,
            protocol_version=ctx.protocol_version,
            request_id=message.id,
            server_info=self.server_info,
        )

    def _request_log_level(self, request: Request[Any, Any], ctx: MCPContext) -> LoggingLevel | None:
        if not ctx.protocol_version.features.log_level_in_meta:
            # No session to keep `logging/setLevel` in, so everything is sent.
            return "debug"

        # No level in `_meta` means no log messages.
        meta = request.params.meta if isinstance(request.params, RequestParams) else None

        return (meta or {}).get(LOG_LEVEL_META_KEY)

    async def _handle_request(self, request: Request[Any, Any], ctx: MCPContext) -> Result:
        if ctx.protocol_version.features.input_required:
            ctx.inputs = read_inputs(request, sealer=self._request_state_sealer)

        try:
            return await handle(request, ctx)
        except InputRequired as e:
            return input_required_result(e, request, ctx.inputs, sealer=self._request_state_sealer)

    def capabilities(self, ctx: MCPContext) -> ServerCapabilities:
        # Worked out per request, as the decorators can add to the server after it is built.
        resources = served_resources(ctx.capability)
        prompts = served_prompts(ctx.capability)
        completable = any(resource.complete for resource in resources) or any(
            prompt.complete for prompt in prompts.values()
        )

        # No `listChanged` or `subscribe`: there is no stream to send them on.
        return ServerCapabilities(
            tools=ToolsCapability(),
            logging=LoggingCapability(),
            resources=ResourcesCapability() if resources else None,
            prompts=PromptsCapability() if prompts else None,
            completions=CompletionsCapability() if completable else None,
        )

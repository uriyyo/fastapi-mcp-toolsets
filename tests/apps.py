# The apps the tests serve: small, one feature each.
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

import anyio
import anyio.lowlevel
from fastapi import FastAPI, Header
from fastapi_injected import DepFactory
from mcp_types import TextContent
from pydantic import BaseModel
from pydantic_ai import RunContext

from fastapi_mcp_toolsets import (
    AppUI,
    InProcessTaskBackend,
    MCPDeps,
    MCPRouter,
    MCPServer,
    TaskMode,
    TaskSupport,
    ToolAnnotation,
    mcp_header,
    tool_metadata,
)


async def player_name(x_player_name: Annotated[str, Header()] = "Anne") -> str:
    return x_player_name


@dataclass
class Deps(MCPDeps):
    player_name: DepFactory[str, player_name]


class Recipient(BaseModel):
    email: str


def build_server(**kwargs: object) -> MCPServer[Deps]:
    server = MCPServer(deps_type=Deps, name="tests", instructions="Be nice.", **kwargs)  # type: ignore[arg-type]

    @server.tool_plain(metadata=tool_metadata(ToolAnnotation(title="Add", read_only_hint=True)))
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @server.tool
    def whoami(ctx: RunContext[Deps]) -> str:
        """The player's name, from a header."""
        return ctx.deps.player_name

    @server.tool
    async def progress(ctx: RunContext[Deps], steps: int) -> str:
        """Reports progress while it runs."""
        for step in range(steps):
            await ctx.deps.mcp.report_progress(step + 1, steps)
            await anyio.lowlevel.checkpoint()

        return "done"

    @server.tool
    async def ask(ctx: RunContext[Deps]) -> str:
        """Asks the user for an email."""
        recipient = await ctx.deps.mcp.elicit("Email?", Recipient)
        return recipient.data.email if recipient.accepted and recipient.data else "nobody"

    add_task_tools(server)
    add_header_tools(server)
    add_sampling_tools(server)

    return server


def add_task_tools(server: MCPServer[Deps]) -> None:
    @server.tool_plain(metadata=tool_metadata(TaskSupport()))
    async def slow(seconds: float) -> str:
        """Takes a while."""
        await anyio.sleep(seconds)
        return "slow done"

    @server.tool(metadata=tool_metadata(TaskSupport(ttl=timedelta(minutes=1), poll_interval=timedelta(seconds=1))))
    async def status_report(ctx: RunContext[Deps]) -> str:
        """Says what it is doing, then waits to be observed."""
        await ctx.deps.mcp.task_status("halfway")
        await anyio.sleep(30)
        return "never"

    @server.tool
    async def sign_in(ctx: RunContext[Deps]) -> str:
        """Sends the user to a sign-in page."""
        done = await ctx.deps.mcp.elicit_url("Sign in first", "https://example.com/login", key="login")
        return "signed in" if done.accepted else done.action

    @server.tool(metadata=tool_metadata(TaskSupport(mode=TaskMode.REQUIRED)))
    async def slow_ask(ctx: RunContext[Deps]) -> str:
        """Asks on the way, only as a task."""
        recipient = await ctx.deps.mcp.elicit("Email?", Recipient)
        return recipient.data.email if recipient.accepted and recipient.data else "nobody"


def add_sampling_tools(server: MCPServer[Deps]) -> None:
    @server.tool
    async def solve(ctx: RunContext[Deps], question: str) -> str:
        """Asks the client's model, letting it use the add tool."""
        answer = await ctx.deps.mcp.sample_with_tools(question, max_tokens=50, tool_names=["add"])
        return answer.content.text if isinstance(answer.content, TextContent) else "?"


def add_header_tools(server: MCPServer[Deps]) -> None:
    @server.tool_plain
    def tenant_name(tenant: Annotated[str, mcp_header("Tenant")], suffix: str = "") -> str:
        """The tenant, routed on by a header."""
        return tenant + suffix


def ui_server() -> MCPServer[Deps]:
    server = MCPServer(
        deps_type=Deps,
        name="ui",
        experimental={"x-feature": {}},
        extensions={"com.example/ext": {"level": 1}},
    )

    @server.tool_plain(metadata=tool_metadata(AppUI(resource_uri="ui://clock/app.html", visibility=["app"])))
    def get_time() -> str:
        """The time."""
        return "noon"

    @server.resource("ui://clock/app.html")
    def clock_page() -> str:
        return "<html>clock</html>"

    return server


def ui_app() -> FastAPI:
    app = FastAPI()
    app.include_router(MCPRouter(ui_server()))
    return app


def build_app(**kwargs: object) -> FastAPI:
    app = FastAPI()
    app.include_router(MCPRouter(build_server(**kwargs)))
    return app


def tasks_app() -> FastAPI:
    return build_app(tasks=InProcessTaskBackend(poll_interval=timedelta(milliseconds=10)))

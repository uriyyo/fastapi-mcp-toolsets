# A research gateway: remote MCP servers and local tools behind one endpoint, with capabilities and hooks
# deciding who sees what, limiting usage, auditing calls and softening remote failures.
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Header
from fastapi_injected import DepFactory
from pydantic_ai import RunContext, ToolCallPart, ToolDefinition, ToolFailed
from pydantic_ai.capabilities import (
    MCP,
    AbstractCapability,
    Hooks,
    PrefixTools,
    ValidatedToolArgs,
    WrapToolExecuteHandler,
)
from pydantic_ai.mcp import MCPToolset

from fastapi_mcp_toolsets import MCPAdapter, MCPDeps, MCPRouter, UserMessage

logger = logging.getLogger("gateway")

type Role = Literal["viewer", "editor"]


@dataclass(frozen=True)
class User:
    name: str
    role: Role


async def current_user(
    x_user: Annotated[str, Header()] = "guest",
    x_role: Annotated[Role, Header()] = "viewer",
) -> User:
    return User(name=x_user, role=x_role)


@dataclass
class Deps(MCPDeps):
    user: DepFactory[User, current_user]


# Remote servers: the `MCP` capability connects to one by URL, and `MCPToolset` is the same as a toolset.
deepwiki = PrefixTools(MCP("https://mcp.deepwiki.com/mcp"), prefix="deepwiki")
pydantic_docs = (
    MCPToolset("https://gitmcp.io/pydantic/pydantic-ai")
    # Not an open proxy: the gateway only serves the repository's own docs and code.
    .filtered(lambda _ctx, tool_def: tool_def.name != "fetch_generic_url_content")
    .prefixed("pydantic_ai")
)

REMOTE_PREFIXES = ("deepwiki_", "pydantic_ai_")


def is_remote(tool_name: str) -> bool:
    return tool_name.startswith(REMOTE_PREFIXES)


hooks = Hooks[Deps]()


@hooks.on.prepare_tools
async def hide_editor_tools(ctx: RunContext[Deps], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
    # Viewers don't even see the tools that change things.
    if ctx.deps.user.role == "editor":
        return tool_defs

    return [tool_def for tool_def in tool_defs if not (tool_def.metadata or {}).get("editors_only")]


@hooks.on.tool_execute
async def audit(
    ctx: RunContext[Deps],
    *,
    call: ToolCallPart,
    tool_def: ToolDefinition,
    args: ValidatedToolArgs,
    handler: WrapToolExecuteHandler,
) -> Any:
    started = time.perf_counter()

    try:
        return await handler(args)
    finally:
        logger.info("%s called %s in %.2fs", ctx.deps.user.name, call.tool_name, time.perf_counter() - started)


@hooks.on.tool_execute_error
async def remote_unavailable(
    ctx: RunContext[Deps],
    *,
    call: ToolCallPart,
    tool_def: ToolDefinition,
    args: ValidatedToolArgs,
    error: Exception,
) -> Any:
    # A remote server being down is an answer the client can work with, not a crash.
    if is_remote(call.tool_name) and not isinstance(error, ToolFailed):
        raise ToolFailed(f"{call.tool_name} is unavailable right now, try again later") from error

    raise error


@dataclass
class Quota(AbstractCapability[Deps]):
    """Caps how many remote calls each user makes per minute."""

    calls_per_minute: int
    calls: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    async def before_tool_execute(
        self,
        ctx: RunContext[Deps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
    ) -> ValidatedToolArgs:
        if not is_remote(call.tool_name):
            return args

        now = time.monotonic()
        window = self.calls[ctx.deps.user.name]

        while window and now - window[0] > 60:  # noqa: PLR2004
            window.popleft()

        if len(window) >= self.calls_per_minute:
            raise ToolFailed(f"{ctx.deps.user.name} has used up {self.calls_per_minute} remote calls this minute")

        window.append(now)
        return args


adapter = MCPAdapter(
    deps_type=Deps,
    name="research-gateway",
    title="Research gateway",
    toolsets=[pydantic_docs],
    capabilities=[deepwiki, hooks, Quota(calls_per_minute=10)],
    instructions=[
        (
            "Answer questions about GitHub repositories with the deepwiki tools, and about Pydantic AI "
            "with the pydantic_ai tools. Save what's worth keeping as a note."
        ),
        lambda ctx: f"You're helping {ctx.deps.user.name}, who is a {ctx.deps.user.role}.",
    ],
    # Applies to the gateway's own tools; remote servers have timeouts of their own.
    tool_timeout=10,
    max_concurrency=20,
    request_state_keys=[key] if (key := os.environ.get("MCP_REQUEST_STATE_KEY")) else None,
)

NOTES: dict[str, dict[str, str]] = defaultdict(dict)


@adapter.tool(requires_approval=True, metadata={"editors_only": True})
async def save_note(ctx: RunContext[Deps], title: str, text: str) -> str:
    """Save a note, after the user approves it."""
    NOTES[ctx.deps.user.name][title] = text
    return f"Saved {title!r}"


@adapter.tool
async def list_notes(ctx: RunContext[Deps]) -> list[str]:
    """List the titles of the user's notes."""
    return sorted(NOTES[ctx.deps.user.name])


def note_titles(ctx: RunContext[Deps], value: str, _arguments: object) -> list[str]:
    return [title for title in NOTES[ctx.deps.user.name] if title.startswith(value)]


@adapter.resource("notes://{title}", mime_type="text/markdown", complete={"title": note_titles})
def note(ctx: RunContext[Deps], title: str) -> str:
    """A saved note."""
    return NOTES[ctx.deps.user.name].get(title, f"No note titled {title!r}")


@adapter.prompt(title="Research a repository")
def research(repo: str, question: str) -> UserMessage:
    """Ask a question about a GitHub repository.

    Args:
        repo: The repository, as `owner/name`.
        question: What to find out.
    """
    return UserMessage(f"Using the deepwiki tools on {repo}: {question} Save the answer as a note.")


app = FastAPI()
app.include_router(MCPRouter(adapter))


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, port=8000)

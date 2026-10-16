# fastapi-mcp-toolsets

Serve [Pydantic AI](https://ai.pydantic.dev/) tools, toolsets, agents and capabilities as an
[MCP](https://modelcontextprotocol.io/) server from a FastAPI app.

`MCPServer` mirrors `Agent`: the same decorators, the same `RunContext[Deps]`, the same toolsets and capabilities,
with deps resolved per request from headers, settings and `yield` dependencies the way FastAPI resolves them.
`MCPRouter` mounts it at a path next to the rest of your app, behind the same middleware and dependencies.

The server is stateless by design: one POST carries one request and its whole answer. That is the Streamable HTTP
transport the current protocol asks for, it scales across workers, and nothing needs a session store.

## Install

```sh
uv add fastapi-mcp-toolsets
uv add "fastapi-mcp-toolsets[stdio]"   # the fastapi-mcp-stdio command, for clients that launch a local server
```

## Quick start

```python
import random
from dataclasses import dataclass
from typing import Annotated

from fastapi import FastAPI, Header
from fastapi_injected import DepFactory
from pydantic_ai import RunContext

from fastapi_mcp_toolsets import MCPRouter, MCPServer, ToolAnnotation, tool_metadata


async def player_name(x_player_name: Annotated[str, Header()] = "Anne") -> str:
    return x_player_name


@dataclass
class Deps:
    # Resolved per request, like a FastAPI dependency.
    player_name: DepFactory[str, player_name]


server = MCPServer(deps_type=Deps, name="dice-game", instructions="Roll the die and tell the player how it went.")


@server.tool_plain(metadata=tool_metadata(ToolAnnotation(title="Roll dice", read_only_hint=True)))
def roll_dice() -> str:
    """Roll a six-sided die."""
    return str(random.randint(1, 6))


@server.tool
def get_player_name(ctx: RunContext[Deps]) -> str:
    """The player's name."""
    return ctx.deps.player_name


app = FastAPI()
app.include_router(MCPRouter(server))
```

Run it with uvicorn and point any MCP client at `http://localhost:8000/mcp`. Every example in
[`examples/`](examples/README.md) is a complete app like this one, a feature each.

## What a server can do

- **Tools** from `@server.tool`, `@server.tool_plain`, `@server.toolset`, or an existing `Agent` passed as
  `MCPServer(agent=...)`, with its instructions served too. Structured output, images, resource links, `_meta`,
  retries and tool hooks all work as in an agent run, because calls go through Pydantic AI's own tool manager.
- **Resources and prompts** from `@server.resource("docs://{name}")` and `@server.prompt`, with argument
  completion.
- **Deps per request**: anything `fastapi-injected` can build, so headers, settings and `yield` dependencies that
  close when the request is answered. `ToolManagerScope.APP` prepares toolsets once for the app instead, for
  things like remote MCP connections.
- **Progress and log notifications** streamed over SSE while a tool runs.
- **Elicitation, sampling and roots**: a tool calls `ctx.deps.mcp.elicit(...)`, `elicit_url(...)`, `sample(...)`,
  `sample_with_tools(...)` or `list_roots()`. The call is answered with what the server needs and the client
  retries it with the answers, so nothing waits on a connection. Tools that need the user's approval work the same
  way.
- **Tasks**: mark a tool with `TaskSupport()` and give the server a `TaskBackend`, and a client that can poll gets
  a task id back instead of waiting. The tool reports progress with `ctx.deps.mcp.task_status(...)`.
  `InProcessTaskBackend` runs tasks in the process; the abstract backend is the seam for a shared store.
- **MCP Apps**: `AppUI(resource_uri="ui://...")` on a tool and a `ui://` resource with the page.
- **Header-routed arguments**: `Annotated[str, mcp_header("Tenant")]` mirrors an argument into a header a proxy
  can route on.
- **Both protocol eras**: the 2025 handshake versions and the 2026 per-request envelope, negotiated per request.
- **The same tools over REST**: `server.context` is a FastAPI dependency that calls them as a client would.

Per-tool configuration shares one API. `tool_metadata(ToolAnnotation(...), TaskSupport(...), AppUI(...))` builds
the metadata, each piece is a `ToolConfig`, and `ToolAnnotation.of(tool_def)` reads it back.

## Over stdio

Clients such as Claude Desktop launch a local server and talk to it over stdin and stdout. The bridge posts each
line to the app's endpoint in-process, so the same middleware, dependencies and lifespan apply:

```sh
fastapi-mcp-stdio myapp:app -H "X-Player-Name: Bob"
```

or from code, `MCPStdio(app, headers={...}).run()`.

## What it leaves out

No sessions, no server-initiated stream, no resumability and no `list_changed` notifications: those pin a client
to a process. Subscriptions are refused with a message that says so. Anything the protocol offers per request is
in.

## Development

```sh
uv sync --all-extras
uv run pytest
uv run ruff check && uv run ty check src
```

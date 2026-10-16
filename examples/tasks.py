# Long-running tools answered with a task to poll, for clients that declare the tasks extension.
import anyio
from fastapi import FastAPI
from pydantic import BaseModel
from pydantic_ai import RunContext

from fastapi_mcp_toolsets import (
    InProcessTaskBackend,
    MCPDeps,
    MCPRouter,
    MCPServer,
    TaskMode,
    TaskSupport,
    ToolAnnotation,
    tool_metadata,
)

# Tasks run in this process; another backend can keep them in a store that workers share.
server = MCPServer(deps_type=MCPDeps, name="reports", tasks=InProcessTaskBackend())


@server.tool_plain(metadata=tool_metadata(TaskSupport(), ToolAnnotation(title="Build report", read_only_hint=True)))
async def build_report(pages: int) -> str:
    """Build a report; takes a while."""
    await anyio.sleep(0.2 * pages)
    return f"Report with {pages} pages"


class Recipient(BaseModel):
    email: str


@server.tool(metadata=tool_metadata(TaskSupport(mode=TaskMode.REQUIRED)))
async def send_report(ctx: RunContext[MCPDeps]) -> str:
    """Send the report; asks who to send it to, so it only runs as a task."""
    await anyio.sleep(0.2)
    recipient = await ctx.deps.mcp.elicit("Who should receive the report?", Recipient)

    if not recipient.accepted or recipient.data is None:
        return "Not sent"

    return f"Sent to {recipient.data.email}"


app = FastAPI()
app.include_router(MCPRouter(server))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

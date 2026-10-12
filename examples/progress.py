# Logging and progress notifications, sent while the tool runs when the client accepts SSE.
import anyio
from fastapi import FastAPI
from pydantic_ai import RunContext

from fastapi_mcp_toolsets import MCPDeps, MCPRouter, MCPServer

server = MCPServer(deps_type=MCPDeps, name="downloads")


@server.tool
async def download(ctx: RunContext[MCPDeps], files: list[str]) -> str:
    """Download the given files."""
    await ctx.deps.mcp.log("info", f"downloading {len(files)} files", logger="download")

    for done, name in enumerate(files, start=1):
        await anyio.sleep(0.5)
        await ctx.deps.mcp.report_progress(done, len(files), f"downloaded {name}")
        await ctx.deps.mcp.log("debug", {"file": name}, logger="download")

    return f"Downloaded {len(files)} files"


app = FastAPI()
app.include_router(MCPRouter(server))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

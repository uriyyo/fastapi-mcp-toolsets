# Asking the client which filesystem roots it lets the server work in.
from fastapi import FastAPI
from pydantic_ai import RunContext

from fastapi_mcp_toolsets import MCPAdapter, MCPDeps, MCPRouter

adapter = MCPAdapter(deps_type=MCPDeps, name="workspace")


@adapter.tool
async def list_projects(ctx: RunContext[MCPDeps]) -> list[str]:
    """List the projects the client has opened."""
    return [root.name or str(root.uri) for root in await ctx.deps.mcp.list_roots()]


app = FastAPI()
app.include_router(MCPRouter(adapter))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

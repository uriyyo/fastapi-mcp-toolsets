# Asking the client's model for a completion, instead of calling one from the server.
from fastapi import FastAPI
from mcp_types import TextContent
from pydantic_ai import RunContext

from fastapi_mcp_toolsets import MCPDeps, MCPRouter, MCPServer

server = MCPServer(deps_type=MCPDeps, name="writer")


@server.tool
async def summarize(ctx: RunContext[MCPDeps], text: str) -> str:
    """Summarize the given text in one sentence."""
    result = await ctx.deps.mcp.sample(
        f"Summarize in one sentence:\n\n{text}",
        max_tokens=100,
        system_prompt="You are a concise editor.",
    )

    match result.content:
        case TextContent(text=summary):
            return summary
        case _:
            return "The client answered with something other than text."


app = FastAPI()
app.include_router(MCPRouter(server))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

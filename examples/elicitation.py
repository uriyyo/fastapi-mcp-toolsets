# Asking the user for input mid-call; at 2026-07-28 the call is answered with the question and retried.
from dataclasses import dataclass
from typing import Literal

from fastapi import FastAPI
from pydantic_ai import RunContext

from fastapi_mcp_toolsets import MCPAdapter, MCPDeps, MCPRouter

adapter = MCPAdapter(deps_type=MCPDeps, name="pizza")


@dataclass
class Pizza:
    size: Literal["small", "medium", "large"]
    extra_cheese: bool = False


@adapter.tool
async def order_pizza(ctx: RunContext[MCPDeps], address: str) -> str:
    """Order a pizza, asking the user which one."""
    choice = await ctx.deps.mcp.elicit("Which pizza would you like?", Pizza)

    if choice.data is None:
        return f"No pizza ordered ({choice.action})."

    cheese = " with extra cheese" if choice.data.extra_cheese else ""
    return f"A {choice.data.size} pizza{cheese} is on its way to {address}."


app = FastAPI()
app.include_router(MCPRouter(adapter))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

# The same tools over MCP and over plain REST routes: `server.context` is a FastAPI dependency
# that calls them as an MCP client would, with the request's deps, hooks and handlers.
import random
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, Header
from fastapi_injected import DepFactory
from pydantic_ai import RunContext

from fastapi_mcp_toolsets import MCPContext, MCPRouter, MCPServer


async def player_name(x_player_name: Annotated[str, Header()] = "Anne") -> str:
    return x_player_name


@dataclass
class Deps:
    player_name: DepFactory[str, player_name]


server = MCPServer(deps_type=Deps, name="dice-game")


@server.tool
def roll_dice(ctx: RunContext[Deps]) -> str:
    """Roll a six-sided die for the player."""
    return f"{ctx.deps.player_name} rolled {random.randint(1, 6)}"  # noqa: S311


@server.resource("game://rules", mime_type="text/plain")
def rules() -> str:
    """How the game is played."""
    return "Guess the number, then roll."


app = FastAPI()
app.include_router(MCPRouter(server))


# The same tool from a REST route, with the same deps resolved from the same request.
@app.post("/roll")
async def roll(ctx: Annotated[MCPContext, Depends(server.context)]) -> dict[str, str | None]:
    result = await ctx.call_tool("roll_dice")
    return {"result": result.content[0].text if result.content else None}


@app.get("/rules")
async def read_rules(ctx: Annotated[MCPContext, Depends(server.context)]) -> str:
    result = await ctx.read_resource("game://rules")
    return result.contents[0].text


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

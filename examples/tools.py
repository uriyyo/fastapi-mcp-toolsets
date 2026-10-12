# The dice game from https://ai.pydantic.dev/tools/, registered on the server itself.
import random
from dataclasses import dataclass
from typing import Annotated

from fastapi import FastAPI, Header
from fastapi_injected import DepFactory
from pydantic_ai import RunContext

from fastapi_mcp_toolsets import MCPRouter, MCPServer, ToolAnnotation


async def player_name(x_player_name: Annotated[str, Header()] = "Anne") -> str:
    return x_player_name


@dataclass
class Deps:
    # Resolved per request, like a FastAPI dependency.
    player_name: DepFactory[str, player_name]


server = MCPServer(
    deps_type=Deps,
    name="dice-game",
    # Served to the client from `initialize` and `server/discover`.
    instructions=(
        "You're a dice game, you should roll the die and see if the number "
        "you get back matches the user's guess. If so, tell them they're a winner. "
        "Use the player's name in the response."
    ),
)


@server.tool_plain(metadata=ToolAnnotation(title="Roll dice", read_only_hint=True).as_metadata())
def roll_dice() -> str:
    """Roll a six-sided die and return the result."""
    return str(random.randint(1, 6))  # noqa: S311


@server.tool
def get_player_name(ctx: RunContext[Deps]) -> str:
    """Get the player's name."""
    return ctx.deps.player_name


app = FastAPI()
app.include_router(MCPRouter(server))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

# Resources and resource templates, with completion of a template's variables.
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi_injected import Dep
from pydantic_ai import RunContext

from fastapi_mcp_toolsets import MCPRouter, MCPServer

USERS = {"1": "Ada Lovelace", "2": "Alan Turing", "42": "Grace Hopper"}


@dataclass
class Settings:
    greeting: str = "Hello"


@dataclass
class Deps:
    settings: Dep[Settings]


server = MCPServer(deps_type=Deps, name="users")


@server.resource("config://settings", mime_type="application/json")
def settings(ctx: RunContext[Deps]) -> Settings:
    """The server's settings."""
    return ctx.deps.settings


def user_ids(_ctx: RunContext[Deps], value: str, _arguments: object) -> list[str]:
    return [user_id for user_id in USERS if user_id.startswith(value)]


# `{user_id}` makes it a template; its variables are the function's parameters.
@server.resource("users://{user_id}/profile", complete={"user_id": user_ids})
def profile(user_id: str) -> str:
    """A user's profile."""
    return f"{USERS.get(user_id, 'Unknown')} (#{user_id})"


app = FastAPI()
app.include_router(MCPRouter(server))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

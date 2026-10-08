# The deps from https://ai.pydantic.dev/dependencies/, resolved per request with fastapi-injected.
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

import httpx2
from fastapi import FastAPI, Header
from fastapi_injected import Dep, DepFactory
from pydantic_ai import RunContext

from fastapi_mcp_toolsets import MCPAdapter, MCPRouter


@dataclass
class Settings:
    joke_api_url: str = "https://icanhazdadjoke.com/"


async def api_key(x_api_key: Annotated[str, Header()]) -> str:
    return x_api_key


async def http_client() -> AsyncIterator[httpx2.AsyncClient]:
    # Closed once the request is answered.
    async with httpx2.AsyncClient() as client:
        yield client


@dataclass
class MyDeps:
    settings: Dep[Settings]
    api_key: DepFactory[str, api_key]
    http_client: DepFactory[httpx2.AsyncClient, http_client]


adapter = MCPAdapter(deps_type=MyDeps, name="jokes")


@adapter.tool
async def get_joke(ctx: RunContext[MyDeps]) -> str:
    """Fetch a joke."""
    response = await ctx.deps.http_client.get(
        ctx.deps.settings.joke_api_url,
        headers={"Accept": "text/plain", "Authorization": f"Bearer {ctx.deps.api_key}"},
    )
    response.raise_for_status()
    return response.text


app = FastAPI()
app.include_router(MCPRouter(adapter))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

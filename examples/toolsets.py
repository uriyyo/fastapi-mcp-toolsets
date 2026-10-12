# The toolsets from https://ai.pydantic.dev/toolsets/, served side by side and prepared once per app.
from dataclasses import dataclass
from datetime import datetime

from fastapi import FastAPI
from fastapi_injected import Dep
from pydantic_ai import AbstractToolset, FunctionToolset, RunContext

from fastapi_mcp_toolsets import MCPRouter, MCPServer, ToolManagerScope

weather_toolset = FunctionToolset()


@weather_toolset.tool_plain
def temperature_celsius(city: str) -> float:
    return 21.0


@weather_toolset.tool_plain
def temperature_fahrenheit(city: str) -> float:
    return 69.8


@weather_toolset.tool
def conditions(ctx: RunContext, city: str) -> str:
    if ctx.run_step % 2 == 0:
        return "It's sunny"

    return "It's raining"


datetime_toolset = FunctionToolset()
datetime_toolset.add_function(lambda: datetime.now().astimezone(), name="now")


@dataclass
class Settings:
    fahrenheit: bool = False


@dataclass
class Deps:
    # Needs no request, so it resolves at startup too.
    settings: Dep[Settings]


server = MCPServer(
    deps_type=Deps,
    toolsets=[datetime_toolset],
    name="weather",
    # Prepared once for the app, with deps built outside any request.
    tool_manager_scope=ToolManagerScope.APP,
)


@server.toolset
def weather(ctx: RunContext[Deps]) -> AbstractToolset[Deps]:
    unwanted = "temperature_celsius" if ctx.deps.settings.fahrenheit else "temperature_fahrenheit"
    return weather_toolset.filtered(lambda _ctx, tool_def: tool_def.name != unwanted)


app = FastAPI()
app.include_router(MCPRouter(server))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

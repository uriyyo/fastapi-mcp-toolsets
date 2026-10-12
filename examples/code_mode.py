# Code mode from https://ai.pydantic.dev/harness/code-mode/: the tools are served as one `run_code` tool.
from typing import TypedDict

from fastapi import FastAPI
from pydantic_ai_harness import CodeMode

from fastapi_mcp_toolsets import MCPRouter, MCPServer

# Per-request scope: the sandbox keeps REPL state per run, which `ToolManagerScope.APP` would share.
server = MCPServer(capabilities=[CodeMode()], name="weather-code")


# The sandbox type-checks code against the tools' signatures, so precise types help.
class Weather(TypedDict):
    city: str
    temp_f: int
    condition: str


@server.tool_plain
def get_weather(city: str) -> Weather:
    """Get current weather for a city."""
    return Weather(city=city, temp_f=72, condition="sunny")


@server.tool_plain
def get_forecast(city: str, days: int) -> list[int]:
    """Get the forecast high temperatures, in Fahrenheit, for the next days."""
    return [70 + day for day in range(days)]


app = FastAPI()
app.include_router(MCPRouter(server))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

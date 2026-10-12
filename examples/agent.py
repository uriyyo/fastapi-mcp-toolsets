# Serving an existing agent: its tools, toolsets, capabilities and instructions, with resources and
# prompts added on the server. The agent's model is never called.
from datetime import datetime

from fastapi import FastAPI
from pydantic_ai import Agent, RunContext

from fastapi_mcp_toolsets import MCPRouter, MCPServer, UserMessage

agent = Agent(
    "openai:gpt-5",
    name="assistant",
    instructions="You're a helpful assistant that knows the time.",
    # The provider is only needed to run the agent, not to serve it.
    defer_model_check=True,
)


@agent.tool_plain
def now() -> str:
    """The current time, as ISO 8601."""
    return datetime.now().astimezone().isoformat()


@agent.tool
def whoami(ctx: RunContext[None]) -> str:
    """The name of the agent answering."""
    return ctx.agent.name if ctx.agent is not None and ctx.agent.name else "unknown"


# Served under the agent's name; tools and instructions come from the agent.
server = MCPServer(agent=agent)


@server.resource("agent://model", mime_type="text/plain")
def model() -> str:
    """The model the agent runs on when used as an agent."""
    return str(agent.model)


@server.prompt
def ask_time() -> UserMessage:
    """Ask what time it is."""
    return UserMessage("What time is it right now?")


app = FastAPI()
app.include_router(MCPRouter(server))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

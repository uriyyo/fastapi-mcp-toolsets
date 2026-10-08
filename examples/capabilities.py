# Custom capabilities from https://ai.pydantic.dev/capabilities/custom/: tools, instructions and hooks.
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from pydantic_ai import RunContext, ToolCallPart, ToolDefinition
from pydantic_ai.capabilities import AbstractCapability, PrefixTools, ValidatedToolArgs
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from fastapi_mcp_toolsets import MCPAdapter, MCPRouter

logger = logging.getLogger(__name__)

math_toolset = FunctionToolset()


@math_toolset.tool_plain
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@math_toolset.tool_plain
def multiply(a: float, b: float) -> float:
    """Multiply two numbers."""
    return a * b


@dataclass
class MathTools(AbstractCapability[Any]):
    """Provides basic math operations."""

    def get_toolset(self) -> AgentToolset[Any] | None:
        return math_toolset

    # Served as the server's `instructions`.
    def get_instructions(self) -> str:
        return "Use the math tools for any arithmetic."


@dataclass
class AuditTools(AbstractCapability[Any]):
    """Logs every tool call, as it would in an agent run."""

    async def before_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
    ) -> ValidatedToolArgs:
        logger.info("calling %s with %s", tool_def.name, args)
        return args


# Tools are listed as `math_add` and `math_multiply`.
adapter = MCPAdapter(capabilities=[PrefixTools(MathTools(), prefix="math"), AuditTools()], name="math")

app = FastAPI()
app.include_router(MCPRouter(adapter))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

# Tool approval from https://ai.pydantic.dev/deferred-tools/: at 2026-07-28 the client is asked for it.
import os

from fastapi import FastAPI
from pydantic_ai import ApprovalRequired, RunContext

from fastapi_mcp_toolsets import MCPRouter, MCPServer

PROTECTED_FILES = {".env"}

server = MCPServer(
    name="files",
    # Encrypts the state the client sends back with its answer; the same for every worker.
    request_state_keys=[key] if (key := os.environ.get("MCP_REQUEST_STATE_KEY")) else None,
)


@server.tool
def update_file(ctx: RunContext, path: str, content: str) -> str:
    if path in PROTECTED_FILES and not ctx.tool_call_approved:
        raise ApprovalRequired(metadata={"reason": "protected"})
    return f"File {path!r} updated: {content!r}"


@server.tool_plain(requires_approval=True)
def delete_file(path: str) -> str:
    return f"File {path!r} deleted"


app = FastAPI()
app.include_router(MCPRouter(server))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

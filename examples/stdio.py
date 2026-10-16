# The dice game served over stdio, the way Claude Desktop or Claude Code launch a local MCP server.
# Every message read from stdin goes through the same endpoint, middleware and dependencies as over HTTP.
from tools import app

from fastapi_mcp_toolsets.stdio import MCPStdio

if __name__ == "__main__":
    # The same as `fastapi-mcp-stdio tools:app -H "X-Player-Name: Bob"` run from `examples/`.
    MCPStdio(app, headers={"X-Player-Name": "Bob"}).run()

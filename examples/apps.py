# MCP Apps: a tool whose result the host shows in an HTML page served as a `ui://` resource.
from fastapi import FastAPI

from fastapi_mcp_toolsets import AppUI, MCPRouter, MCPServer, tool_metadata

server = MCPServer(name="clock")

CLOCK_HTML = """<!doctype html>
<html><body><h1 id="time">Loading...</h1>
<script>
  // The host posts the tool result to the app; the page shows it.
  window.addEventListener("message", (event) => {
    const text = event.data?.result?.content?.[0]?.text;
    if (text) document.getElementById("time").textContent = text;
  });
</script></body></html>
"""


@server.tool_plain(metadata=tool_metadata(AppUI(resource_uri="ui://clock/app.html")))
def get_time() -> str:
    """The current time."""
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


# A `ui://` resource is served as `text/html;profile=mcp-app` unless told otherwise.
@server.resource("ui://clock/app.html")
def clock_page() -> str:
    return CLOCK_HTML


app = FastAPI()
app.include_router(MCPRouter(server))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

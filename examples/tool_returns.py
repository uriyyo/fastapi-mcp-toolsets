# Rich tool results from https://ai.pydantic.dev/tools-advanced/, as MCP content blocks.
import base64

from fastapi import FastAPI
from pydantic import BaseModel
from pydantic_ai import BinaryContent, ImageUrl, ToolReturn

from fastapi_mcp_toolsets import MCPRouter, MCPServer

server = MCPServer(name="screen")

# 8x8 PNGs: grey before the click, green after.
BEFORE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAD0lEQVR4nGO4gwMwDC0JAHiIpQHpwXZPAAAAAElFTkSuQmCC"
)
AFTER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEUlEQVR4nGPQWGCDFTEMLQkAYT9BAZjEcQwAAAAASUVORK5CYII="
)


class Point(BaseModel):
    x: int
    y: int


# Becomes `structuredContent`, with an `outputSchema` on the tool.
@server.tool_plain
def locate(element: str) -> Point:
    """Find where an element is on the screen."""
    return Point(x=len(element) * 10, y=42)


@server.tool_plain
def click_and_capture(x: int, y: int) -> ToolReturn:
    """Click at coordinates and show before/after screenshots."""
    before_screenshot = BinaryContent(data=BEFORE_PNG, media_type="image/png")
    after_screenshot = BinaryContent(data=AFTER_PNG, media_type="image/png")
    return ToolReturn(
        return_value=f"Successfully clicked at ({x}, {y})",
        content=["Before:", before_screenshot, "After:", after_screenshot],
        # Becomes the result's `_meta`.
        metadata={"coordinates": {"x": x, "y": y}, "action_type": "click_and_capture"},
    )


# A URL is a resource link, or text before 2025-06-18.
@server.tool_plain
def reference_image() -> ImageUrl:
    """Link to the image to compare screenshots against."""
    return ImageUrl("https://upload.wikimedia.org/wikipedia/commons/7/7b/Parabola.png")


app = FastAPI()
app.include_router(MCPRouter(server))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=8000)

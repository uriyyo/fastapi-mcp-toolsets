from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic_ai import UserError

from .config import ToolConfig

__all__ = [
    "APPS_EXTENSION",
    "APP_MIME_TYPE",
    "UI_SCHEME",
    "AppUI",
]

# The MCP Apps extension: a tool's `_meta.ui.resourceUri` names a `ui://` HTML resource the host renders.
APPS_EXTENSION: Final = "io.modelcontextprotocol/ui"
APP_MIME_TYPE: Final = "text/html;profile=mcp-app"
UI_SCHEME: Final = "ui://"

type Visibility = Literal["model", "app"]


@dataclass(frozen=True, kw_only=True)
class AppUI(ToolConfig):
    """In a tool's metadata, the app the host shows for it: `tool_metadata(AppUI(resource_uri="ui://..."))`."""

    resource_uri: str
    # Where the tool is offered: to the model, to the app, or both (the default).
    visibility: Sequence[Visibility] | None = None

    def __post_init__(self) -> None:
        if not self.resource_uri.startswith(UI_SCHEME):
            raise UserError(f"An app's resource URI must start with {UI_SCHEME!r}, got {self.resource_uri!r}")

    def to_meta(self) -> dict[str, Any]:
        ui: dict[str, Any] = {"resourceUri": self.resource_uri}

        if self.visibility is not None:
            ui["visibility"] = [*self.visibility]

        return ui

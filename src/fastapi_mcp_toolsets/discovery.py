from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final, Literal, cast

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from mcp.server.auth.routes import cors_middleware
from pydantic import AnyHttpUrl, BaseModel, Field
from starlette.requests import Request

from fastapi_mcp_toolsets.protocol import MCPProtocolVersion
from fastapi_mcp_toolsets.server import MCPServer

__all__ = [
    "AI_CATALOG_PATH",
    "AICatalog",
    "AICatalogEntry",
    "Remote",
    "ServerCard",
    "ai_catalog_router",
    "server_card_router",
]

# SEP-2127: a card describes a remote server before any connection is made.
SERVER_CARD_SCHEMA: Final = "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json"
SERVER_CARD_MEDIA_TYPE: Final = "application/mcp-server-card+json"
SERVER_CARD_SUFFIX: Final = "/server-card"
AI_CATALOG_PATH: Final = "/.well-known/ai-catalog.json"
AI_CATALOG_MEDIA_TYPE: Final = "application/ai-catalog+json"
AI_CATALOG_SPEC_VERSION: Final = "1.0"
CACHE_CONTROL: Final = "public, max-age=3600"


class Remote(BaseModel):
    type: Literal["streamable-http", "sse"] = "streamable-http"
    url: str
    headers: list[dict[str, Any]] | None = None
    supported_protocol_versions: list[str] | None = Field(default=None, alias="supportedProtocolVersions")

    model_config = {"populate_by_name": True}


class ServerCard(BaseModel):
    """What a client can learn about the server without connecting; `name` is reverse-DNS, `ns/name`."""

    schema_: str = Field(default=SERVER_CARD_SCHEMA, alias="$schema")
    name: str = Field(pattern=r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")
    version: str
    description: str
    title: str | None = None
    website_url: str | None = Field(default=None, alias="websiteUrl")
    icons: list[dict[str, Any]] | None = None
    repository: dict[str, Any] | None = None
    remotes: list[Remote] | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")

    model_config = {"populate_by_name": True}

    @classmethod
    def for_server(
        cls,
        server: MCPServer[Any],
        *,
        name: str,
        url: AnyHttpUrl | str,
        description: str | None = None,
        protocol_versions: Sequence[MCPProtocolVersion] | None = None,
        **extra: Any,
    ) -> ServerCard:
        """A card from the server's info, pointing at the Streamable HTTP endpoint at `url`."""
        info = server.server_info
        versions = protocol_versions if protocol_versions is not None else list(MCPProtocolVersion)

        return cls(
            name=name,
            version=info.version,
            description=description or info.description or info.title or info.name,
            title=info.title,
            website_url=info.website_url,
            icons=[icon.model_dump(mode="json", exclude_none=True) for icon in info.icons] if info.icons else None,
            remotes=[Remote(url=str(url), supported_protocol_versions=[version.version for version in versions])],
            **extra,
        )


class AICatalogEntry(BaseModel):
    identifier: str
    type: str = SERVER_CARD_MEDIA_TYPE
    url: str | None = None
    data: ServerCard | None = None


class AICatalog(BaseModel):
    spec_version: str = Field(default=AI_CATALOG_SPEC_VERSION, alias="specVersion")
    entries: list[AICatalogEntry]

    model_config = {"populate_by_name": True}


def document_response(model: BaseModel, *, media_type: str) -> Response:
    return JSONResponse(
        model.model_dump(mode="json", by_alias=True, exclude_none=True),
        media_type=media_type,
        headers={"Cache-Control": CACHE_CONTROL},
    )


def server_card_router(card: ServerCard, *, path: str) -> APIRouter:
    """Serves the card at `{path}/server-card`, the location SEP-2127 reserves next to the MCP endpoint."""
    router = APIRouter()

    async def read_card(request: Request) -> Response:
        return document_response(card, media_type=SERVER_CARD_MEDIA_TYPE)

    endpoint = cast("Any", cors_middleware(read_card, ["GET", "OPTIONS"]))
    router.add_route(
        path.rstrip("/") + SERVER_CARD_SUFFIX, endpoint, methods=["GET", "OPTIONS"], name="mcp_server_card"
    )
    return router


def ai_catalog_router(catalog: AICatalog, *, path: str = AI_CATALOG_PATH) -> APIRouter:
    """Domain-level discovery: which server cards this host publishes."""
    router = APIRouter()

    async def read_catalog(request: Request) -> Response:
        return document_response(catalog, media_type=AI_CATALOG_MEDIA_TYPE)

    endpoint = cast("Any", cors_middleware(read_catalog, ["GET", "OPTIONS"]))
    router.add_route(path, endpoint, methods=["GET", "OPTIONS"], name="ai_catalog")
    return router

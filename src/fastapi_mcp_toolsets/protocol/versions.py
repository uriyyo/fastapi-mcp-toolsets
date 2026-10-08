from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Self

__all__ = [
    "MCPProtocolVersion",
    "ProtocolFeatures",
]


@dataclass(frozen=True, kw_only=True)
class ProtocolFeatures:
    version: str
    # Negotiated by `initialize` rather than sent with every request.
    handshake: bool
    # Results carry the server info in `_meta`.
    server_info_in_meta: bool
    # Errors map onto 4xx statuses rather than a 200.
    http_error_status: bool
    # Structured content may be any JSON value, not only an object.
    any_structured_content: bool
    # Arguments marked `x-mcp-header` are mirrored into `Mcp-Param-*` headers.
    mcp_param_headers: bool
    # The log level comes in each request's `_meta`.
    log_level_in_meta: bool
    # A request can be answered with `input_required` and retried with the answers.
    input_required: bool
    # Tools have an `outputSchema` and results `structuredContent`.
    structured_content: bool
    # Content may link to a resource instead of embedding it.
    resource_links: bool


class MCPProtocolVersion(Enum):
    V2025_03_26 = ProtocolFeatures(
        version="2025-03-26",
        handshake=True,
        server_info_in_meta=False,
        http_error_status=False,
        any_structured_content=False,
        mcp_param_headers=False,
        log_level_in_meta=False,
        input_required=False,
        structured_content=False,
        resource_links=False,
    )
    V2025_06_18 = ProtocolFeatures(
        version="2025-06-18",
        handshake=True,
        server_info_in_meta=False,
        http_error_status=False,
        any_structured_content=False,
        mcp_param_headers=False,
        log_level_in_meta=False,
        input_required=False,
        structured_content=True,
        resource_links=True,
    )
    V2025_11_25 = ProtocolFeatures(
        version="2025-11-25",
        handshake=True,
        server_info_in_meta=False,
        http_error_status=False,
        any_structured_content=False,
        mcp_param_headers=False,
        log_level_in_meta=False,
        input_required=False,
        structured_content=True,
        resource_links=True,
    )
    V2026_07_28 = ProtocolFeatures(
        version="2026-07-28",
        handshake=False,
        server_info_in_meta=True,
        http_error_status=True,
        any_structured_content=True,
        mcp_param_headers=True,
        log_level_in_meta=True,
        input_required=True,
        structured_content=True,
        resource_links=True,
    )

    @property
    def features(self) -> ProtocolFeatures:
        return self.value

    @property
    def version(self) -> str:
        return self.value.version

    @classmethod
    def _missing_(cls, value: object) -> Self | None:
        for protocol in cls:
            if protocol.version == value:
                return protocol

        return None

    @classmethod
    def modern(cls) -> list[Self]:
        return [protocol for protocol in cls if not protocol.features.handshake]

    @classmethod
    def latest(cls) -> Self:
        return cls.modern()[-1]

    @classmethod
    def assumed(cls) -> MCPProtocolVersion:
        return cls.V2025_03_26

    @classmethod
    def negotiate_handshake(cls, requested: Any | None = None, /) -> Self:
        handshake = [protocol for protocol in cls if protocol.features.handshake]

        for protocol in handshake:
            if protocol.version == requested:
                return protocol

        return handshake[-1]

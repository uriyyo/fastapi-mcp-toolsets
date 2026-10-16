from __future__ import annotations

from typing import Any, Final, Self

from pydantic_ai import ToolDefinition

__all__ = [
    "METADATA_KEY",
    "ToolConfig",
    "tool_metadata",
]

METADATA_KEY: Final = "fastapi_mcp_toolsets"


class ToolConfig:
    @classmethod
    def of(cls, tool_def: ToolDefinition) -> Self | None:
        match (tool_def.metadata or {}).get(METADATA_KEY):
            case {**configs} if isinstance(config := configs.get(cls), cls):
                return config
            case _:
                return None

    def as_metadata(self) -> dict[str, Any]:
        return tool_metadata(self)


def tool_metadata(*configs: ToolConfig, **metadata: Any) -> dict[str, Any]:
    return {
        **metadata,
        METADATA_KEY: {type(config): config for config in configs},
    }

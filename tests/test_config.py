from __future__ import annotations

from pydantic_ai import ToolDefinition

from fastapi_mcp_toolsets import TaskMode, TaskSupport, ToolAnnotation, tool_metadata
from fastapi_mcp_toolsets.features import METADATA_KEY


def test_tool_metadata_holds_one_config_per_kind() -> None:
    metadata = tool_metadata(ToolAnnotation(title="A"), TaskSupport(mode=TaskMode.REQUIRED), owner="me")
    tool_def = ToolDefinition(name="t", metadata=metadata)

    assert metadata["owner"] == "me"
    assert ToolAnnotation.of(tool_def) == ToolAnnotation(title="A")
    assert TaskSupport.of(tool_def) == TaskSupport(mode=TaskMode.REQUIRED)
    assert ToolAnnotation.of(ToolDefinition(name="bare")) is None
    assert ToolAnnotation().as_metadata() == {METADATA_KEY: {ToolAnnotation: ToolAnnotation()}}

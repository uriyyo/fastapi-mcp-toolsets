from __future__ import annotations

import mcp_types

from fastapi_mcp_toolsets.tasks import TASKS_EXTENSION

from .apps import build_app
from .conftest import HANDSHAKE_VERSION, MODERN_VERSION, modern_meta


async def test_handshake_initialize(connect) -> None:
    async with connect(build_app()) as client:
        result = await client.initialize()

        assert result["protocolVersion"] == HANDSHAKE_VERSION
        assert result["serverInfo"]["name"] == "tests"
        assert result["instructions"] == "Be nice."
        assert "tools" in result["capabilities"]
        assert "extensions" not in result["capabilities"]


async def test_handshake_list_and_call(connect) -> None:
    async with connect(build_app()) as client:
        await client.initialize()

        listed = await client.call("tools/list")
        tools = {tool["name"]: tool for tool in listed["result"]["tools"]}

        assert set(tools) == {"add", "whoami", "progress", "ask", "slow", "slow_ask"}
        assert tools["add"]["annotations"] == {"title": "Add", "readOnlyHint": True}
        assert tools["add"]["inputSchema"]["required"] == ["a", "b"]

        called = await client.call("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}})

        assert called["result"]["content"] == [{"type": "text", "text": "5"}]
        assert called["result"]["structuredContent"] == {"result": 5}


async def test_deps_resolve_from_headers(connect) -> None:
    async with connect(build_app()) as client:
        await client.initialize()

        default = await client.call("tools/call", {"name": "whoami", "arguments": {}})
        named = await client.call("tools/call", {"name": "whoami", "arguments": {}}, **{"x-player-name": "Bob"})

        assert default["result"]["content"][0]["text"] == "Anne"
        assert named["result"]["content"][0]["text"] == "Bob"


async def test_modern_envelope(connect) -> None:
    async with connect(build_app()) as client:
        discovered = await client.call("server/discover", {"_meta": modern_meta()})
        called = await client.call("tools/call", {"name": "add", "arguments": {"a": 1, "b": 1}, "_meta": modern_meta()})

        assert discovered["result"]["supportedVersions"] == [MODERN_VERSION]
        assert called["result"]["content"][0]["text"] == "2"
        assert called["result"]["_meta"][mcp_types.SERVER_INFO_META_KEY]["name"] == "tests"


async def test_modern_header_mismatch_is_rejected(connect) -> None:
    async with connect(build_app()) as client:
        rejected = await client.call(
            "tools/call",
            {"name": "add", "arguments": {"a": 1, "b": 1}, "_meta": modern_meta()},
            **{"mcp-name": "other"},
        )

        assert rejected["error"]["code"] == mcp_types.HEADER_MISMATCH


async def test_unknown_tool_and_method(connect) -> None:
    async with connect(build_app()) as client:
        await client.initialize()

        unknown_tool = await client.call("tools/call", {"name": "nope", "arguments": {}})
        unknown_method = await client.call("tasks/get", {"taskId": "x"})

        assert unknown_tool["error"]["code"] == mcp_types.INVALID_PARAMS
        assert unknown_method["error"]["code"] == mcp_types.METHOD_NOT_FOUND


async def test_tool_errors_are_results(connect) -> None:
    async with connect(build_app()) as client:
        await client.initialize()

        failed = await client.call("tools/call", {"name": "add", "arguments": {"a": "x", "b": 1}})

        assert failed["result"]["isError"] is True


async def test_progress_streams_before_result(connect) -> None:
    async with connect(build_app()) as client:
        await client.initialize()

        messages = await client.request(
            "tools/call",
            {"name": "progress", "arguments": {"steps": 2}, "_meta": {"progressToken": "p1"}},
        )

        assert [m["method"] for m in messages[:-1]] == ["notifications/progress"] * 2
        assert messages[0]["params"] == {"progressToken": "p1", "progress": 1, "total": 2}
        assert messages[-1]["result"]["content"][0]["text"] == "done"


async def test_elicitation_is_a_retry(connect) -> None:
    async with connect(build_app()) as client:
        meta = modern_meta({"elicitation": {"form": {}}})

        asked = await client.call("tools/call", {"name": "ask", "arguments": {}, "_meta": meta})
        (key, request), *_ = asked["result"]["inputRequests"].items()

        assert asked["result"]["resultType"] == "input_required"
        assert request["method"] == "elicitation/create"
        assert request["params"]["message"] == "Email?"

        answered = await client.call(
            "tools/call",
            {
                "name": "ask",
                "arguments": {},
                "_meta": meta,
                "requestState": asked["result"]["requestState"],
                "inputResponses": {key: {"action": "accept", "content": {"email": "a@b.c"}}},
            },
        )

        assert answered["result"]["content"][0]["text"] == "a@b.c"


async def test_elicitation_unavailable_on_handshake_versions(connect) -> None:
    async with connect(build_app()) as client:
        await client.initialize()

        asked = await client.call("tools/call", {"name": "ask", "arguments": {}})

        assert asked["result"]["isError"] is True
        assert "cannot ask" in asked["result"]["content"][0]["text"]


async def test_tasks_capability_only_with_backend(connect) -> None:
    from .apps import tasks_app

    async with connect(tasks_app()) as client:
        discovered = await client.call("server/discover", {"_meta": modern_meta()})

        assert discovered["result"]["capabilities"]["extensions"] == {TASKS_EXTENSION: {}}

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

        assert set(tools) == {
            "add",
            "whoami",
            "progress",
            "ask",
            "slow",
            "slow_ask",
            "tenant_name",
            "status_report",
            "sign_in",
            "solve",
        }
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


async def test_subscriptions_are_refused_outright(connect) -> None:
    async with connect(build_app()) as client:
        await client.initialize()

        refused = await client.call("resources/subscribe", {"uri": "file:///x"})

        assert refused["error"]["code"] == mcp_types.METHOD_NOT_FOUND
        assert "stateless" in refused["error"]["message"]


async def test_header_params_are_declared_and_checked(connect) -> None:
    async with connect(build_app()) as client:
        listed = await client.call("tools/list", {"_meta": modern_meta()})
        tool = next(tool for tool in listed["result"]["tools"] if tool["name"] == "tenant_name")
        params = {"name": "tenant_name", "arguments": {"tenant": "acme"}, "_meta": modern_meta()}

        unrouted = await client.call("tools/call", params)
        routed = await client.call("tools/call", params, **{"mcp-param-tenant": "acme"})

        assert tool["inputSchema"]["properties"]["tenant"]["x-mcp-header"] == "Tenant"
        assert unrouted["error"]["code"] == mcp_types.HEADER_MISMATCH
        assert routed["result"]["content"][0]["text"] == "acme"


async def test_apps_and_capability_settings(connect) -> None:
    from .apps import ui_app

    async with connect(ui_app()) as client:
        discovered = await client.call("server/discover", {"_meta": modern_meta()})
        listed = await client.call("tools/list", {"_meta": modern_meta()})
        resources = await client.call("resources/list", {"_meta": modern_meta()})
        page = await client.call("resources/read", {"uri": "ui://clock/app.html", "_meta": modern_meta()})

        capabilities = discovered["result"]["capabilities"]

        assert capabilities["experimental"] == {"x-feature": {}}
        assert capabilities["extensions"] == {"com.example/ext": {"level": 1}, "io.modelcontextprotocol/ui": {}}
        assert listed["result"]["tools"][0]["_meta"] == {
            "ui": {"resourceUri": "ui://clock/app.html", "visibility": ["app"]}
        }
        assert resources["result"]["resources"][0]["mimeType"] == "text/html;profile=mcp-app"
        assert page["result"]["contents"][0]["text"] == "<html>clock</html>"


async def test_url_elicitation_is_a_retry(connect) -> None:
    async with connect(build_app()) as client:
        meta = modern_meta({"elicitation": {"url": {}}})

        asked = await client.call("tools/call", {"name": "sign_in", "arguments": {}, "_meta": meta})
        request = asked["result"]["inputRequests"]["login"]

        assert request["params"] == {"mode": "url", "message": "Sign in first", "url": "https://example.com/login"}

        declined = await client.call(
            "tools/call",
            {
                "name": "sign_in",
                "arguments": {},
                "_meta": meta,
                "requestState": asked["result"]["requestState"],
                "inputResponses": {"login": {"action": "decline"}},
            },
        )
        unsupported = await client.call("tools/call", {"name": "sign_in", "arguments": {}, "_meta": modern_meta()})

        assert declined["result"]["content"][0]["text"] == "decline"
        assert unsupported["result"]["isError"] is True


async def test_sampling_with_tools_loops_through_retries(connect) -> None:
    async with connect(build_app()) as client:
        meta = modern_meta({"sampling": {"tools": {}}})
        params = {"name": "solve", "arguments": {"question": "2+2?"}, "_meta": meta}

        asked = await client.call("tools/call", params)
        (key, request), *_ = asked["result"]["inputRequests"].items()

        assert request["method"] == "sampling/createMessage"
        assert [tool["name"] for tool in request["params"]["tools"]] == ["add"]
        assert request["params"]["messages"] == [{"role": "user", "content": {"type": "text", "text": "2+2?"}}]

        # The model uses the tool; the server runs it and asks again with the result.
        use = {"type": "tool_use", "id": "u1", "name": "add", "input": {"a": 2, "b": 2}}
        asked_again = await client.call(
            "tools/call",
            {
                **params,
                "requestState": asked["result"]["requestState"],
                "inputResponses": {key: {"role": "assistant", "content": use, "model": "m", "stopReason": "toolUse"}},
            },
        )
        (key2, request2), *_ = asked_again["result"]["inputRequests"].items()
        tool_result = request2["params"]["messages"][-1]

        assert key2 != key
        assert request2["params"]["messages"][1]["content"] == use
        assert tool_result["role"] == "user"
        assert tool_result["content"][0]["type"] == "tool_result"
        assert tool_result["content"][0]["toolUseId"] == "u1"
        assert tool_result["content"][0]["content"] == [{"type": "text", "text": "4"}]

        answered = await client.call(
            "tools/call",
            {
                **params,
                "requestState": asked_again["result"]["requestState"],
                "inputResponses": {key2: {"role": "assistant", "content": {"type": "text", "text": "4"}, "model": "m"}},
            },
        )

        assert answered["result"]["content"][0]["text"] == "4"


async def test_sampling_with_tools_needs_the_capability(connect) -> None:
    async with connect(build_app()) as client:
        params = {"name": "solve", "arguments": {"question": "2+2?"}, "_meta": modern_meta({"sampling": {}})}

        refused = await client.call("tools/call", params)

        assert refused["result"]["isError"] is True
        assert "sampling with tools" in refused["result"]["content"][0]["text"]

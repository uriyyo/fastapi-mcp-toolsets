from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import anyio
import mcp_types
import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CallToolResult,
    ClientCapabilities,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    TextContent,
)

from fastapi_mcp_toolsets.tasks import (
    TASKS_EXTENSION,
    InProcessTaskBackend,
    TaskCall,
    TaskCompleted,
    TaskInputs,
    TaskNeedsInput,
    TaskOutcome,
)

from .apps import tasks_app
from .conftest import MCPClient, modern_meta

OPTED_IN = {"extensions": {TASKS_EXTENSION: {}}, "elicitation": {"form": {}}}


async def poll(client: MCPClient, task_id: str, until: set[str]) -> dict[str, Any]:
    for _ in range(200):
        task = (await client.call("tasks/get", {"taskId": task_id, "_meta": modern_meta(OPTED_IN)}))["result"]

        if task["status"] in until:
            return task

        await anyio.sleep(0.01)

    raise AssertionError(task)


async def test_opted_in_call_becomes_a_task(connect) -> None:
    async with connect(tasks_app()) as client:
        created = await client.call(
            "tools/call", {"name": "slow", "arguments": {"seconds": 0}, "_meta": modern_meta(OPTED_IN)}
        )
        task = created["result"]

        assert task["resultType"] == "task"
        assert task["status"] == "working"
        assert task["pollIntervalMs"] == 10

        done = await poll(client, task["taskId"], {"completed", "failed"})

        assert done["status"] == "completed"
        assert done["resultType"] == "complete"
        assert done["result"]["content"][0]["text"] == "slow done"


async def test_without_opt_in_runs_inline_or_refuses(connect) -> None:
    async with connect(tasks_app()) as client:
        inline = await client.call("tools/call", {"name": "slow", "arguments": {"seconds": 0}, "_meta": modern_meta()})
        refused = await client.call("tools/call", {"name": "slow_ask", "arguments": {}, "_meta": modern_meta()})

        assert inline["result"]["content"][0]["text"] == "slow done"
        assert refused["error"]["code"] == mcp_types.jsonrpc.MISSING_REQUIRED_CLIENT_CAPABILITY
        assert refused["error"]["data"] == {"requiredCapabilities": {"extensions": {TASKS_EXTENSION: {}}}}


async def test_task_waits_for_input_and_resumes(connect) -> None:
    async with connect(tasks_app()) as client:
        created = await client.call("tools/call", {"name": "slow_ask", "arguments": {}, "_meta": modern_meta(OPTED_IN)})
        task_id = created["result"]["taskId"]

        waiting = await poll(client, task_id, {"input_required"})
        (key, request), *_ = waiting["inputRequests"].items()

        assert request["method"] == "elicitation/create"

        updated = await client.call(
            "tasks/update",
            {
                "taskId": task_id,
                "inputResponses": {key: {"action": "accept", "content": {"email": "a@b.c"}}},
                "_meta": modern_meta(OPTED_IN),
            },
        )
        done = await poll(client, task_id, {"completed", "failed"})

        assert updated["result"]["resultType"] == "complete"
        assert done["result"]["content"][0]["text"] == "a@b.c"


async def test_cancel_and_not_found(connect) -> None:
    async with connect(tasks_app()) as client:
        created = await client.call(
            "tools/call", {"name": "slow", "arguments": {"seconds": 30}, "_meta": modern_meta(OPTED_IN)}
        )
        task_id = created["result"]["taskId"]

        cancelled = await client.call("tasks/cancel", {"taskId": task_id, "_meta": modern_meta(OPTED_IN)})
        task = await poll(client, task_id, {"cancelled"})
        missing = await client.call("tasks/get", {"taskId": "nope", "_meta": modern_meta(OPTED_IN)})
        undeclared = await client.call("tasks/get", {"taskId": task_id, "_meta": modern_meta()})

        assert cancelled["result"]["resultType"] == "complete"
        assert task["status"] == "cancelled"
        assert missing["error"]["code"] == mcp_types.INVALID_PARAMS
        assert undeclared["error"]["code"] == mcp_types.jsonrpc.MISSING_REQUIRED_CLIENT_CAPABILITY


# The backend on its own, with a runner standing in for the server.


def call(tool: str = "t") -> TaskCall:
    return TaskCall(
        tool=tool, arguments={}, protocol_version="2026-07-28", client_capabilities=ClientCapabilities(), headers={}
    )


def completed(text: str) -> TaskCompleted:
    return TaskCompleted(result=CallToolResult(content=[TextContent(type="text", text=text)]))


async def settled(backend: InProcessTaskBackend, task_id: str) -> Any:
    for _ in range(200):
        if (task := await backend.get(task_id)).status != "working":
            return task

        await anyio.sleep(0.01)

    raise AssertionError(task)


async def test_backend_runs_legs_until_complete() -> None:
    seen: list[Mapping[str, Any]] = []

    async def runner(task_id: str, call: TaskCall, inputs: TaskInputs) -> TaskOutcome:
        seen.append(inputs.responses)

        if "email" not in inputs.responses:
            request = ElicitRequest(params=ElicitRequestFormParams(message="?", requested_schema={"type": "object"}))
            return TaskNeedsInput(requests={"email": request}, inputs=TaskInputs(pending=("email",)))

        return completed(f"hello {task_id[:2]}")

    backend = InProcessTaskBackend(ttl=timedelta(seconds=5))

    async with backend.lifespan(runner):
        created = await backend.create(call())

        task = await settled(backend, created.task_id)

        assert task.status == "input_required"
        assert set(task.input_requests or {}) == {"email"}

        # Answers to keys not outstanding are ignored.
        await backend.update(
            created.task_id,
            {"email": ElicitResult(action="accept", content={}), "other": ElicitResult(action="accept")},
        )

        task = await settled(backend, created.task_id)

        assert task.status == "completed"
        assert (task.result or {})["content"][0]["text"] == f"hello {created.task_id[:2]}"
        assert [set(responses) for responses in seen] == [set(), {"email"}]


async def test_backend_expires_tasks() -> None:
    async def runner(task_id: str, call: TaskCall, inputs: TaskInputs) -> TaskOutcome:
        return completed("x")

    backend = InProcessTaskBackend(ttl=timedelta(0))

    async with backend.lifespan(runner):
        created = await backend.create(call())

        with pytest.raises(MCPError, match="expired"):
            await backend.get(created.task_id)

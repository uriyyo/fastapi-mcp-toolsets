from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final, Literal

from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, ErrorData, InputRequest, InputResponse, Request, RequestParams
from mcp_types.jsonrpc import MISSING_REQUIRED_CLIENT_CAPABILITY

from fastapi_mcp_toolsets.protocol import ExtensionResult

__all__ = [
    "TASKS_EXTENSION",
    "TASK_NAME_PARAMS",
    "CancelTaskRequest",
    "CancelTaskResult",
    "CreateTaskResult",
    "GetTaskRequest",
    "GetTaskResult",
    "TaskInfo",
    "TaskParams",
    "TaskStatus",
    "UpdateTaskParams",
    "UpdateTaskRequest",
    "UpdateTaskResult",
    "missing_tasks_capability",
    "task_expired",
    "task_not_found",
]

# The `io.modelcontextprotocol/tasks` extension (SEP-2663): a `tools/call` answered with a task to poll.
TASKS_EXTENSION: Final = "io.modelcontextprotocol/tasks"

# The param each task method mirrors into the `Mcp-Name` header, as `NAME_BEARING_METHODS` does for the core ones.
TASK_NAME_PARAMS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "tasks/get": "taskId",
        "tasks/update": "taskId",
        "tasks/cancel": "taskId",
    }
)

type TaskStatus = Literal["working", "input_required", "completed", "failed", "cancelled"]


class TaskInfo(ExtensionResult):
    """The flat task fields every task result carries."""

    task_id: str
    status: TaskStatus
    status_message: str | None = None
    created_at: str
    last_updated_at: str
    # Milliseconds from creation; `None` is unlimited.
    ttl_ms: int | None = None
    poll_interval_ms: int | None = None


class CreateTaskResult(TaskInfo):
    """What a `tools/call` answers when the server runs it as a task."""

    result_type: Literal["task"] = "task"


class GetTaskResult(TaskInfo):
    """The task as `tasks/get` reports it, with its result, error or outstanding input requests."""

    result_type: Literal["complete"] = "complete"
    result: dict[str, Any] | None = None
    error: ErrorData | None = None
    input_requests: dict[str, InputRequest] | None = None


class UpdateTaskResult(ExtensionResult):
    result_type: Literal["complete"] = "complete"


class CancelTaskResult(ExtensionResult):
    result_type: Literal["complete"] = "complete"


class TaskParams(RequestParams):
    task_id: str


class UpdateTaskParams(TaskParams):
    input_responses: dict[str, InputResponse]


class GetTaskRequest(Request[TaskParams, Literal["tasks/get"]]):
    method: Literal["tasks/get"] = "tasks/get"
    params: TaskParams


class UpdateTaskRequest(Request[UpdateTaskParams, Literal["tasks/update"]]):
    method: Literal["tasks/update"] = "tasks/update"
    params: UpdateTaskParams


class CancelTaskRequest(Request[TaskParams, Literal["tasks/cancel"]]):
    method: Literal["tasks/cancel"] = "tasks/cancel"
    params: TaskParams


def task_not_found(task_id: str) -> MCPError:
    # One message for a missing, expired or someone else's task, so ids cannot be probed.
    return MCPError(INVALID_PARAMS, f"Failed to retrieve task: Task {task_id} not found")


def task_expired(task_id: str) -> MCPError:
    return MCPError(INVALID_PARAMS, f"Failed to retrieve task: Task {task_id} has expired")


def missing_tasks_capability(what: str) -> MCPError:
    return MCPError(
        MISSING_REQUIRED_CLIENT_CAPABILITY,
        f"{what} requires the tasks extension ({TASKS_EXTENSION}), which the client did not declare for this request",
        {"requiredCapabilities": {"extensions": {TASKS_EXTENSION: {}}}},
    )

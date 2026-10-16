from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any, Protocol

from mcp_types import (
    CallToolResult,
    ClientCapabilities,
    ErrorData,
    Implementation,
    InputRequest,
    InputResponse,
    LoggingLevel,
)
from pydantic import BaseModel, ConfigDict

from fastapi_mcp_toolsets.features import ToolConfig

from .wire import CreateTaskResult, GetTaskResult

__all__ = [
    "TaskBackend",
    "TaskCall",
    "TaskCompleted",
    "TaskFailed",
    "TaskInputs",
    "TaskMode",
    "TaskNeedsInput",
    "TaskOutcome",
    "TaskRunner",
    "TaskSupport",
]


class TaskMode(StrEnum):
    FORBIDDEN = "forbidden"
    OPTIONAL = "optional"
    REQUIRED = "required"


@dataclass(frozen=True, kw_only=True)
class TaskSupport(ToolConfig):
    mode: TaskMode = TaskMode.OPTIONAL
    # How long the task is kept after creation, and how often to poll; the backend's defaults when `None`.
    ttl: timedelta | None = None
    poll_interval: timedelta | None = None


class TaskCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool: str
    arguments: dict[str, Any]
    protocol_version: str
    client_capabilities: ClientCapabilities
    client_info: Implementation | None = None
    headers: dict[str, str]
    log_level: LoggingLevel | None = None
    # From the tool's `TaskSupport`; the backend's defaults when `None`.
    ttl: timedelta | None = None
    poll_interval: timedelta | None = None


class TaskInputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Every answer so far, dumped; the run replays them in order.
    responses: dict[str, Any] = {}
    # The keys asked for in the last leg, the only ones `tasks/update` may answer.
    pending: tuple[str, ...] = ()
    # From `ApprovalRequired`, becomes the tool call metadata.
    metadata: Any = None

    def answer(self, responses: Mapping[str, Any]) -> TaskInputs:
        # Answers to anything not outstanding are ignored, as the spec asks.
        answered = {key: value for key, value in responses.items() if key in self.pending}

        return TaskInputs(
            responses={**self.responses, **answered},
            pending=(),
            metadata=self.metadata,
        )


class TaskCompleted(BaseModel):
    result: CallToolResult


class TaskFailed(BaseModel):
    error: ErrorData


class TaskNeedsInput(BaseModel):
    requests: dict[str, InputRequest]
    inputs: TaskInputs


type TaskOutcome = TaskCompleted | TaskFailed | TaskNeedsInput


class TaskRunner(Protocol):
    """Runs one leg of a task: the tool call up to its result, or up to the next input it needs."""

    async def __call__(self, task_id: str, call: TaskCall, inputs: TaskInputs) -> TaskOutcome: ...


class TaskBackend(ABC):
    """
    Where tasks live and run.

    The server hands the backend a `TaskRunner` for its lifetime and, for each `tools/call` it decides to run as
    a task, a `TaskCall`; the backend owns the task's id, state and execution from there, wherever that is.
    """

    @asynccontextmanager
    async def lifespan(self, runner: TaskRunner) -> AsyncGenerator[None]:
        yield

    @abstractmethod
    async def create(self, call: TaskCall) -> CreateTaskResult: ...

    @abstractmethod
    async def get(self, task_id: str) -> GetTaskResult: ...

    @abstractmethod
    async def update(self, task_id: str, responses: Mapping[str, InputResponse]) -> None: ...

    @abstractmethod
    async def cancel(self, task_id: str) -> None: ...

    @abstractmethod
    async def set_status(self, task_id: str, message: str) -> None:
        """What the running task says about itself, the `statusMessage` a client sees while it polls."""

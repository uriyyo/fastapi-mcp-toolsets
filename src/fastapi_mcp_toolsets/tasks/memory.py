from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
from anyio.abc import TaskGroup
from mcp.shared.exceptions import MCPError
from mcp_types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData, InputRequest, InputResponse
from pydantic_ai import UserError

from fastapi_mcp_toolsets.protocol import dump_model

from .backend import TaskBackend, TaskCall, TaskCompleted, TaskFailed, TaskInputs, TaskNeedsInput, TaskRunner
from .wire import CreateTaskResult, GetTaskResult, TaskStatus, task_expired, task_not_found

__all__ = [
    "InProcessTaskBackend",
]

logger = logging.getLogger(__name__)


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(kw_only=True)
class TaskRecord:
    task_id: str
    call: TaskCall
    inputs: TaskInputs = field(default_factory=TaskInputs)
    status: TaskStatus = "working"
    status_message: str | None = None
    created_at: datetime = field(default_factory=now)
    last_updated_at: datetime = field(default_factory=now)
    expires_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: ErrorData | None = None
    input_requests: dict[str, InputRequest] | None = None
    # Of the leg running right now, if one is.
    scope: anyio.CancelScope | None = None

    @property
    def done(self) -> bool:
        return self.status in {"completed", "failed", "cancelled"}

    def expired(self, at: datetime) -> bool:
        return self.expires_at is not None and at >= self.expires_at

    def settle(self, status: TaskStatus, *, message: str | None = None) -> None:
        self.status = status
        self.status_message = message
        self.last_updated_at = now()


class InProcessTaskBackend(TaskBackend):
    """Tasks run as background tasks of this process and live in its memory.

    Enough for one worker; a task created on one process cannot be polled from another, and none survive a
    restart.
    """

    def __init__(
        self,
        *,
        ttl: timedelta | None = timedelta(minutes=10),
        poll_interval: timedelta = timedelta(seconds=2),
    ) -> None:
        self.ttl = ttl
        self.poll_interval = poll_interval
        self._records: dict[str, TaskRecord] = {}
        self._runner: TaskRunner | None = None
        self._task_group: TaskGroup | None = None

    @asynccontextmanager
    async def lifespan(self, runner: TaskRunner) -> AsyncGenerator[None]:
        async with anyio.create_task_group() as tg:
            self._runner, self._task_group = runner, tg

            try:
                yield
            finally:
                # Whatever is still running will never be polled again.
                self._task_group = None
                tg.cancel_scope.cancel()

        self._runner = None

    @property
    def ttl_ms(self) -> int | None:
        return None if self.ttl is None else int(self.ttl.total_seconds() * 1000)

    @property
    def poll_interval_ms(self) -> int:
        return int(self.poll_interval.total_seconds() * 1000)

    def _record(self, task_id: str) -> TaskRecord:
        self._sweep()

        match self._records.get(task_id):
            case None:
                raise task_not_found(task_id)
            case record:
                return record

    def _sweep(self) -> None:
        at = now()

        for task_id, record in [*self._records.items()]:
            if record.expired(at):
                if record.scope is not None:
                    record.scope.cancel()

                del self._records[task_id]

    def _start(self, record: TaskRecord) -> None:
        match self._task_group:
            case None:
                raise UserError("The task backend must be entered first, e.g. by including the MCPRouter in the app")
            case tg:
                tg.start_soon(self._run, record)

    async def _run(self, record: TaskRecord) -> None:
        # Swept before it could start, e.g. by a TTL of zero.
        if self._runner is None or self._records.get(record.task_id) is not record:
            return

        with anyio.CancelScope() as scope:
            record.scope = scope

            try:
                outcome = await self._runner(record.task_id, record.call, record.inputs)
            except Exception:
                logger.exception("Task running %s raised", record.call.tool)
                outcome = TaskFailed(error=ErrorData(code=INTERNAL_ERROR, message="Internal server error"))

            record.scope = None

            match outcome:
                case TaskCompleted(result=result):
                    record.result = dump_model(result)
                    record.settle("completed")
                case TaskFailed(error=error):
                    record.error = error
                    record.settle("failed", message=error.message)
                case TaskNeedsInput(requests=requests, inputs=inputs):
                    record.input_requests = requests
                    record.inputs = inputs
                    record.settle("input_required", message="The task is waiting for input")

        if scope.cancel_called and not record.done:
            record.scope = None
            record.settle("cancelled")

    def _info(self, task_id: str, record: TaskRecord) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "status": record.status,
            "status_message": record.status_message,
            "created_at": record.created_at.isoformat(),
            "last_updated_at": record.last_updated_at.isoformat(),
            "ttl_ms": self.ttl_ms,
            "poll_interval_ms": self.poll_interval_ms,
        }

    async def create(self, call: TaskCall) -> CreateTaskResult:
        self._sweep()

        task_id = secrets.token_urlsafe(32)
        record = TaskRecord(task_id=task_id, call=call, expires_at=None if self.ttl is None else now() + self.ttl)
        self._records[task_id] = record
        self._start(record)

        return CreateTaskResult(**self._info(task_id, record))

    async def get(self, task_id: str) -> GetTaskResult:
        if (record := self._records.get(task_id)) is not None and record.expired(now()):
            self._sweep()
            raise task_expired(task_id)

        record = self._record(task_id)

        return GetTaskResult(
            **self._info(task_id, record),
            result=record.result,
            error=record.error,
            input_requests=record.input_requests,
        )

    async def update(self, task_id: str, responses: Mapping[str, InputResponse]) -> None:
        record = self._record(task_id)

        if record.status != "input_required":
            raise MCPError(INVALID_PARAMS, f"Task {task_id} is not waiting for input")

        record.inputs = record.inputs.answer({key: dump_model(response) for key, response in responses.items()})
        record.input_requests = None
        record.settle("working")
        self._start(record)

    async def cancel(self, task_id: str) -> None:
        record = self._record(task_id)

        if record.done:
            return

        match record.scope:
            case None:
                # Parked on input, nothing is running.
                record.settle("cancelled")
            case scope:
                scope.cancel()

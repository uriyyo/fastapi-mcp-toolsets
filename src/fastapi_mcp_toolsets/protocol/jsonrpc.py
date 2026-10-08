from __future__ import annotations

from typing import Any

from fastapi import status
from mcp.shared.inbound import ERROR_CODE_HTTP_STATUS
from mcp_types import (
    JSONRPC_VERSION,
    SERVER_INFO_META_KEY,
    ErrorData,
    Implementation,
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCResponse,
    RequestId,
    Result,
    ServerNotification,
)
from mcp_types.methods import serialize_server_result
from pydantic import BaseModel

from .versions import MCPProtocolVersion

__all__ = [
    "MCPResponse",
    "as_request_id",
    "dump",
    "dump_model",
    "http_status",
    "jsonrpc_error",
    "jsonrpc_notification",
    "jsonrpc_result",
]

type MCPResponse = JSONRPCResponse | JSONRPCError | None


def as_request_id(value: Any) -> RequestId | None:
    match value:
        case bool():
            # `bool` is an `int` subclass.
            return None
        case int() | str():
            return value
        case _:
            return None


def jsonrpc_error(
    code: int,
    message: str,
    *,
    request_id: RequestId | None = None,
    data: Any = None,
) -> JSONRPCError:
    return JSONRPCError(
        jsonrpc=JSONRPC_VERSION,
        id=request_id,
        error=ErrorData(code=code, message=message, data=data),
    )


def dump_model(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(by_alias=True, mode="json", exclude_none=True)


def jsonrpc_result(
    result: Result,
    *,
    method: str,
    protocol_version: MCPProtocolVersion,
    request_id: RequestId,
    server_info: Implementation,
) -> JSONRPCResponse:
    data = serialize_server_result(
        method,
        protocol_version.version,
        dump_model(result),
    )

    meta = data.get("_meta") or {}

    if protocol_version.features.server_info_in_meta and meta.get(SERVER_INFO_META_KEY) is None:
        data["_meta"] = {
            **meta,
            SERVER_INFO_META_KEY: dump_model(server_info),
        }

    return JSONRPCResponse(jsonrpc=JSONRPC_VERSION, id=request_id, result=data)


def jsonrpc_notification(notification: ServerNotification) -> JSONRPCNotification:
    data = dump_model(notification)

    return JSONRPCNotification(
        jsonrpc=JSONRPC_VERSION,
        method=data["method"],
        params=data.get("params"),
    )


def http_status(message: JSONRPCResponse | JSONRPCError, *, protocol_version: MCPProtocolVersion) -> int:
    if not protocol_version.features.http_error_status:
        return status.HTTP_200_OK

    match message:
        case JSONRPCError(error=ErrorData(code=code)):
            return ERROR_CODE_HTTP_STATUS.get(code, status.HTTP_200_OK)
        case _:
            return status.HTTP_200_OK


def dump(message: JSONRPCResponse | JSONRPCError | JSONRPCNotification) -> dict[str, Any]:
    # `JSONRPCError.id` is required on the wire, even as null.
    serialized = dump_model(message)

    match message:
        case JSONRPCError(id=request_id):
            serialized["id"] = request_id

    return serialized

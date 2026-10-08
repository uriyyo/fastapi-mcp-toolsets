from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from typing import Any

from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, PaginatedRequest

__all__ = [
    "page_cursor",
    "paginate",
]


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def decode_cursor(cursor: str) -> int:
    try:
        offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise MCPError(INVALID_PARAMS, f"Invalid cursor: {cursor}") from None

    if offset < 0:
        raise MCPError(INVALID_PARAMS, f"Invalid cursor: {cursor}")

    return offset


def paginate[T](
    items: Sequence[T],
    *,
    cursor: str | None,
    page_size: int | None,
) -> tuple[Sequence[T], str | None]:
    start = 0 if cursor is None else decode_cursor(cursor)

    if start > len(items):
        raise MCPError(INVALID_PARAMS, f"Invalid cursor: {cursor}")

    if page_size is None:
        return items[start:], None

    end = start + page_size
    return items[start:end], encode_cursor(end) if end < len(items) else None


def page_cursor(request: PaginatedRequest[Any]) -> str | None:
    return None if request.params is None else request.params.cursor

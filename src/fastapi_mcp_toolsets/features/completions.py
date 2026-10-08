from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Final

from mcp_types import Completion
from pydantic_ai import RunContext, UserError

__all__ = [
    "Completer",
    "complete",
]

MAX_COMPLETION_VALUES: Final[int] = 100

type Completer = (
    Sequence[str]
    | Callable[
        [
            RunContext[Any],
            str,
            Mapping[str, str],
        ],
        Sequence[str] | Awaitable[Sequence[str]],
    ]
)


async def complete(
    completer: Completer | None,
    ctx: RunContext[Any],
    value: str,
    arguments: Mapping[str, str],
) -> Completion:
    match completer:
        case None:
            values: Sequence[str] = []
        case str():
            raise UserError("A completer must be a sequence of strings, not a string")
        case Sequence():
            values = [choice for choice in completer if choice.startswith(value)]
        case _:
            result = completer(ctx, value, arguments)
            values = await result if inspect.isawaitable(result) else result

    return Completion(
        values=[*values[:MAX_COMPLETION_VALUES]],
        total=len(values),
        has_more=len(values) > MAX_COMPLETION_VALUES,
    )

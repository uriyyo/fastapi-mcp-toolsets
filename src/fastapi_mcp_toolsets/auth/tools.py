from __future__ import annotations

from dataclasses import dataclass
from functools import reduce
from operator import and_
from typing import Any, Final

from fastapi_has_permissions import CheckResult, Permission, evaluate, get_reason, is_successful
from fastapi_injected import UnboundScopeError
from pydantic_ai import RunContext, ToolCallPart, ToolDefinition, ToolFailed
from pydantic_ai.capabilities import AbstractCapability, ValidatedToolArgs

from .chain import Challenger
from .permissions import HasScopes

__all__ = [
    "PERMISSION_METADATA_KEY",
    "SCOPES_METADATA_KEY",
    "GuardedTools",
    "requires",
    "requires_scopes",
    "tool_permission",
    "tool_scopes",
]

PERMISSION_METADATA_KEY: Final = "permission"
SCOPES_METADATA_KEY: Final = "scopes"


def requires(permission: Permission) -> dict[str, Any]:
    """Tool metadata: the permission a caller needs to see and call the tool."""
    return {PERMISSION_METADATA_KEY: permission}


def requires_scopes(*scopes: str) -> dict[str, Any]:
    """Tool metadata: the scopes a caller's token or key needs to see and call the tool."""
    return {SCOPES_METADATA_KEY: frozenset(scopes)}


def tool_permission(tool_def: ToolDefinition) -> Permission | None:
    match (tool_def.metadata or {}).get(PERMISSION_METADATA_KEY):
        case Permission() as permission:
            return permission
        case _:
            return None


def tool_scopes(tool_def: ToolDefinition) -> frozenset[str]:
    match (tool_def.metadata or {}).get(SCOPES_METADATA_KEY):
        case str() as scope:
            return frozenset({scope})
        case None:
            return frozenset()
        case scopes:
            return frozenset(scopes)


@dataclass
class GuardedTools(AbstractCapability[Any]):
    """Hides the tools the caller may not use and refuses calls to them.

    A tool's requirement is its `requires(...)` permission and its `requires_scopes(...)`, checked against the
    actor `actor_dep` resolves to: the chain, a bearer, or any dependency with a `scopes` attribute. Checks run
    through fastapi-has-permissions inside the request's inject scope, so they share the route's dependency cache.

    Not for a `LazyBearer`: hiding tools from anonymous callers is the opposite of lazy authentication, which
    lists every tool and asks for sign-in on the call.
    """

    actor_dep: Any
    # For the `insufficient_scope` details; defaults to the bearer or chain behind `actor_dep`.
    challenge: Challenger | None = None

    def permission_for(self, tool_def: ToolDefinition) -> Permission | None:
        permissions: list[Permission] = []

        if scopes := tool_scopes(tool_def):
            permissions.append(HasScopes(self.actor_dep, scopes, challenge=self.challenge))

        if (permission := tool_permission(tool_def)) is not None:
            permissions.append(permission)

        return reduce(and_, permissions) if permissions else None

    async def check(self, tool_def: ToolDefinition) -> CheckResult:
        if (permission := self.permission_for(tool_def)) is None:
            return True

        try:
            return await evaluate(permission)
        except UnboundScopeError:
            # Prepared without a request (`ToolManagerScope.APP`): nothing to decide by yet.
            return True

    async def prepare_tools(self, ctx: RunContext[Any], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
        allowed: list[ToolDefinition] = []

        for tool_def in tool_defs:
            result = await self.check(tool_def)

            if is_successful(result):
                allowed.append(tool_def)

        return allowed

    async def before_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
    ) -> ValidatedToolArgs:
        result = await self.check(tool_def)

        if not is_successful(result):
            raise ToolFailed(f"{tool_def.name}: {get_reason(result) or 'permission denied'}")

        return args

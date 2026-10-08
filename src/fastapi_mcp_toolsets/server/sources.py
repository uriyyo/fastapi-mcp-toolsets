from __future__ import annotations

from typing import Any

from pydantic_ai import UserError
from pydantic_ai.capabilities import WrapperCapability
from pydantic_ai.capabilities.abstract import AbstractCapability, leaf_capabilities

from fastapi_mcp_toolsets.features import MCPPrompts, MCPResources, PromptFunction, ResourceFunction

__all__ = [
    "find_capabilities",
    "served_prompts",
    "served_resources",
]


def find_capabilities[C](capability: AbstractCapability[Any], kind: type[C]) -> list[C]:
    found: list[C] = []

    for leaf in leaf_capabilities(capability):
        unwrapped = leaf

        # `apply` stops at a wrapper around a single capability.
        while isinstance(unwrapped, WrapperCapability):
            unwrapped = unwrapped.wrapped

        if isinstance(unwrapped, kind):
            found.append(unwrapped)

    return found


def served_resources(capability: AbstractCapability[Any]) -> list[ResourceFunction]:
    return [resource for resources in find_capabilities(capability, MCPResources) for resource in resources.resources]


def served_prompts(capability: AbstractCapability[Any]) -> dict[str, PromptFunction]:
    prompts: dict[str, PromptFunction] = {}

    for found in find_capabilities(capability, MCPPrompts):
        if duplicated := prompts.keys() & found.prompts.keys():
            raise UserError(f"Prompts registered more than once: {sorted(duplicated)}")

        prompts |= found.prompts

    return prompts

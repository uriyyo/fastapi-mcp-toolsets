# Imported for the handlers they register.
from . import completions, lifecycle, prompts, resources, tools  # noqa: F401
from .dispatch import DEFAULT_HANDLERS, MCPHandler, MCPHandlers, handle

__all__ = [
    "DEFAULT_HANDLERS",
    "MCPHandler",
    "MCPHandlers",
    "handle",
]

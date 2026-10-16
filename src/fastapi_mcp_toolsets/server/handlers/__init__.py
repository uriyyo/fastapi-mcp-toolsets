# Imported for the handlers they register.
from . import completions, lifecycle, prompts, resources, tasks, tools  # noqa: F401
from .dispatch import DEFAULT_HANDLERS, MCPHandler, MCPHandlers, handle, parse_extension_request
from .tasks import run_task

__all__ = [
    "DEFAULT_HANDLERS",
    "MCPHandler",
    "MCPHandlers",
    "handle",
    "parse_extension_request",
    "run_task",
]

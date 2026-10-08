from typing_extensions import TypeVar

__all__ = [
    "AgentDepsT",
]

# Defaults to `None`, so a server without deps needs no type argument.
AgentDepsT = TypeVar("AgentDepsT", default=None)

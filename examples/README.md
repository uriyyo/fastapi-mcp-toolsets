# Examples

One feature per file, most of them taken from the [Pydantic AI docs](https://ai.pydantic.dev/).
Each one is a FastAPI app serving its MCP server at `/mcp`:

```sh
uv run python examples/tools.py
```

| Example                            | Shows                                                                                                                           |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| [tools.py](tools.py)               | Tools registered on the server, with deps and tool annotations ([tools](https://ai.pydantic.dev/tools/))                        |
| [toolsets.py](toolsets.py)         | Serving existing toolsets, prepared once per app ([toolsets](https://ai.pydantic.dev/toolsets/))                                                       |
| [agent.py](agent.py)               | Serving an existing agent's tools and instructions, with resources and prompts added on the adapter ([agents](https://ai.pydantic.dev/agents/)) |
| [rest.py](rest.py)                 | The same tools from plain REST routes, through the `adapter.context` FastAPI dependency |
| [capabilities.py](capabilities.py) | Capabilities providing tools, instructions and hooks ([capabilities](https://ai.pydantic.dev/capabilities/custom/))             |
| [dependencies.py](dependencies.py) | Deps resolved per request from headers, settings and yield dependencies ([dependencies](https://ai.pydantic.dev/dependencies/)) |
| [approvals.py](approvals.py)       | Tools that need the user's approval ([deferred tools](https://ai.pydantic.dev/deferred-tools/))                                 |
| [tool_returns.py](tool_returns.py) | Structured content, images, resource links and `_meta` ([advanced tools](https://ai.pydantic.dev/tools-advanced/))              |
| [code_mode.py](code_mode.py) | Tools served as one sandboxed `run_code` tool ([code mode](https://ai.pydantic.dev/harness/code-mode/), needs `pydantic-ai-harness[codemode]`) |
| [progress.py](progress.py)         | Log and progress notifications                                                                                                  |
| [elicitation.py](elicitation.py)   | Asking the user for input                                                                                                       |
| [sampling.py](sampling.py)         | Asking the client's model for a completion                                                                                      |
| [roots.py](roots.py)               | Asking the client for its filesystem roots                                                                                      |
| [resources.py](resources.py)       | Resources and resource templates, with completion                                                                               |
| [prompts.py](prompts.py)           | Prompts, with completion                                                                                                        |
| [gateway.py](gateway.py) | Remote MCP servers (DeepWiki, GitMCP) and local tools behind one endpoint, with hooks for access control, auditing and failures, a per-user quota, approvals, resources and prompts (needs `pydantic-ai-slim[mcp]`) |
| [oauth.py](oauth.py) | Your own sign-in page turned into an OAuth authorization server for MCP clients (`StoredProvider` with `@provider.authenticate` and `@provider.on.identity`), a bearer `Security` dependency on the MCP route, the token in deps, and per-tool scopes with `GuardedTools` |

Approvals, elicitation, sampling and roots need protocol version 2026-07-28: the call is answered with
what the server needs, and the client retries it with the answers.

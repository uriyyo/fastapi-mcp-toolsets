from __future__ import annotations

import io
import json

import anyio

from fastapi_mcp_toolsets.stdio import MCPStdio

from .apps import build_app


def lines(*messages: dict) -> str:
    return "".join(json.dumps(message) + "\n" for message in messages)


async def test_stdio_round_trip() -> None:
    stdin = anyio.wrap_file(
        io.StringIO(
            lines(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "whoami", "arguments": {}}},
            )
        )
    )
    out = io.StringIO()

    await MCPStdio(build_app(), headers={"X-Player-Name": "Bob"}).serve(stdin, anyio.wrap_file(out))

    replies = {message["id"]: message for message in map(json.loads, out.getvalue().splitlines())}

    assert replies[1]["result"]["protocolVersion"] == "2025-11-25"
    assert replies[2]["result"]["content"][0]["text"] == "Bob"

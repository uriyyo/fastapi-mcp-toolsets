from __future__ import annotations

import importlib
import sys
from typing import Annotated, Any

import typer

from .bridge import MCPStdio

app = typer.Typer(
    name="fastapi-mcp-stdio",
    add_completion=False,
    rich_markup_mode=None,
)


def parse_header(value: str) -> tuple[str, str]:
    match value.partition(":"):
        case name, ":", header if name.strip():
            return name.strip(), header.strip()
        case _:
            raise typer.BadParameter(f"expected 'Name: value', got {value!r}")


def load_app(spec: str) -> Any:
    match spec.partition(":"):
        case module_name, ":", attribute:
            sys.path.insert(0, "")

            try:
                return getattr(importlib.import_module(module_name), attribute)
            except (ImportError, AttributeError) as e:
                raise typer.BadParameter(str(e)) from e
        case _:
            raise typer.BadParameter(f"expected 'module:app', got {spec!r}")


@app.command()
def main(
    app: Annotated[
        str,
        typer.Argument(
            help="The ASGI app serving the endpoint, as 'module:app'",
        ),
    ],
    path: Annotated[
        str,
        typer.Option(
            help="The endpoint path within the app",
        ),
    ] = "/mcp",
    header: Annotated[
        list[str] | None,
        typer.Option(
            "--header",
            "-H",
            metavar="'NAME: VALUE'",
            help="A header sent with every request",
        ),
    ] = None,
) -> None:
    """Serve an app's MCP endpoint over stdio."""
    headers = dict(map(parse_header, header or ()))

    server = MCPStdio(load_app(app), path=path, headers=headers)
    server.run()


if __name__ == "__main__":
    app()

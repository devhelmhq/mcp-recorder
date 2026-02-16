"""CLI entry point for mcp-recorder."""

import click

from mcp_recorder import __version__


@click.group()
@click.version_option(version=__version__, prog_name="mcp-recorder")
def main() -> None:
    """Record and replay MCP server interactions for deterministic testing."""


@main.command()
@click.option("--target", required=True, help="URL of the real MCP server.")
@click.option("--port", default=5555, show_default=True, help="Local proxy port.")
@click.option("--output", default="recording.json", show_default=True, help="Output cassette file.")
def record(target: str, port: int, output: str) -> None:
    """Record interactions from a live MCP server."""
    click.echo(f"Recording from {target} on port {port} -> {output}")
    raise NotImplementedError("Recording will be implemented in Phase 2.")


@main.command()
@click.option("--cassette", required=True, help="Path to cassette file.")
@click.option("--port", default=5555, show_default=True, help="Local server port.")
def replay(cassette: str, port: int) -> None:
    """Start a mock server from a recorded cassette."""
    click.echo(f"Replaying {cassette} on port {port}")
    raise NotImplementedError("Replay will be implemented in Phase 3.")


@main.command()
@click.argument("cassette")
def inspect(cassette: str) -> None:
    """Pretty-print a cassette summary."""
    click.echo(f"Inspecting {cassette}")
    raise NotImplementedError("Inspect will be implemented in Phase 4.")

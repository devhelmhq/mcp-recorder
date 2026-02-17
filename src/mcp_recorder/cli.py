"""CLI entry point for mcp-recorder."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click
import uvicorn

from mcp_recorder import __version__
from mcp_recorder._types import RawRecording
from mcp_recorder.proxy import create_proxy_app

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    stream=sys.stderr,
)


@click.group()
@click.version_option(version=__version__, prog_name="mcp-recorder")
def main() -> None:
    """Record, replay, and verify MCP server interactions for deterministic testing."""


@main.command()
@click.option("--target", required=True, help="URL of the real MCP server.")
@click.option("--port", default=5555, show_default=True, help="Local proxy port.")
@click.option("--output", default="recording.json", show_default=True, help="Output cassette file.")
@click.option(
    "--format", "fmt", default="json", show_default=True, help="Cassette format (json/yaml)."
)
@click.option("--no-redact", is_flag=True, help="Disable automatic secret redaction.")
@click.option("--redact-patterns", multiple=True, help="Additional regex patterns to redact.")
def record(
    target: str,
    port: int,
    output: str,
    fmt: str,
    no_redact: bool,
    redact_patterns: tuple[str, ...],
) -> None:
    """Record interactions from a live MCP server."""
    recording = RawRecording(target=target)
    app = create_proxy_app(target_url=target, recording=recording)

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    click.echo(f"Proxying http://localhost:{port} -> {target}")
    click.echo(f"Output:  {output_path}")
    click.echo("Press Ctrl+C to stop and save the recording.\n")

    try:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            log_level="warning",
        )
    except KeyboardInterrupt:
        pass
    finally:
        _save_recording(recording, output_path)


def _save_recording(recording: RawRecording, path: Path) -> None:
    """Flush the in-memory recording to disk."""
    recording.interaction_count = len(recording.interactions)

    if recording.interaction_count == 0:
        click.echo("\nNo interactions captured. Nothing to save.")
        return

    data = recording.model_dump(mode="json")
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    click.echo(f"\nSaved {recording.interaction_count} interactions to {path}")


@main.command()
@click.option("--cassette", required=True, help="Path to cassette file.")
@click.option("--port", default=5555, show_default=True, help="Local server port.")
@click.option(
    "--match",
    default="method_params",
    show_default=True,
    type=click.Choice(["method_params", "sequential", "strict"]),
    help="Request matching strategy.",
)
@click.option("--simulate-latency", is_flag=True, help="Replay with original recorded timing.")
def replay(cassette: str, port: int, match: str, simulate_latency: bool) -> None:
    """Start a mock server from a recorded cassette."""
    click.echo(f"Replaying {cassette} on port {port} (match={match})")
    raise NotImplementedError("Replay will be implemented in Phase 3.")


@main.command()
@click.option("--cassette", required=True, help="Path to golden cassette file.")
@click.option("--target", required=True, help="URL of the server to verify.")
@click.option("--ignore-fields", multiple=True, help="JSON paths to ignore during comparison.")
@click.option("--update", is_flag=True, help="Update the cassette with new responses.")
def verify(cassette: str, target: str, ignore_fields: tuple[str, ...], update: bool) -> None:
    """Replay recorded requests against a server and compare responses."""
    click.echo(f"Verifying {cassette} against {target}")
    raise NotImplementedError("Verify will be implemented in Phase 3.")


@main.command()
@click.argument("cassette")
def inspect(cassette: str) -> None:
    """Pretty-print a cassette summary."""
    click.echo(f"Inspecting {cassette}")
    raise NotImplementedError("Inspect will be implemented in Phase 4.")

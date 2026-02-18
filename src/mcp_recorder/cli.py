"""CLI entry point for mcp-recorder."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click
import uvicorn

from mcp_recorder import __version__
from mcp_recorder._types import Cassette, CassetteMetadata, InteractionType
from mcp_recorder.matcher import create_matcher
from mcp_recorder.proxy import create_proxy_app
from mcp_recorder.replayer import create_replay_app
from mcp_recorder.scrubber import scrub_cassette
from mcp_recorder.verifier import run_verify


@click.group()
@click.version_option(version=__version__, prog_name="mcp-recorder")
def main() -> None:
    """Record, replay, and verify MCP server interactions for deterministic testing."""


@main.command()
@click.option("--target", required=True, help="URL of the real MCP server.")
@click.option("--port", default=5555, show_default=True, help="Local proxy port.")
@click.option("--output", default="recording.json", show_default=True, help="Output cassette file.")
@click.option("--verbose", is_flag=True, help="Log full headers and bodies to stderr.")
@click.option(
    "--redact-server-url/--no-redact-server-url",
    default=True,
    show_default=True,
    help="Strip the URL path from metadata (keeps scheme + host).",
)
@click.option(
    "--redact-env",
    multiple=True,
    help="Env var name whose value is redacted from metadata + responses. Repeatable.",
)
@click.option(
    "--redact-patterns",
    multiple=True,
    help="Regex pattern to redact from metadata + responses. Repeatable.",
)
def record(
    target: str,
    port: int,
    output: str,
    verbose: bool,
    redact_server_url: bool,
    redact_env: tuple[str, ...],
    redact_patterns: tuple[str, ...],
) -> None:
    """Record interactions from a live MCP server."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(message)s", stream=sys.stderr)

    cassette = Cassette(metadata=CassetteMetadata(server_url=target))
    app = create_proxy_app(target_url=target, cassette=cassette, verbose=verbose)

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    click.echo(f"Proxying http://localhost:{port} -> {target}", err=True)
    click.echo(f"Output:  {output_path}", err=True)
    click.echo("Press Ctrl+C to stop and save the recording.\n", err=True)

    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        _save_cassette(
            cassette,
            output_path,
            redact_server_url=redact_server_url,
            redact_env=redact_env,
            redact_patterns=redact_patterns,
        )


def _load_cassette(path: Path) -> Cassette:
    """Load a cassette from a JSON file."""
    raw = json.loads(path.read_text())
    return Cassette.model_validate(raw)


def _save_cassette(
    cassette: Cassette,
    path: Path,
    *,
    redact_server_url: bool = False,
    redact_env: tuple[str, ...] = (),
    redact_patterns: tuple[str, ...] = (),
) -> None:
    """Flush the cassette to disk as JSON, applying any requested redactions."""
    count = len(cassette.interactions)
    if count == 0:
        click.echo("\nNo interactions captured. Nothing to save.", err=True)
        return

    cassette = scrub_cassette(
        cassette,
        redact_server_url=redact_server_url,
        redact_env=redact_env,
        redact_patterns=redact_patterns,
    )

    data = cassette.model_dump(mode="json")
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    click.echo(f"\nSaved {count} interactions to {path}", err=True)


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
@click.option("--verbose", is_flag=True, help="Log every matched request to stderr.")
def replay(cassette: str, port: int, match: str, verbose: bool) -> None:
    """Start a mock MCP server from a recorded cassette."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(message)s", stream=sys.stderr)

    cassette_path = Path(cassette)
    if not cassette_path.exists():
        click.echo(f"Error: cassette file not found: {cassette_path}", err=True)
        raise SystemExit(1)

    loaded = _load_cassette(cassette_path)
    matcher_obj = create_matcher(match, loaded.interactions)
    app = create_replay_app(loaded, matcher_obj)

    total = sum(1 for i in loaded.interactions if i.type.value == "jsonrpc_request")
    click.echo(f"Replaying {cassette_path.name} ({total} request interactions)", err=True)
    click.echo(f"Matching strategy: {match}", err=True)
    click.echo(f"Mock server: http://localhost:{port}/mcp", err=True)
    click.echo("Press Ctrl+C to stop.\n", err=True)

    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        if matcher_obj.all_consumed:
            click.echo("\nAll recorded interactions were consumed.", err=True)
        else:
            remaining = total - matcher_obj._matched_count
            click.echo(f"\nWarning: {remaining} recorded interactions were NOT consumed.", err=True)
        if matcher_obj.unmatched_requests:
            click.echo(
                f"{len(matcher_obj.unmatched_requests)} incoming request(s) had no match.",
                err=True,
            )


@main.command()
@click.option("--cassette", required=True, help="Path to golden cassette file.")
@click.option("--target", required=True, help="URL of the server to verify.")
@click.option("--ignore-fields", multiple=True, help="Response fields to ignore during comparison.")
@click.option("--update", is_flag=True, help="Update the cassette with new responses.")
@click.option("--verbose", is_flag=True, help="Show full diff for each failing interaction.")
def verify(
    cassette: str,
    target: str,
    ignore_fields: tuple[str, ...],
    update: bool,
    verbose: bool,
) -> None:
    """Replay recorded requests against a server and compare responses."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(message)s", stream=sys.stderr)

    cassette_path = Path(cassette)
    if not cassette_path.exists():
        click.echo(f"Error: cassette file not found: {cassette_path}", err=True)
        raise SystemExit(1)

    loaded = _load_cassette(cassette_path)
    ignore = frozenset(ignore_fields)

    click.echo(f"Verifying {cassette_path.name} against {target}", err=True)
    click.echo(f"Interactions: {len(loaded.interactions)}", err=True)
    if ignore:
        click.echo(f"Ignoring fields: {', '.join(ignore)}", err=True)
    click.echo("", err=True)

    result = run_verify(loaded, target, ignore_fields=ignore)

    click.echo("", err=True)
    for r in result.results:
        status = "PASS" if r.passed else "FAIL"
        click.echo(f"  {r.index}. {r.method} [{status}]", err=True)
        if not r.passed and r.diff:
            for line in r.diff:
                click.echo(f"    {line}", err=True)

    click.echo("", err=True)
    click.echo(
        f"Result: {result.passed}/{result.total} passed, {result.failed} failed",
        err=True,
    )

    if update and result.failed > 0:
        for r in result.results:
            if not r.passed and r.actual is not None:
                loaded.interactions[r.index - 1].response = r.actual
        _save_cassette(loaded, cassette_path)
        click.echo(f"Updated {cassette_path} with new responses.", err=True)

    if result.failed > 0:
        raise SystemExit(1)


@main.command()
@click.argument("cassette")
def inspect(cassette: str) -> None:
    """Pretty-print a cassette summary."""
    cassette_path = Path(cassette)
    if not cassette_path.exists():
        click.echo(f"Error: cassette file not found: {cassette_path}", err=True)
        raise SystemExit(1)

    loaded = _load_cassette(cassette_path)
    meta = loaded.metadata

    click.echo(cassette_path.name)

    recorded = meta.recorded_at[:19].replace("T", " ") if meta.recorded_at else "unknown"
    click.echo(f"  Recorded: {recorded}")

    if meta.server_info:
        name = meta.server_info.get("name", "unknown")
        version = meta.server_info.get("version", "")
        click.echo(f"  Server:   {name} v{version}" if version else f"  Server:   {name}")
    if meta.protocol_version:
        click.echo(f"  Protocol: {meta.protocol_version}")
    if meta.server_url:
        click.echo(f"  Target:   {meta.server_url}")

    interactions = loaded.interactions
    click.echo(f"\n  Interactions ({len(interactions)}):")
    for i, interaction in enumerate(interactions, 1):
        click.echo(f"    {i}. {interaction.summary}")

    requests = sum(1 for x in interactions if x.type == InteractionType.JSONRPC_REQUEST)
    notifications = sum(1 for x in interactions if x.type == InteractionType.NOTIFICATION)
    lifecycle = sum(1 for x in interactions if x.type == InteractionType.LIFECYCLE)
    parts = []
    if requests:
        parts.append(f"{requests} request{'s' if requests != 1 else ''}")
    if notifications:
        parts.append(f"{notifications} notification{'s' if notifications != 1 else ''}")
    if lifecycle:
        parts.append(f"{lifecycle} lifecycle")
    click.echo(f"\n  Summary: {', '.join(parts)}")

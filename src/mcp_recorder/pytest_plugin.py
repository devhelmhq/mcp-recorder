"""Pytest plugin for mcp-recorder (registered via pytest11 entry point).

Provides fixtures and markers for using MCP cassettes in tests:

    @pytest.mark.mcp_cassette("cassettes/golden.json")
    def test_tool_call(mcp_replay_url):
        async with Client(mcp_replay_url) as client:
            result = await client.call_tool("add", {"a": 1, "b": 2})
            assert ...
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from pathlib import Path

import pytest

from mcp_recorder._utils import UvicornServer, find_free_port, load_cassette
from mcp_recorder.matcher import Matcher, create_matcher
from mcp_recorder.replayer import create_replay_app
from mcp_recorder.verifier import VerifyResult, run_verify

logger = logging.getLogger("mcp_recorder.plugin")


# ---------------------------------------------------------------------------
# pytest CLI options
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("mcp-recorder", "MCP cassette recording and replay")
    group.addoption(
        "--mcp-record-mode",
        default="replay",
        choices=["replay", "record", "auto"],
        help=(
            "replay: serve from cassette (default). "
            "record: record a new cassette from --mcp-target. "
            "auto: replay if cassette exists, skip if not."
        ),
    )
    group.addoption(
        "--mcp-target",
        default=None,
        help="Live MCP server URL for record/verify modes.",
    )
    group.addoption(
        "--mcp-match",
        default="method_params",
        choices=["method_params", "sequential", "strict"],
        help="Matching strategy for replay (default: method_params).",
    )


# ---------------------------------------------------------------------------
# Marker registration
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "mcp_cassette(path, *, match=None): bind an MCP cassette to this test",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_cassette_path(request: pytest.FixtureRequest) -> tuple[Path, str]:
    """Extract cassette path and match strategy from the mcp_cassette marker."""
    marker = request.node.get_closest_marker("mcp_cassette")
    if marker is None:
        pytest.fail("mcp_replay_url / mcp_verify_result requires @pytest.mark.mcp_cassette('path')")

    if not marker.args:
        pytest.fail("@pytest.mark.mcp_cassette requires a cassette path as the first argument")

    cassette_rel = marker.args[0]
    match_strategy: str | None = marker.kwargs.get("match")

    # Resolve relative to the test file's directory
    test_dir = request.path.parent
    cassette_path = test_dir / cassette_rel
    if not cassette_path.is_absolute():
        cassette_path = cassette_path.resolve()

    # Fall back to config-level match strategy
    if match_strategy is None:
        match_strategy = request.config.getoption("--mcp-match")

    return cassette_path, match_strategy


# ---------------------------------------------------------------------------
# Public fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp_replay_url(request: pytest.FixtureRequest) -> Generator[str, None, None]:
    """Start a replay server from the marked cassette and yield its URL.

    Usage::

        @pytest.mark.mcp_cassette("cassettes/golden.json")
        def test_my_client(mcp_replay_url):
            # mcp_replay_url == "http://127.0.0.1:<port>/mcp"
            async with Client(mcp_replay_url) as client:
                ...
    """
    cassette_path, match_strategy = _resolve_cassette_path(request)

    mode = request.config.getoption("--mcp-record-mode")
    if mode == "auto" and not cassette_path.exists():
        pytest.skip(f"Cassette not found (auto mode): {cassette_path}")

    if not cassette_path.exists():
        pytest.fail(f"Cassette file not found: {cassette_path}")

    cassette = load_cassette(cassette_path)
    matcher: Matcher = create_matcher(match_strategy, cassette.interactions)
    app = create_replay_app(cassette, matcher)

    port = find_free_port()
    server = UvicornServer(app, port)
    server.start()

    try:
        yield server.url
    finally:
        server.stop()


@pytest.fixture
def mcp_verify_result(request: pytest.FixtureRequest) -> VerifyResult:
    """Run verify against a live server and return the result.

    Requires ``--mcp-target`` to be set.

    Usage::

        @pytest.mark.mcp_cassette("cassettes/golden.json")
        def test_no_regression(mcp_verify_result):
            assert mcp_verify_result.failed == 0
    """
    cassette_path, _ = _resolve_cassette_path(request)

    target = request.config.getoption("--mcp-target")
    if target is None:
        pytest.fail("mcp_verify_result requires --mcp-target to be set")

    if not cassette_path.exists():
        pytest.fail(f"Cassette file not found: {cassette_path}")

    cassette = load_cassette(cassette_path)

    marker = request.node.get_closest_marker("mcp_cassette")
    ignore: frozenset[str] = frozenset()
    if marker is not None:
        ignore_val = marker.kwargs.get("ignore_fields")
        if ignore_val is not None:
            ignore = (
                frozenset(ignore_val) if isinstance(ignore_val, list | tuple | set) else frozenset()
            )

    return run_verify(cassette, target, ignore_fields=ignore)

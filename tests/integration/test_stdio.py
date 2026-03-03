"""Integration tests: stdio transport record and verify."""

from __future__ import annotations

import sys
from pathlib import Path

from mcp_recorder._types import Cassette, CassetteMetadata, InteractionType
from mcp_recorder._utils import UvicornServer, find_free_port, load_cassette
from mcp_recorder.mcp_client import McpClient
from mcp_recorder.proxy import create_proxy_app
from mcp_recorder.scenarios import (
    RedactConfig,
    Scenario,
    StdioTargetConfig,
    _run_single_scenario,
)
from mcp_recorder.transport import StdioTransport
from mcp_recorder.verifier import verify_cassette

STDIO_SERVER = str(Path(__file__).parent.parent / "fixtures" / "stdio_server.py")


def _stdio_target() -> StdioTargetConfig:
    return StdioTargetConfig(command=sys.executable, args=[STDIO_SERVER])


# ---------------------------------------------------------------------------
# Record via scenario runner
# ---------------------------------------------------------------------------


class TestStdioRecordScenario:
    async def test_record_scenario_via_stdio(self, tmp_path: Path) -> None:
        output = tmp_path / "recorded.json"
        count = await _run_single_scenario(
            name="test_scenario",
            scenario=Scenario(
                actions=[
                    "list_tools",
                    {"call_tool": {"name": "add", "arguments": {"a": 2, "b": 3}}},
                    {"call_tool": {"name": "echo", "arguments": {"message": "hello"}}},
                ]
            ),
            target=_stdio_target(),
            output_path=output,
            redact=RedactConfig(server_url=False),
            verbose=False,
        )

        assert output.exists()
        cassette = load_cassette(output)

        # Expect: initialize, notifications/initialized, tools/list,
        #         tools/call (add), tools/call (echo)
        assert count >= 5

        assert cassette.metadata.transport_type == "stdio"
        assert cassette.metadata.server_url.startswith("stdio://")

        # No lifecycle interactions for stdio.
        lifecycle = [i for i in cassette.interactions if i.type == InteractionType.LIFECYCLE]
        assert len(lifecycle) == 0

        # All responses should be non-SSE.
        for interaction in cassette.interactions:
            assert interaction.response_is_sse is False

        # Verify specific tool results are captured.
        add_interactions = [i for i in cassette.interactions if i.tool_name == "add"]
        assert len(add_interactions) == 1
        assert add_interactions[0].response is not None
        assert add_interactions[0].response["result"]["content"][0]["text"] == "5"


# ---------------------------------------------------------------------------
# Verify a stdio-recorded cassette
# ---------------------------------------------------------------------------


class TestStdioVerify:
    async def test_verify_stdio_cassette(self, tmp_path: Path) -> None:
        """Record via stdio, then verify against the same stdio server."""
        output = tmp_path / "golden.json"
        await _run_single_scenario(
            name="verify_test",
            scenario=Scenario(
                actions=[
                    "list_tools",
                    {"call_tool": {"name": "multiply", "arguments": {"a": 3.0, "b": 4.0}}},
                ]
            ),
            target=_stdio_target(),
            output_path=output,
            redact=RedactConfig(server_url=False),
            verbose=False,
        )

        cassette = load_cassette(output)
        transport = StdioTransport(
            command=sys.executable,
            args=[STDIO_SERVER],
        )

        result = await verify_cassette(cassette, transport=transport)
        assert result.failed == 0
        assert result.total >= 4

    async def test_verify_skips_lifecycle_for_stdio(
        self,
        mock_session_cassette: Cassette,
    ) -> None:
        """HTTP cassette (with lifecycle entries) verified via stdio should
        skip lifecycle interactions gracefully."""
        transport = StdioTransport(
            command=sys.executable,
            args=[STDIO_SERVER],
        )

        result = await verify_cassette(
            mock_session_cassette,
            transport=transport,
            ignore_fields=frozenset({"_meta"}),
        )

        # Lifecycle interactions should be marked as passed (skipped).
        lifecycle_results = [r for r in result.results if "DELETE" in r.method or "GET" in r.method]
        assert all(r.passed for r in lifecycle_results)


# ---------------------------------------------------------------------------
# Record via proxy with transport
# ---------------------------------------------------------------------------


class TestStdioProxyRecord:
    async def test_proxy_with_stdio_transport(self) -> None:
        """Start a proxy backed by StdioTransport, drive it with McpClient."""
        transport = StdioTransport(
            command=sys.executable,
            args=[STDIO_SERVER],
        )
        cassette = Cassette(
            metadata=CassetteMetadata(
                server_url=f"stdio://{sys.executable} {STDIO_SERVER}",
                transport_type="stdio",
            ),
        )
        app = create_proxy_app(cassette=cassette, transport=transport, verbose=False)

        port = find_free_port()
        server = UvicornServer(app, port)
        server.start()

        try:
            proxy_url = f"http://127.0.0.1:{port}"
            async with McpClient(proxy_url) as client:
                await client.initialize()
                tools_resp = await client.list_tools()
                assert tools_resp is not None

                add_resp = await client.call_tool("add", {"a": 5, "b": 3})
                assert add_resp is not None
        finally:
            server.stop()

        requests = [i for i in cassette.interactions if i.type == InteractionType.JSONRPC_REQUEST]
        assert len(requests) >= 3

        add_call = [i for i in requests if i.tool_name == "add"]
        assert len(add_call) == 1
        assert add_call[0].response is not None
        assert add_call[0].response["result"]["content"][0]["text"] == "8"

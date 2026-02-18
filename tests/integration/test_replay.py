"""Integration tests: replay server serves correct responses from a cassette."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
from fastmcp import Client

from mcp_recorder._types import Cassette
from mcp_recorder.cli import inspect as inspect_cmd
from mcp_recorder.matcher import create_matcher
from mcp_recorder.pytest_plugin import _ReplayServer
from mcp_recorder.replayer import create_replay_app


def _start_replay(cassette: Cassette) -> tuple[_ReplayServer, str]:
    from mcp_recorder.pytest_plugin import _find_free_port

    matcher = create_matcher("method_params", cassette.interactions)
    app = create_replay_app(cassette, matcher)
    port = _find_free_port()
    server = _ReplayServer(app, port)
    server.start()
    return server, server.url


class TestReplayPipeline:
    async def test_replay_serves_correct_tool_results(
        self, mock_session_cassette: Cassette
    ) -> None:
        server, url = _start_replay(mock_session_cassette)
        try:
            async with Client(url) as client:
                result = await client.call_tool("add", {"a": 2, "b": 3})
                assert result.content[0].text == "5"

                result = await client.call_tool("multiply", {"a": 3.5, "b": 2.0})
                assert result.content[0].text == "7.0"

                result = await client.call_tool("echo", {"message": "hello mcp-recorder"})
                assert result.content[0].text == "hello mcp-recorder"

                result = await client.call_tool("get_weather", {"city": "London"})
                assert "London" in result.content[0].text
        finally:
            server.stop()

    async def test_replay_lists_tools(self, mock_session_cassette: Cassette) -> None:
        server, url = _start_replay(mock_session_cassette)
        try:
            async with Client(url) as client:
                tools = await client.list_tools()
                tool_names = sorted(t.name for t in tools)
                assert tool_names == ["add", "echo", "get_weather", "multiply"]
        finally:
            server.stop()


class TestInspectCli:
    def test_inspect_output(self, mock_session_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(inspect_cmd, [str(mock_session_path)])

        assert result.exit_code == 0
        output = result.output

        assert "mock_session.json" in output
        assert "Test Calculator" in output
        assert "2025-11-25" in output
        assert "initialize" in output
        assert "tools/list" in output
        assert "tools/call [add]" in output
        assert "6 requests" in output
        assert "1 notification" in output
        assert "2 lifecycle" in output

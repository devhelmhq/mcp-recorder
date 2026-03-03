"""Unit tests for stdio target parsing in scenarios.py."""

from __future__ import annotations

from mcp_recorder.scenarios import (
    ScenariosFile,
    StdioTargetConfig,
    _resolve_target,
)
from mcp_recorder.transport import StdioTransport


class TestStdioTargetParsing:
    def test_http_target_string(self) -> None:
        sf = ScenariosFile.model_validate(
            {
                "target": "http://localhost:3000",
                "scenarios": {"s1": {"actions": ["list_tools"]}},
            }
        )
        assert isinstance(sf.target, str)
        assert sf.target == "http://localhost:3000"

    def test_stdio_target_dict(self) -> None:
        sf = ScenariosFile.model_validate(
            {
                "target": {"command": "node", "args": ["server.js"]},
                "scenarios": {"s1": {"actions": ["list_tools"]}},
            }
        )
        assert isinstance(sf.target, StdioTargetConfig)
        assert sf.target.command == "node"
        assert sf.target.args == ["server.js"]

    def test_stdio_target_with_env_and_cwd(self) -> None:
        sf = ScenariosFile.model_validate(
            {
                "target": {
                    "command": "npx",
                    "args": ["-y", "some-mcp-server"],
                    "env": {"API_KEY": "test123"},
                    "cwd": "/tmp",
                },
                "scenarios": {"s1": {"actions": ["list_tools"]}},
            }
        )
        assert isinstance(sf.target, StdioTargetConfig)
        assert sf.target.env == {"API_KEY": "test123"}
        assert sf.target.cwd == "/tmp"

    def test_stdio_target_minimal(self) -> None:
        sf = ScenariosFile.model_validate(
            {
                "target": {"command": "npx"},
                "scenarios": {"s1": {"actions": ["list_tools"]}},
            }
        )
        assert isinstance(sf.target, StdioTargetConfig)
        assert sf.target.command == "npx"
        assert sf.target.args == []
        assert sf.target.env == {}
        assert sf.target.cwd is None


class TestResolveTarget:
    def test_resolve_http_target(self) -> None:
        server_url, target_url, transport = _resolve_target("http://localhost:3000")
        assert server_url == "http://localhost:3000"
        assert target_url == "http://localhost:3000"
        assert transport is None

    def test_resolve_stdio_target(self) -> None:
        cfg = StdioTargetConfig(command="node", args=["server.js"])
        server_url, target_url, transport = _resolve_target(cfg)
        assert server_url == "stdio://node server.js"
        assert target_url is None
        assert isinstance(transport, StdioTransport)

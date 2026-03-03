"""Unit tests for Transport abstraction (StdioTransport + HttpTransport)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mcp_recorder.transport import HttpTransport, StdioTransport

STDIO_SERVER = str(Path(__file__).parent.parent / "fixtures" / "stdio_server.py")

_INIT_REQUEST = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.1.0"},
    },
}

_TOOLS_LIST_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {},
}

_NOTIFICATION = {
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
}


def _make_transport() -> StdioTransport:
    return StdioTransport(command=sys.executable, args=[STDIO_SERVER])


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestStdioTransportLifecycle:
    async def test_connect_spawns_subprocess(self) -> None:
        t = _make_transport()
        await t.connect()
        try:
            assert t._process is not None
            assert t._process.pid > 0
            assert t._process.returncode is None
        finally:
            await t.close()

    async def test_close_terminates_subprocess(self) -> None:
        t = _make_transport()
        await t.connect()
        pid = t._process.pid  # type: ignore[union-attr]
        await t.close()
        assert t._process is not None
        assert t._process.returncode is not None
        assert pid > 0

    async def test_close_is_idempotent(self) -> None:
        t = _make_transport()
        await t.connect()
        await t.close()
        await t.close()

    async def test_context_manager(self) -> None:
        t = _make_transport()
        async with t:
            assert t._process is not None
        assert t._process.returncode is not None


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------


class TestStdioTransportMessaging:
    async def test_send_request_returns_response(self) -> None:
        async with _make_transport() as t:
            resp = await t.send_request(_INIT_REQUEST)
            assert resp is not None
            assert "result" in resp
            assert "serverInfo" in resp["result"]

    async def test_send_notification_does_not_hang(self) -> None:
        async with _make_transport() as t:
            await t.send_request(_INIT_REQUEST)
            await t.send_notification(_NOTIFICATION)

    async def test_request_response_id_matching(self) -> None:
        async with _make_transport() as t:
            resp0 = await t.send_request(_INIT_REQUEST)
            await t.send_notification(_NOTIFICATION)
            resp1 = await t.send_request(_TOOLS_LIST_REQUEST)

            assert resp0 is not None and resp0.get("id") == 0
            assert resp1 is not None and resp1.get("id") == 1
            tools = resp1["result"]["tools"]
            tool_names = sorted(tool["name"] for tool in tools)
            assert tool_names == ["add", "echo", "get_weather", "multiply"]

    async def test_send_request_without_id_raises(self) -> None:
        async with _make_transport() as t:
            with pytest.raises(ValueError, match="'id' field"):
                await t.send_request({"jsonrpc": "2.0", "method": "tools/list"})

    async def test_tools_call_returns_correct_result(self) -> None:
        async with _make_transport() as t:
            await t.send_request(_INIT_REQUEST)
            await t.send_notification(_NOTIFICATION)

            resp = await t.send_request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "add", "arguments": {"a": 10, "b": 7}},
                }
            )
            assert resp is not None
            text = resp["result"]["content"][0]["text"]
            assert text == "17"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestStdioTransportErrors:
    async def test_send_on_closed_transport_raises(self) -> None:
        t = _make_transport()
        await t.connect()
        await t.close()
        with pytest.raises(ConnectionError):
            await t.send_request(_INIT_REQUEST)

    async def test_send_before_connect_raises(self) -> None:
        t = _make_transport()
        with pytest.raises(ConnectionError):
            await t.send_request(_INIT_REQUEST)

    async def test_subprocess_crash_raises(self) -> None:
        t = StdioTransport(command=sys.executable, args=["-c", "pass"])
        await t.connect()
        # Process exits immediately; sending should fail.
        with pytest.raises((ConnectionError, TimeoutError)):
            await t.send_request(_INIT_REQUEST)
        await t.close()


# ---------------------------------------------------------------------------
# HttpTransport (thin wrapper — light coverage)
# ---------------------------------------------------------------------------


class TestHttpTransport:
    async def test_connect_and_close(self) -> None:
        t = HttpTransport("http://localhost:9999")
        await t.connect()
        assert t._client is not None
        await t.close()
        assert t._client is None

    async def test_close_is_idempotent(self) -> None:
        t = HttpTransport("http://localhost:9999")
        await t.connect()
        await t.close()
        await t.close()

"""Unit tests for the transport-based proxy code path in proxy.py."""

from __future__ import annotations

from typing import Any

import httpx

from mcp_recorder._types import Cassette, CassetteMetadata, InteractionType
from mcp_recorder._utils import UvicornServer, find_free_port
from mcp_recorder.proxy import create_proxy_app
from mcp_recorder.transport import Transport

# ---------------------------------------------------------------------------
# Fake transport for deterministic testing
# ---------------------------------------------------------------------------


class FakeTransport(Transport):
    """In-memory transport that returns canned responses."""

    def __init__(self, responses: dict[int | str, dict[str, Any]] | None = None) -> None:
        self.responses = responses or {}
        self.sent_requests: list[dict[str, Any]] = []
        self.sent_notifications: list[dict[str, Any]] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def send_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        self.sent_requests.append(request)
        msg_id = request.get("id")
        return self.responses.get(msg_id)  # type: ignore[arg-type]

    async def send_notification(self, notification: dict[str, Any]) -> None:
        self.sent_notifications.append(notification)

    async def close(self) -> None:
        self.closed = True


class ErrorTransport(FakeTransport):
    """Transport that raises on every send."""

    async def send_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        raise ConnectionError("Subprocess crashed")

    async def send_notification(self, notification: dict[str, Any]) -> None:
        raise ConnectionError("Subprocess crashed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server(
    transport: Transport | None = None,
) -> tuple[Cassette, UvicornServer, str]:
    """Create a proxy app backed by a FakeTransport, start on a free port."""
    cassette = Cassette(metadata=CassetteMetadata(server_url="stdio://test"))
    t = transport or FakeTransport(
        responses={
            0: {"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2025-11-25"}},
            1: {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}},
        }
    )
    app = create_proxy_app(cassette=cassette, transport=t, verbose=False)
    port = find_free_port()
    server = UvicornServer(app, port)
    server.start()
    base_url = f"http://127.0.0.1:{port}"
    return cassette, server, base_url


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTransportProxy:
    async def test_json_rpc_request_forwarded(self) -> None:
        cassette, server, base_url = _make_server()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{base_url}/mcp",
                    json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
                )
            assert resp.status_code == 200
            body = resp.json()
            assert body["result"]["protocolVersion"] == "2025-11-25"

            assert len(cassette.interactions) == 1
            interaction = cassette.interactions[0]
            assert interaction.type == InteractionType.JSONRPC_REQUEST
            assert interaction.request is not None
            assert interaction.request["method"] == "initialize"
            assert interaction.response is not None
            assert interaction.response_is_sse is False
        finally:
            server.stop()

    async def test_notification_returns_202(self) -> None:
        cassette, server, base_url = _make_server()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{base_url}/mcp",
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
            assert resp.status_code == 202

            assert len(cassette.interactions) == 1
            interaction = cassette.interactions[0]
            assert interaction.type == InteractionType.NOTIFICATION
            assert interaction.response_status == 202
        finally:
            server.stop()

    async def test_lifecycle_delete_returns_200(self) -> None:
        cassette, server, base_url = _make_server()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.delete(f"{base_url}/mcp")
            assert resp.status_code == 200
            assert len(cassette.interactions) == 0
        finally:
            server.stop()

    async def test_interaction_captures_latency(self) -> None:
        cassette, server, base_url = _make_server()
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{base_url}/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                )
            assert cassette.interactions[0].latency_ms >= 0
        finally:
            server.stop()

    async def test_transport_error_returns_502(self) -> None:
        cassette, server, base_url = _make_server(transport=ErrorTransport())
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{base_url}/mcp",
                    json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
                )
            assert resp.status_code == 502
            assert "error" in resp.json()
            assert len(cassette.interactions) == 0
        finally:
            server.stop()

    async def test_notification_transport_error_returns_502(self) -> None:
        _cassette, server, base_url = _make_server(transport=ErrorTransport())
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{base_url}/mcp",
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
            assert resp.status_code == 502
        finally:
            server.stop()

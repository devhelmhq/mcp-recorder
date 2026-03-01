"""Lightweight async MCP client and RecordSession context manager."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from mcp_recorder._types import Cassette, CassetteMetadata
from mcp_recorder._utils import (
    UvicornServer,
    find_free_port,
    parse_sse_response,
    save_cassette,
)
from mcp_recorder.proxy import create_proxy_app
from mcp_recorder.scrubber import scrub_cassette

logger = logging.getLogger("mcp_recorder.mcp_client")


class McpClient:
    """Minimal MCP client that speaks JSON-RPC 2.0 over HTTP/SSE.

    Manages request IDs, session tracking, and SSE response parsing.
    Designed to drive interactions through the recording proxy.
    """

    def __init__(self, base_url: str, *, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._mcp_url = f"{self._base_url}/mcp"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=30.0))
        self._request_id = 0
        self._session_id: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> McpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _next_id(self) -> int:
        rid = self._request_id
        self._request_id += 1
        return rid

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if self._session_id:
            h["mcp-session-id"] = self._session_id
        return h

    def _update_session(self, resp: httpx.Response) -> None:
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid

    async def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Send a JSON-RPC request and return the parsed response."""
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params,
        }
        logger.debug("-> %s %s", method, json.dumps(params, default=str)[:200])
        resp = await self._client.post(
            self._mcp_url,
            content=json.dumps(body).encode(),
            headers=self._headers(),
        )
        self._update_session(resp)

        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            result = parse_sse_response(resp.text)
        else:
            try:
                result = resp.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                result = None

        logger.debug("<- %d %s", resp.status_code, str(result)[:200] if result else "(empty)")
        return result

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            body["params"] = params
        logger.debug("-> notification %s", method)
        resp = await self._client.post(
            self._mcp_url,
            content=json.dumps(body).encode(),
            headers=self._headers(),
        )
        self._update_session(resp)
        logger.debug("<- %d (notification ack)", resp.status_code)

    # -- Protocol lifecycle --------------------------------------------------

    async def initialize(self) -> dict[str, Any] | None:
        """Send initialize + notifications/initialized. Returns the server's init result."""
        result = await self._send_request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "mcp-recorder", "version": "0.1.0"},
            },
        )
        await self._send_notification("notifications/initialized")
        return result

    # -- Tools ---------------------------------------------------------------

    async def list_tools(self) -> dict[str, Any] | None:
        return await self._send_request("tools/list", {})

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        return await self._send_request("tools/call", {"name": name, "arguments": arguments or {}})

    # -- Prompts -------------------------------------------------------------

    async def list_prompts(self) -> dict[str, Any] | None:
        return await self._send_request("prompts/list", {})

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = arguments
        return await self._send_request("prompts/get", params)

    # -- Resources -----------------------------------------------------------

    async def list_resources(self) -> dict[str, Any] | None:
        return await self._send_request("resources/list", {})

    async def read_resource(self, uri: str) -> dict[str, Any] | None:
        return await self._send_request("resources/read", {"uri": uri})


class RecordSession:
    """Context manager that records MCP interactions through a proxy.

    Starts a recording proxy, yields an McpClient connected through it,
    and saves the cassette on exit.

    Usage::

        async with RecordSession(
            target="http://localhost:8000",
            output="golden.json",
        ) as client:
            await client.list_tools()
            await client.call_tool("add", {"a": 2, "b": 3})
    """

    def __init__(
        self,
        target: str,
        output: str | Path,
        *,
        redact_server_url: bool = True,
        redact_env: tuple[str, ...] = (),
        redact_patterns: tuple[str, ...] = (),
        verbose: bool = False,
    ) -> None:
        self._target = target
        self._output = Path(output)
        self._redact_server_url = redact_server_url
        self._redact_env = redact_env
        self._redact_patterns = redact_patterns
        self._verbose = verbose
        self._server: UvicornServer | None = None
        self._client: McpClient | None = None
        self._cassette: Cassette | None = None

    async def __aenter__(self) -> McpClient:
        self._cassette = Cassette(metadata=CassetteMetadata(server_url=self._target))
        app = create_proxy_app(
            target_url=self._target, cassette=self._cassette, verbose=self._verbose
        )

        port = find_free_port()
        self._server = UvicornServer(app, port)
        self._server.start()

        self._client = McpClient(f"http://127.0.0.1:{port}")
        await self._client.initialize()
        return self._client

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.close()
        if self._server:
            self._server.stop()
        if self._cassette and self._cassette.interactions:
            self._cassette = scrub_cassette(
                self._cassette,
                redact_server_url=self._redact_server_url,
                redact_env=self._redact_env,
                redact_patterns=self._redact_patterns,
            )
            save_cassette(self._cassette, self._output)
            logger.info(
                "Saved %d interactions to %s",
                len(self._cassette.interactions),
                self._output,
            )
